"""Approved tuning changes, durable backups and verified restore."""
import json
from pathlib import Path
import shlex
from .core import DomainError,new_id


class Tuning:
    def __init__(self,catalog,task,role,executor):
        self.catalog,self.core,self.task,self.role,self.executor=catalog,catalog.core,task,role,executor
        self.device=self.core.task(task)['snapshot']['roles'][role]

    def apply(self,spec):
        ident=new_id();path='/tmp/testagent-tune-'+ident+'.json';script='/tmp/testagent-tune-'+ident+'.py'
        with self.core.tx():
            if self.core.db.execute('SELECT 1 FROM tuning WHERE task_id=? AND name=?',(self.task,spec['name'])).fetchone():raise DomainError('调优名称已使用')
            self.core.db.execute('INSERT INTO tuning VALUES(?,?,?,?,?,0)',(ident,self.task,self.role,spec['name'],json.dumps({'path':path,'script':script,'spec':spec})))
        self.executor.write_file(self.device,script,(Path(__file__).parent/'remote/tune.py').read_bytes())
        try:
            result=self.executor.exec(self.device,shlex.join(['python3',script,'capture',path,json.dumps(spec)]),30)
        except Exception:
            with self.core.tx():self.core.db.execute('UPDATE tuning SET restored=1 WHERE id=?',(ident,))
            raise
        record=json.loads(result['stdout'])
        self.catalog.add_file(self.task,'tuning-'+spec['name']+'.json',json.dumps(record).encode(),'backup')
        with self.core.tx():self.core.db.execute('UPDATE tuning SET data=? WHERE id=?',(json.dumps({'path':path,'script':script,'spec':spec,'backup':record}),ident))
        result=self.executor.exec(self.device,shlex.join(['python3',script,'apply',path]),30)
        return json.loads(result['stdout'])

    def restore(self,name):
        owner=self.core.task(self.task)['snapshot'].get('parent_task',self.task)
        with self.core.lock:
            row=self.core.db.execute('SELECT * FROM tuning WHERE task_id=? AND role=? AND name=?',(owner,self.role,name)).fetchone()
        if not row:raise DomainError('没有此调优记录')
        value=json.loads(row['data'])
        if row['restored']:return {'restored':True,'name':name}
        result=self.executor.exec(self.device,shlex.join(['python3',value['script'],'restore',value['path']]),30)
        record=json.loads(result['stdout'])
        if not record.get('restored'):raise DomainError('调优恢复未核实')
        with self.core.tx():self.core.db.execute('UPDATE tuning SET restored=1 WHERE id=?',(row['id'],))
        return record
