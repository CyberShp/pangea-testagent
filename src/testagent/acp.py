"""ACP JSON-RPC/stdio adapter. Framework tools exposed via task-scoped MCP bridge."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from .backends import launch_spec,resolve_command
from .core import DomainError
from .ssh import Cancelled


class ACP:
    def __init__(self,profile,cwd,cancelled,emit,bridge=None):
        self.profile,self.cwd,self.cancelled,self.emit,self.bridge=profile,str(cwd),cancelled,emit,bridge
        self.process=None;self.pending={};self.lock=threading.Lock();self.next_id=0;self.session=None
        self.last_models=[];self.options=[];self.stderr=[]

    def start(self):
        config=self.profile['config'];spec=launch_spec(self.profile['kind'])
        command=config.get('command') or spec.command
        command=resolve_command(command)
        args=list(config.get('args') or spec.args)
        if self.profile['kind']=='opencode' and 'acp' in args:
            if '--print-logs' not in args: args.append('--print-logs')
            if not any(a=='--log-level' or a.startswith('--log-level=') for a in args): args.extend(['--log-level','ERROR'])
        env=os.environ.copy();env.update(spec.env)
        env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])+os.pathsep+env.get('PYTHONPATH','')
        if self.profile['kind']=='opencode':
            # Process-local policy. Does not rewrite the user's installed Agent configuration.
            env['OPENCODE_CONFIG_CONTENT']=json.dumps({'permission':{'*':'deny','testagent_*':'allow'}})
        argv=[command,*args]
        if os.name=='nt' and command.lower().endswith(('.cmd','.bat')):
            if any(any(c in a for c in '&|<>^%!\r\n') for a in argv):
                raise DomainError('Windows 批处理启动参数含不支持的命令解释符，请使用绝对程序路径及普通参数')
            argv=subprocess.list2cmdline([os.environ.get('COMSPEC','cmd.exe')])+' /d /s /c "'+subprocess.list2cmdline(argv)+'"'
        self.process=subprocess.Popen(argv,cwd=self.cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    text=True,encoding='utf-8',errors='replace',bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0,
                    start_new_session=os.name!='nt')
        threading.Thread(target=self._read,daemon=True).start()
        threading.Thread(target=self._stderr,daemon=True).start()
        result=self.call('initialize',{'protocolVersion':1,'clientCapabilities':{'fs':{'readTextFile':False,'writeTextFile':False},'terminal':False},'clientInfo':{'name':'pangea-testagent','version':'1.0.0'}},timeout=60)
        if result.get('protocolVersion')!=1: raise DomainError('ACP 协议版本不支持')
        self.emit('backend.initialized',{'agent':result.get('agentInfo',{}),'capabilities':result.get('agentCapabilities',{})})
        params={'cwd':str(Path(self.cwd).resolve()),'mcpServers':[]}
        if self.bridge: params['mcpServers']=[self.bridge]
        result=self.call('session/new',params,timeout=90)
        self.session=result['sessionId']
        self.last_models=result.get('models',{}).get('availableModels',[])
        self.options=result.get('configOptions',[])
        if not self.last_models:
            for option in self.options:
                if option.get('category')=='model':
                    self.last_models=[{'modelId':x['value'],'name':x.get('name',x['value'])} for x in option.get('options',[]) if 'value' in x]
        return result

    def _send(self,message):
        with self.lock:
            if not self.process or self.process.poll() is not None: raise DomainError('ACP 进程已退出')
            self.process.stdin.write(json.dumps(message,ensure_ascii=False)+'\n');self.process.stdin.flush()

    def call(self,method,params,timeout=None):
        self.next_id+=1;ident=self.next_id;result=queue.Queue();self.pending[ident]=result
        self._send({'jsonrpc':'2.0','id':ident,'method':method,'params':params})
        deadline=time.monotonic()+timeout if timeout else None
        try:
            while True:
                if self.cancelled.is_set(): raise Cancelled('任务已停止')
                try:
                    response=result.get(timeout=.15)
                    if 'error' in response: raise DomainError('ACP：'+str(response['error']))
                    return response.get('result',{})
                except queue.Empty:
                    if self.process.poll() is not None: raise DomainError('ACP 进程退出：'+'\n'.join(self.stderr)[-2000:])
                    if deadline and time.monotonic()>deadline: raise TimeoutError('ACP '+method+' 超时')
        finally: self.pending.pop(ident,None)

    def _read(self):
        try:
            for line in self.process.stdout:
                try: msg=json.loads(line)
                except ValueError:
                    self.emit('backend.diagnostic',{'text':line[:2000]});continue
                if 'method' not in msg and msg.get('id') in self.pending:
                    self.pending[msg['id']].put(msg);continue
                if msg.get('method')=='session/update':
                    update=msg.get('params',{}).get('update',{})
                    if update.get('sessionUpdate')=='agent_message_chunk':
                        self.emit('agent.chunk',{'text':update.get('content',{}).get('text','')})
                    else: self.emit('agent.event',update)
                elif 'id' in msg:
                    # Agent-owned terminal and filesystem requests are never forwarded.
                    if msg.get('method')=='session/request_permission':
                        params=msg.get('params',{});title=params.get('toolCall',{}).get('title','')
                        allow=title in ('testagent','testagent.testagent','mcp__testagent__testagent','testagent_testagent')
                        option=next((x for x in params.get('options',[]) if x.get('kind')==('allow_once' if allow else 'reject_once')),None)
                        outcome={'outcome':'selected','optionId':option['optionId']} if option else {'outcome':'cancelled'}
                        self._send({'jsonrpc':'2.0','id':msg['id'],'result':{'outcome':outcome}})
                        if not allow: self.emit('backend.permission_denied',{'tool':title,'reason':'请使用统一 testagent 工具'})
                    else:
                        self._send({'jsonrpc':'2.0','id':msg['id'],'error':{'code':-32601,'message':'Client capability not supported; use testagent MCP'}})
        except Exception as exc:
            for pending in list(self.pending.values()): pending.put({'error':str(exc)})

    def _stderr(self):
        for line in self.process.stderr:
            self.stderr.append(line.rstrip());self.stderr=self.stderr[-30:]
            self.emit('backend.stderr',{'text':line[:2000]})

    def select_model(self,model):
        if not model: return
        option=next((o for o in self.options if o.get('category')=='model'),None)
        if option:
            self.call('session/set_config_option',{'sessionId':self.session,'configId':option['id'],'value':model},timeout=30)
        else: self.call('session/set_model',{'sessionId':self.session,'modelId':model},timeout=30)

    def run(self,prompt,model,gateway,messages):
        self.start();self.select_model(model)
        texts=[prompt]
        for _ in range(100):
            texts+=messages()
            result=self.call('session/prompt',{'sessionId':self.session,'prompt':[{'type':'text','text':'\n\n'.join(texts)}]})
            if self.cancelled.is_set(): raise Cancelled('任务已停止')
            if result.get('stopReason') not in ('end_turn','completed'): raise DomainError('ACP 停止原因：'+str(result.get('stopReason')))
            if gateway.finished or gateway.readonly: return ''
            texts=messages()
            if not texts: raise DomainError('ACP 会话结束但未完成任务约定；不自动重复续接')
        raise DomainError('ACP 会话轮数超限')

    def cancel(self):
        if self.session and self.process and self.process.poll() is None:
            try: self._send({'jsonrpc':'2.0','method':'session/cancel','params':{'sessionId':self.session}})
            except Exception: pass
        self.close()

    def close(self):
        process=self.process
        if not process: return
        if process.poll() is None:
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=15)
            else:
                import signal
                try: os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError: pass
                try: process.wait(2)
                except subprocess.TimeoutExpired:
                    try: os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError: pass
            try: process.wait(5)
            except subprocess.TimeoutExpired: pass
        for pipe in (process.stdin,process.stdout,process.stderr):
            try: pipe.close()
            except Exception: pass
