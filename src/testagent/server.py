"""Local web application and per-task MCP gateway."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, quote

from .paths import ROOT,VERSION,data_root
from .core import Core,DomainError
from .catalog import Catalog
from .runtime import Runtime
from .notifications import Notifications
from .updates import Updates
from .packages import from_zip,from_files,to_zip
from .skills import validate


def example_package():
    base=ROOT/'examples'/'diagnostic-demo'
    return {'id':'diagnostic-demo','version':'1.0.0','name':'诊断视图检查（模拟）','files':{
        'SKILL.md':(base/'README.md').read_text(encoding='utf-8'),
        'contract.json':(base/'contract.json').read_text(encoding='utf-8')}}


def make_server(core,port=0,catalog=None,runtime=None):
    if catalog is None:
        database=core.db.execute('PRAGMA database_list').fetchone()[2]
        catalog=Catalog(core,Path(database).parent)
    runtime=runtime or Runtime(catalog)
    updates=Updates(catalog)
    token=secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*_):pass

        def send(self,status,data,content_type='application/json; charset=utf-8',filename=None):
            body=json.dumps(data,ensure_ascii=False).encode() if not isinstance(data,bytes) else data
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
            if filename:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(filename))
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass

        def allowed(self):
            expected=f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host')!=expected or (self.headers.get('Origin') and self.headers['Origin']!='http://'+expected):
                self.send(403,{'error':'仅允许本机同源访问'});self.close_connection=True;return False
            return True

        def do_GET(self):
            if not self.allowed():return
            url=urlsplit(self.path);q={k:v[0] for k,v in parse_qs(url.query).items()}
            try:
                if url.path=='/api/health':self.send(200,{'version':VERSION,'status':'ready'})
                elif url.path=='/api/state':self.send(200,{**catalog.state(),'token':token,'version':VERSION})
                elif url.path.startswith('/api/tasks/'):
                    ident=url.path.rsplit('/',1)[-1]
                    task=core.task(ident)
                    # Credential references are opaque, never plaintext; remove profile details from normal page payload.
                    public=json.loads(json.dumps(task))
                    if public['snapshot'].get('profile'):public['snapshot']['profile']['config'].pop('credential',None)
                    for device in public['snapshot'].get('devices',{}).values():device.pop('credential',None)
                    self.send(200,{'task':public,'events':core.events(ident,int(q.get('after',0))),
                        'files':catalog.files(ident),'recoveries':runtime.recovery_list(ident)})
                elif url.path=='/api/skills/detail':
                    with core.lock:row=core.db.execute('SELECT package FROM skills WHERE id=? AND version=?',(q['id'],q['version'])).fetchone()
                    if not row:raise DomainError('Skill 不存在')
                    self.send(200,json.loads(row[0]))
                elif url.path=='/api/skills/export':
                    with core.lock:row=core.db.execute('SELECT package FROM skills WHERE id=? AND version=?',(q['id'],q['version'])).fetchone()
                    if not row:raise DomainError('Skill 不存在')
                    package=json.loads(row[0]);self.send(200,to_zip(package),'application/zip',package['id']+'-'+package['version']+'.zip')
                elif url.path=='/api/environments/export':
                    self.send(200,json.dumps(catalog.export_environments(),ensure_ascii=False,indent=2).encode(),'application/json','environments.json')
                elif url.path=='/api/files/download':
                    info,path=catalog.file(q['task_id'],q['file_id'])
                    self.send(200,path.read_bytes(),'application/octet-stream',info['name'])
                elif url.path=='/api/report':self.send(200,catalog.report(q['task_id']),'application/zip','task-'+q['task_id']+'.zip')
                elif url.path=='/api/authoring-skill':
                    from .packages import from_folder
                    self.send(200,to_zip(from_folder(ROOT/'skills'/'testagent-skill-author')),'application/zip','testagent-skill-author.zip')
                elif url.path in ('/','/app.js','/style.css'):
                    name='index.html' if url.path=='/' else url.path[1:]
                    mime={'index.html':'text/html','app.js':'text/javascript','style.css':'text/css'}[name]
                    self.send(200,(ROOT/'web'/name).read_bytes(),mime+'; charset=utf-8')
                else:self.send(404,{'error':'资源不存在'})
            except Exception as exc:self.send(400,{'error':catalog.vault.redact(str(exc))})

        def do_POST(self):
            if not self.allowed():return
            internal=self.path=='/internal/tool'
            if internal:
                gateway=runtime.tokens.get(self.headers.get('Authorization','').removeprefix('Bearer '))
                if not gateway:self.send(403,{'error':'任务工具凭据无效'});self.close_connection=True;return
            elif not secrets.compare_digest(self.headers.get('X-Testagent-Token',''),token):
                self.send(403,{'error':'本机会话已失效，请刷新'});self.close_connection=True;return
            try:
                length=int(self.headers.get('Content-Length',0))
                maximum=800*1024**2 if self.path=='/api/updates/import' else 90*1024**2
                if not 0<length<=maximum:raise DomainError('请求大小无效')
                raw=self.rfile.read(length)
                if self.path=='/api/updates/import':result=updates.inspect(raw)
                elif self.path=='/api/skills/import':
                    package=from_zip(raw);result=core.import_skill(package)
                else:
                    value=json.loads(raw)
                    if not isinstance(value,dict):raise DomainError('请求必须是对象')
                    result=self.dispatch(value,internal,gateway if internal else None)
                self.send(200,{'result':result})
            except Exception as exc:
                message=catalog.vault.redact(str(exc))
                if internal:
                    core.fail(gateway.task,message)
                    gateway.cancelled.set()
                    self.send(200,{'error':message})
                else:self.send(400,{'error':message})

        def dispatch(self,v,internal,gateway):
            if internal:return gateway(v)
            path=self.path
            if path=='/api/devices':return catalog.save_device(v)
            if path=='/api/devices/delete':return catalog.delete_device(v['id'])
            if path=='/api/devices/probe':
                from .ssh import SSHExecutor
                with core.lock:
                    row=core.db.execute('SELECT d.*,s.* FROM devices d JOIN device_settings s ON d.id=s.id WHERE d.id=?',(v['id'],)).fetchone()
                if not row:raise DomainError('设备不存在')
                probe=SSHExecutor(catalog,'probe',threading.Event(),lambda *_:None)
                probe.device=lambda _:dict(row)
                try:
                    client=probe.connect(v['id'])
                    from .ssh import fingerprint
                    return {'connected':True,'fingerprint':fingerprint(client.get_transport().get_remote_server_key())}
                finally:probe.close()
            if path=='/api/environments':return catalog.save_environment(v)
            if path=='/api/environments/delete':return catalog.delete_environment(v['id'])
            if path=='/api/environments/import':return catalog.import_environments(v)
            if path=='/api/profiles':return catalog.save_profile(v)
            if path=='/api/profiles/delete':return catalog.delete_profile(v['id'])
            if path=='/api/profiles/models':return runtime.models(v['id'])
            if path=='/api/profiles/discover':
                from .backends import resolve_command
                found={}
                for name in ('nga','opencode','codeagent'):
                    try:found[name]={'command':resolve_command(name),'found':True}
                    except Exception as exc:found[name]={'found':False,'error':str(exc)}
                return found
            if path=='/api/skills/example':return core.import_skill(example_package())
            if path=='/api/skills':return core.import_skill(v)
            if path=='/api/skills/folder':return core.import_skill(from_files(v['files'],v.get('identity')))
            if path=='/api/skills/validate':return validate(v)
            if path=='/api/skills/delete':
                with core.tx():core.db.execute('DELETE FROM skills WHERE id=? AND version=?',(v['id'],v['version']))
                return {'deleted':True,'historical_versions':'保留在任务快照中'}
            if path=='/api/tasks':
                with runtime.dispatch_lock:
                    task=core.create_task(v['title'],v['environment_id'],v['skill_id'],v['version'],v.get('backend','simulation'),v.get('model',''),v.get('parameters'),v.get('roles'))
                    try:
                        for item in v.get('files',[]):catalog.add_file(task,item['name'],base64.b64decode(item['data'],validate=True))
                    except Exception as exc:
                        core.fail(task,str(exc));raise
                return task
            if path=='/api/tasks/delete':
                if v['task_id'] in runtime.runs:raise DomainError('任务执行器尚未结束')
                return catalog.delete_task(v['task_id'])
            if path=='/api/tasks/message':return catalog.message(v['task_id'],v['text'])
            if path=='/api/files':return catalog.add_file(v['task_id'],v['name'],base64.b64decode(v['data'],validate=True))
            if path=='/api/approve':return core.approve(v['task_id'],v['approval_id'])
            if path=='/api/reject':
                core.fail(v['task_id'],'用户拒绝操作授权')
                if v['task_id'] in runtime.runs:runtime.runs[v['task_id']]['cancel'].set()
                return {'rejected':True}
            if path=='/api/stop':return runtime.stop(v['task_id'])
            if path=='/api/scene/release':return runtime.release_scene(v['task_id'])
            if path=='/api/recovery/propose':return runtime.propose_recovery(v['task_id'])
            if path=='/api/recovery/approve':return runtime.approve_recovery(v['id'])
            if path=='/api/recovery/reject':return runtime.reject_recovery(v['id'])
            if path=='/api/updates/apply':
                result=updates.apply(self.server.server_port)
                threading.Thread(target=self.server.shutdown,daemon=True).start();return result
            raise DomainError('接口不存在')

    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    server.runtime=runtime;server.catalog=catalog
    return server


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data',type=Path,default=data_root())
    args=parser.parse_args()
    core=Core(args.data/'testagent.sqlite3')
    catalog=Catalog(core,args.data);runtime=Runtime(catalog)
    server=make_server(core,args.port,catalog,runtime)
    core.recover_startup()
    runtime.start(server.server_port)
    notifications=Notifications(core,args.data,server.server_port)
    print(f'testagent {VERSION}: http://127.0.0.1:{server.server_port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        runtime.close();notifications.close();core.recover_startup()
        server.server_close()
        with core.lock:core.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

if __name__=='__main__':main()
