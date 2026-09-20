"""Single tool contract shared by OpenAI function calling and ACP's MCP bridge."""
import json
import re
import shlex
import time
from .core import DomainError
from .ssh import Cancelled, SSHExecutor
from .execution import OPERATION_SCHEMA, REMOTE_ACTIONS, validate_operation

ACTIONS = ['skill_read','files','file_read','artifact_write','exec','shell_open','shell_send','shell_close',
           'remote_read','remote_write','upload','download','script','wait_connected','step','check','ask','finish','preview','load_start','load_status','load_wait','load_stop','tool_deploy','tune_apply','tune_restore','tool_list']
TOOL = {'name':'testagent','description': '统一测试任务工具。所有设备操作必须通过此工具；role 来自任务绑定。先读取 Skill 和附件，按约定上报步骤与检查，最后 finish。',
        'inputSchema': {'type':'object','properties': {
            'action':{'type':'string','enum':ACTIONS}, 'role':{'type':'string'}, 'action_id':{'type':'string'},
            'command':{'type':'string'},'timeout':{'type':'number','minimum':1,'maximum':86400},
            'session_id':{'type':'string'}, 'text':{'type':'string'}, 'expect':{'type':'string'}, 'expect_disconnect':{'type':'boolean'},
            'path':{'type':'string'}, 'file_id':{'type':'string'}, 'name':{'type':'string'},
            'args':{'type':'array','items':{'type':'string'}}, 'step_id':{'type':'string'},
            'check_id':{'type':'string'},'passed':{'type':'boolean'},'evidence_operation':{'type':'string'},
            'summary':{'type':'string'}, 'impact':{'type':'string'}, 'verification':{'type':'string'},
            'load':OPERATION_SCHEMA['properties']['load'], 'tuning':OPERATION_SCHEMA['properties']['tuning'],
            'tool_id':{'type':'string'},
            'capture_id':{'type':'string'}, 'capture_phase':{'enum':['before','after']},
            'operations':{'type':'array','maxItems':200,'items':OPERATION_SCHEMA}},'required':['action'],'additionalProperties':False}}


