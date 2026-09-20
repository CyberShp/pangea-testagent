"""Durable remote load ownership, polling and termination through pinned SSH."""
import json
from pathlib import Path
import shlex
import threading
import time
from .core import DomainError,new_id
from .ssh import SSHExecutor
from .load_plans import compile_load
from .metrics import parse_line,compare_rounds

TERMINAL={'succeeded','failed','stopped'}


class Workloads:
    def __init__(self,catalog):
        self.catalog,self.core=catalog,catalog.core
        self.closed=threading.Event();self.lock=threading.RLock()
        self.thread=None

    def start_monitor(self):
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()

    def list(self,task, samples=True):
        with self.core.lock:
            rows=list(self.core.db.execute('SELECT * FROM workloads WHERE task_id=? ORDER BY created',(task,)))
            results=[]
            for row in rows:
                item=dict(row);item['spec']=json.loads(item['spec']);item['status']=json.loads(item['status'])
                if samples:
                    item['samples']=[json.loads(r[0]) for r in self.core.db.execute(
                        'SELECT sample FROM load_samples WHERE workload_id=? ORDER BY seq DESC LIMIT 2000',(item['id'],))][::-1]
                results.append(item)
        return results

    def remote(self,executor,device,directory,action,offset=0):
        command=shlex.join(['python3',directory+'/runner.py',action,directory,str(offset)])
        client=executor.connect(device,fresh=True)
        try:
            stdin,out,err=client.exec_command(command);stdin.close()
            result=executor._collect(out.channel,20,output=False)
            if result['exit_code']:raise DomainError(result['stderr'][-2000:] or '远端负载操作失败')
            return json.loads(result['stdout'])
        finally:client.close()

    def start(self,task,role,spec,executor,operation):
        from .skills import validate_workload_binding
        validate_workload_binding(self.core.task(task)['snapshot'],role,spec)
        device=self.core.task(task)['snapshot']['roles'][role]
        ident=new_id();directory='/tmp/testagent-load-'+ident
        compiled=compile_load(spec,directory)
        with self.lock:
            with self.core.tx():
                if self.core.db.execute('SELECT 1 FROM workloads WHERE task_id=? AND name=?',(task,spec['name'])).fetchone():
                    raise DomainError('负载名称已使用，请为每轮测试使用新名称')
                self.core.db.execute('INSERT INTO workloads VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (ident,task,role,device,spec['name'],json.dumps(spec),directory,'starting','{}',0,time.time(),operation))
            # Persist before any remote mutation; interrupted startup is reconciled, never relaunched.
            if spec['driver']=='vdbench':
                code=(Path(__file__).parent/'remote/block_guard.py').read_text()
                guard=executor.exec(device,shlex.join(['python3','-c',code,json.dumps(spec['vdbench'])]),30)
                self.catalog.add_file(task,'targets-'+spec['name']+'.json',guard['stdout'].encode(),'output')
            config={**compiled,'marker':ident,'grace':spec.get('iperf',{}).get('warmup',0)+30}
            executor.exec(device,'umask 077; mkdir '+shlex.quote(directory),30)
            executor.write_file(device,directory+'/runner.py',(Path(__file__).parent/'remote/load_runner.py').read_bytes())
            executor.write_file(device,directory+'/config.json',json.dumps(config).encode())
            for path,text in compiled['files'].items():executor.write_file(device,path,text.encode())
            result=self.remote(executor,device,directory,'start')
            self.store(ident,result)
            return {'name':spec['name'],'workload_id':ident,'status':result,'transition':compiled['transition']}

    def store(self,ident,status):
        text=status.pop('text','');offset=status.pop('offset',None)
        with self.core.tx():
            row=self.core.db.execute('SELECT * FROM workloads WHERE id=?',(ident,)).fetchone()
            spec=json.loads(row['spec']);state=status.get('state','unknown')
            if state in TERMINAL and (not status.get('confirmed_exit') or offset is None or status.get('more')):state='finishing'
            old=json.loads(row['status'])
            status['output_tail']=self.catalog.vault.redact(text[-4000:]) if text else old.get('output_tail','')
            self.core.db.execute('UPDATE workloads SET state=?,status=?,offset=? WHERE id=?',
                (state,json.dumps(status),row['offset'] if offset is None else offset,ident))
            count=0
            for line in text.splitlines():
                for sample in parse_line(line,spec.get('parser',spec['driver'] if spec['driver'] in ('iperf3','vdbench') else 'none'),spec.get('metrics',[])):
                    self.core.db.execute('INSERT INTO load_samples(workload_id,sample) VALUES(?,?)',(ident,json.dumps(sample)))
                    count+=1
            if text:
                # Local raw evidence is append-only and survives remote cleanup.
                path=self.catalog.task_dir(row['task_id'])/('load-'+ident+'.log')
                with path.open('a',encoding='utf-8',newline='') as stream:stream.write(self.catalog.vault.redact(text))
            if state!=row['state'] or count:
                self.core.emit(row['task_id'],'workload.updated',{'id':ident,'name':row['name'],'state':state,'samples':count})

    def refresh(self,task,name,stop=False):
        with self.lock:
            with self.core.lock:
                row=self.core.db.execute('SELECT * FROM workloads WHERE task_id=? AND name=?',(task,name)).fetchone()
            if not row:raise DomainError('当前任务没有此负载')
            executor=SSHExecutor(self.catalog,task,threading.Event(),lambda *_:None)
            try:
                status=self.remote(executor,row['device_id'],row['directory'],'stop' if stop else 'read',row['offset'])
                self.store(row['id'],status)
                if stop:
                    status=self.remote(executor,row['device_id'],row['directory'],'read',row['offset'])
                    self.store(row['id'],status)
                return {'name':name,**status}
            except Exception as exc:
                self.store(row['id'],{'state':'unknown','error':self.catalog.vault.redact(str(exc)),'confirmed_exit':False})
                raise
            finally:executor.close()

    def stop_all(self,task):
        results=[]
        for job in self.list(task,False):
            if job['state'] not in TERMINAL:
                try:result=self.refresh(task,job['name'],True)
                except Exception as exc:result={'name':job['name'],'error':str(exc),'confirmed_exit':False}
                results.append(result)
        return {'confirmed':all(r.get('confirmed_exit') for r in results),'workloads':results}

    def settled(self,task):
        return all(j['state'] in TERMINAL for j in self.list(task,False))

    def loop(self):
        while not self.closed.wait(2):
            with self.core.lock:
                rows=list(self.core.db.execute("SELECT task_id,name FROM workloads WHERE state NOT IN ('succeeded','failed','stopped')"))
            for row in rows:
                if self.closed.is_set():return
                try:self.refresh(row['task_id'],row['name'])
                except Exception:pass  # State is explicitly unknown until the next successful read.