class Gateway:
    def __init__(self, runtime, task, cancelled, readonly=False):
        self.runtime,self.catalog,self.core=runtime,runtime.catalog,runtime.core
        self.task,self.cancelled,self.readonly=task,cancelled,readonly
        self.executor=SSHExecutor(self.catalog,task,cancelled,self.emit)
        self.finished=False
        self.active_operation=None
        self.call_lock=__import__('threading').Lock()

    def emit(self,kind,payload):
        if kind=='tool.output' and self.active_operation:
            payload={**payload,**self.active_operation}
        with self.core.tx(): self.core.emit(self.task,kind,payload)

    def snapshot(self): return self.core.task(self.task)['snapshot']

    def __call__(self,args):
        try:
            return self.catalog.vault.redact_tree(self.execute(args))
        except Cancelled:
            raise
        except Exception as exc:
            # MCP clients receive a tool error, but cannot recover/retry this task themselves.
            if not self.readonly:
                self.core.fail(self.task,self.catalog.vault.redact(str(exc)))
            self.cancelled.set()
            raise

    def execute(self,args):
        from jsonschema import validate
        validate(args,TOOL['inputSchema'])
        with self.call_lock:
            if self.cancelled.is_set(): raise Cancelled('任务已停止')
            if not self.readonly and self.core.task(self.task)['status'] not in ('running','waiting_user'): raise Cancelled('任务不再执行')
            if self.finished: raise DomainError('任务已提交完成请求，不再接受操作')
            action=args['action']
            if self.readonly and action not in ('skill_read','files','file_read'):
                raise DomainError('恢复方案生成阶段只允许读取任务文件，不能操作远端或更改任务')
            if action=='tool_list':
                from .tool_library import ToolLibrary
                return ToolLibrary(self.catalog).list()
            if action=='skill_read':
                name=args.get('path','SKILL.md')
                files=self.snapshot()['skill']['files']
                if name not in files: raise DomainError('Skill 文件不存在')
                return {'path':name,'text':files[name]}
            if action=='files': return self.catalog.files(self.task)
            if action=='preview':
                plan_id=self.core.propose_preview(self.task,args['summary'],args['impact'],args['verification'],args['operations'])
                while not self.cancelled.wait(.15):
                    with self.core.lock:
                        row=self.core.db.execute('SELECT status FROM previews WHERE id=?',(plan_id,)).fetchone()
                    if self.core.task(self.task)['status'] in ('failed','stopped','stopping'):
                        raise Cancelled('任务已终止')
                    if row['status']=='approved': return {'preview_id':plan_id,'approved':True}
                    if row['status']=='superseded':
                        return {'approved':False,'new_user_instructions':self.catalog.drain_messages(self.task),'next':'重新生成变更预览'}
                raise Cancelled('任务已停止')
            if action=='file_read':
                info,path=self.catalog.file(self.task,args['file_id'])
                if info['size']>2*1024*1024: raise DomainError('模型读取文件限 2 MiB，请使用下载或脚本处理')
                return {'name':info['name'],'text':path.read_text(encoding='utf-8')}
            if action=='artifact_write':
                return {'file_id':self.catalog.add_file(self.task,args['name'],self.catalog.vault.redact(args['text']).encode(),'output')}
            if action=='step':
                self.core.complete_step(self.task,args['step_id']); return {'completed':True}
            if action=='check':
                evidence=args['evidence_operation']
                with self.core.lock:
                    row=self.core.db.execute("SELECT payload FROM events WHERE task_id=? AND kind='tool.finished' AND json_extract(payload,'$.id')=? ORDER BY seq DESC LIMIT 1",(self.task,evidence)).fetchone()
                if not row: raise DomainError('没有找到工具执行证据')
                contract=json.loads(self.snapshot()['skill']['files'].get('contract.json','{}'))
                definition=next((x for x in contract.get('checks',[]) if x['id']==args['check_id']),{})
                output=json.loads(row[0])['output']
                if isinstance(output,dict) and output.get('error'): raise DomainError('失败操作不能作为通过证据')
                rule=definition.get('assertion')
                passed=args.get('passed',False)
                if rule:
                    value=json.dumps(output,ensure_ascii=False)
                    if 'contains' in rule: passed=rule['contains'] in value
                    elif 'equals' in rule:
                        actual=output
                        for key in rule.get('field','').split('.'):
                            if key: actual=actual.get(key) if isinstance(actual,dict) else None
                        passed=actual==rule['equals']
                self.core.check(self.task,args['check_id'],bool(passed),evidence)
                if not passed: raise DomainError('检查点未通过：'+args['check_id'])
                return {'passed':passed,'basis':'deterministic' if rule else 'agent_evaluation_of_tool_evidence'}
            if action=='ask':
                with self.core.tx():
                    self.core.db.execute("UPDATE tasks SET status='waiting_user' WHERE id=?",(self.task,))
                    self.core.emit(self.task,'input.requested',{'text':args['text']})
                while not self.cancelled.wait(.15):
                    messages=self.catalog.drain_messages(self.task)
                    if messages:
                        with self.core.tx():
                            self.core.db.execute("UPDATE tasks SET status='running' WHERE id=? AND status='waiting_user'",(self.task,))
                        return {'answer':'\n'.join(messages)}
                raise Cancelled('任务已停止')
            if action=='finish':
                # finish is called only after all tools have returned; the worker checks process
                # cleanup before declaring success and releasing reservations.
                self.finished=True
                self.emit('agent.summary',{'text':args.get('summary','任务执行结束')})
                return {'finish_requested':True}
            role=args.get('role')
            validate_operation(args,self.snapshot())
            device=self.snapshot()['roles'].get(role)
            if not device: raise DomainError('未知设备角色')
            if args.get('capture_id'):
                with self.core.lock:
                    exists=self.core.db.execute('SELECT 1 FROM config_captures WHERE task_id=? AND comparison_id=? AND phase=?',
                        (self.task,args['capture_id'],args['capture_phase'])).fetchone()
                if exists:raise DomainError('配置采集证据已经保存，不能覆盖')
            operation=self.core.request_operation(self.task,device,json.dumps(args,ensure_ascii=False),args.get('action_id'),force_confirm=False)
            while not self.cancelled.wait(.1):
                with self.core.lock:
                    row=self.core.db.execute('SELECT status FROM approvals WHERE id=?',(operation,)).fetchone()
                if row and row[0]=='approved': break
                if row and row[0]=='finished':
                    return {'not_executed':True,'new_user_instructions':self.catalog.drain_messages(self.task),'next':'重新生成变更预览'}
                if self.core.task(self.task)['status'] in ('failed','stopped','stopping'): raise Cancelled('任务已终止')
            if self.cancelled.is_set(): raise Cancelled('任务已停止')
            pending=self.catalog.drain_messages(self.task)
            if pending:
                # A changed request invalidates a prepared operation. Do not execute the stale command.
                with self.core.tx():
                    self.core.db.execute("UPDATE approvals SET status='finished' WHERE id=?",(operation,))
                    self.core.emit(self.task,'operation.superseded',{'id':operation,'reason':'用户有新要求，需重新规划'})
                return {'not_executed':True,'new_user_instructions':pending}
            self.core.consume_operation(self.task,operation)
            self.active_operation={'id':operation,'device_id':device}
            timeout=args.get('timeout',300)
            try:
                if action=='load_start':result=self.runtime.workloads.start(self.task,role,args['load'],self.executor,operation)
                elif action in ('load_status','load_stop','load_wait'):
                    jobs=self.runtime.workloads.list(self.task,False)
                    if not any(j['name']==args['name'] and j['role']==role for j in jobs):raise DomainError('负载不属于此角色')
                    result=self.runtime.workloads.refresh(self.task,args['name'],action=='load_stop')
                    if action=='load_wait':
                        deadline=time.monotonic()+timeout
                        while True:
                            if args.get('expect') and re.search(args['expect'],result.get('output_tail','')):break
                            if not args.get('expect') and result.get('confirmed_exit') and not result.get('more'):break
                            if result.get('state') in ('failed','stopped'):raise DomainError('负载提前结束：'+str(result))
                            if time.monotonic()>deadline:raise TimeoutError('等待负载超时')
                            if self.cancelled.wait(1):raise Cancelled('任务已停止')
                            result=self.runtime.workloads.refresh(self.task,args['name'])
                elif action=='tool_deploy':
                    from .tool_library import ToolLibrary
                    result=ToolLibrary(self.catalog).deploy(args['tool_id'],self.executor,device)
                elif action in ('tune_apply','tune_restore'):
                    from .tuning import Tuning
                    tuning=Tuning(self.catalog,self.task,role,self.executor)
                    result=tuning.apply(args['tuning']) if action=='tune_apply' else tuning.restore(args['name'])
                elif action=='exec': result=self.executor.exec(device,args['command'],timeout,operation)
                elif action=='shell_open': result=self.executor.shell_open(device,timeout,args.get('expect'))
                elif action=='shell_send': result=self.executor.shell_send(device,args['session_id'],args['text'],args.get('expect'),timeout,args.get('expect_disconnect',False))
                elif action=='shell_close': result=self.executor.shell_close(device,args['session_id'])
                elif action=='wait_connected': result=self.executor.wait_connected(device,timeout)
                elif action=='remote_read':
                    data=self.executor.read_file(device,args['path'])
                    if len(data)>2*1024*1024: raise DomainError('请用 download 保存大文件')
                    result={'text':data.decode('utf-8'),'path':args['path']}
                elif action in ('remote_write','upload'):
                    data=args['text'].encode() if action=='remote_write' else self.catalog.file(self.task,args['file_id'])[1].read_bytes()
                    result=self.executor.write_file(device,args['path'],data)
                elif action=='download':
                    data=self.executor.read_file(device,args['path'])
                    result={'file_id':self.catalog.add_file(self.task,args.get('name') or args['path'].rsplit('/',1)[-1],data,'output')}
                elif action=='script':
                    if args.get('file_id'): data=self.catalog.file(self.task,args['file_id'])[1].read_bytes()
                    else:
                        text=self.snapshot()['skill']['files'].get(args.get('path'))
                        if text is None: raise DomainError('Skill 脚本不存在')
                        data=text.encode()
                    remote='/tmp/testagent-script-'+operation+'.sh'
                    self.executor.write_file(device,remote,data)
                    result=self.executor.exec(device,'sh '+shlex.quote(remote)+' '+ ' '.join(shlex.quote(x) for x in args.get('args',[])),timeout,operation)
                else: raise DomainError('不支持的工具操作')
                self.core.record_result(self.task,operation,result)
                if args.get('capture_id'):
                    self.catalog.capture(self.task,args,operation,result)
                return {'operation_id':operation,**result}
            except Exception as exc:
                with self.core.lock:
                    unfinished=self.core.db.execute("SELECT 1 FROM approvals WHERE id=? AND status='consumed'",(operation,)).fetchone()
                if unfinished:self.core.record_result(self.task,operation,{'error':str(exc)})
                raise
            finally:
                self.active_operation=None
