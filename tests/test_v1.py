import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import urlopen

from testagent.core import Core,DomainError
from testagent.catalog import Catalog
from testagent.packages import from_zip,to_zip
from testagent.runtime import Runtime
from testagent.tools import Gateway
from testagent.ssh import Cancelled
from testagent.server import make_server,example_package
from testagent.ssh import SSHExecutor
from testagent.openai_backend import OpenAIBackend
from testagent.acp import ACP
from testagent.updates import Updates
from testagent.skills import validate
from fixtures.ssh_server import SSHFixture


def until(predicate,seconds=6):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.05)
    raise AssertionError('condition not observed before deadline')


class V1Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.core=Core(self.root/'db.sqlite3');self.catalog=Catalog(self.core,self.root/'data')
        self.device=self.catalog.save_device({'name':'fixture','address':'127.0.0.1','username':'tester','password':'fixture-secret'})
        self.env=self.catalog.save_environment({'name':'fixture','roles':{'controller':self.device},'policy':'automatic'})
        self.package=example_package()
        contract=json.loads(self.package['files']['contract.json']);contract['steps']=[];contract['checks']=[];contract['critical_actions']=[]
        self.package['files']['contract.json']=json.dumps(contract)
        self.core.import_skill(self.package)

    def tearDown(self):
        self.core.db.close();self.temp.cleanup()

    def profile(self,kind='openai',config=None):
        return self.catalog.save_profile({'name':'fixture','kind':kind,'config':config or {'base_url':'http://127.0.0.1:1/v1'}})

    def task(self,profile=None):return self.core.create_task('fixture task',self.env,'diagnostic-demo','1.0.0',profile or self.profile(),'fixture-model')

    def test_vault_redaction_export_and_preserve_password(self):
        self.assertNotIn(b'fixture-secret',b''.join(p.read_bytes() for p in (self.catalog.root/'credentials').glob('*.secret')))
        self.catalog.save_device({'id':self.device,'name':'renamed','address':'127.0.0.1','username':'tester','password':''})
        state=self.catalog.state();self.assertTrue(state['devices'][0]['has_password'])
        self.assertNotIn('fixture-secret',json.dumps(state))
        task=self.task()
        with self.core.tx():self.core.emit(task,'test',{'text':'fixture-secret'})
        self.assertNotIn('fixture-secret',json.dumps(self.core.events(task)))
        self.assertNotIn('password',json.dumps(self.catalog.export_environments()))

    def test_numeric_credential_redaction_preserves_json_structure(self):
        self.catalog.vault.put('numeric-fixture','1')
        task=self.task()
        with self.core.tx():self.core.emit(task,'numeric',{'count':1,'message':'secret=1','operation_id':'a1b2'})
        event=self.core.events(task)[-1]
        self.assertEqual(event['payload']['count'],1)
        self.assertEqual(event['payload']['operation_id'],'a1b2')
        self.assertEqual(event['payload']['message'],'secret=[已隐藏凭据]')
        with zipfile.ZipFile(io.BytesIO(self.catalog.report(task))) as archive:
            json.loads(archive.read('events.json'))
            json.loads(archive.read('task.json'))

    def test_skill_zip_validation_and_parameter_defaults(self):
        package=self.package.copy();package['files']=dict(package['files'])
        contract=json.loads(package['files']['contract.json']);contract['parameters']={'type':'object','properties':{'count':{'type':'integer','minimum':1,'default':2}},'required':['count']}
        package['files']['contract.json']=json.dumps(contract);package['version']='2'
        roundtrip=from_zip(to_zip(package));self.assertEqual(validate(roundtrip)['mode'],'structured')
        self.core.import_skill(roundtrip)
        task=self.core.create_task('params',self.env,package['id'],'2',self.profile(),'model')
        self.assertEqual(self.core.task(task)['snapshot']['parameters']['count'],2)
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:archive.writestr('../SKILL.md','escape')
        with self.assertRaises(ValueError):from_zip(stream.getvalue())

    def test_artifacts_ownership_history_and_delete_guards(self):
        one,two=self.task(),self.task()
        file=self.catalog.add_file(one,'config.txt',b'abc')
        with self.assertRaises(DomainError):self.catalog.file(two,file)
        with self.assertRaises(DomainError):self.catalog.delete_task(one)
        self.core.fail(one,'test')
        self.catalog.release_scene(one)
        report=self.catalog.report(one)
        with zipfile.ZipFile(io.BytesIO(report)) as archive:self.assertIn('report.md',archive.namelist())
        self.catalog.delete_task(one)
        with self.assertRaises(DomainError):self.core.task(one)

    def test_environment_export_import_reuses_device(self):
        before=len(self.catalog.state()['devices'])
        self.catalog.import_environments(self.catalog.export_environments())
        self.assertEqual(len(self.catalog.state()['devices']),before)
        self.assertEqual(len(self.catalog.state()['environments']),2)

    def test_openai_streamed_function_call_and_models(self):
        class API(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                body=json.dumps({'data':[{'id':'fixture-model'}]}).encode();self.send_response(200);self.end_headers();self.wfile.write(body)
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                events=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':'call1','function':{'name':'testagent','arguments':'{"action":'}}]},'finish_reason':None}]},
                        {'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':'"finish"}'}}]},'finish_reason':'tool_calls'}]}]
                for event in events:self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
        server=ThreadingHTTPServer(('127.0.0.1',0),API);threading.Thread(target=server.serve_forever,daemon=True).start()
        profile={'kind':'openai','config':{'base_url':f'http://127.0.0.1:{server.server_port}/v1'}}
        backend=OpenAIBackend(profile,'',threading.Event(),lambda *_:None)
        try:
            self.assertEqual(backend.models(),['fixture-model'])
            msg=backend.completion([{'role':'user','content':'test'}],'fixture-model')
            self.assertEqual(json.loads(msg['tool_calls'][0]['function']['arguments']),{'action':'finish'})
        finally:backend.close();server.shutdown();server.server_close()

    def test_mcp_stdio_uses_utf8_with_non_utf8_process_default(self):
        env=os.environ.copy()
        env['PYTHONIOENCODING']='ascii'
        env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
        requests=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':'中文请求','method':'ping'}]
        wire=''.join(json.dumps(request,ensure_ascii=False)+'\n' for request in requests).encode('utf-8')
        result=subprocess.run([sys.executable,'-m','testagent.bridge'],input=wire,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
        replies=[json.loads(line) for line in result.stdout.decode('utf-8').splitlines()]
        self.assertEqual(len(replies),2,replies)
        self.assertIn('result',replies[0],replies[0])
        self.assertEqual(replies[0]['result']['tools'][0]['name'],'testagent')
        self.assertEqual(replies[1],{'jsonrpc':'2.0','id':'中文请求','result':{}})

    def test_acp_mcp_bridge_complete_task(self):
        fixture=Path(__file__).parent/'fixtures'/'fake_acp.py'
        profile=self.profile('nga',{'command':sys.executable,'args':[str(fixture.resolve())]})
        runtime=Runtime(self.catalog);server=make_server(self.core,0,self.catalog,runtime)
        threading.Thread(target=server.serve_forever,daemon=True).start();runtime.start(server.server_port)
        try:
            models=runtime.models(profile)
            self.assertEqual(models['models'],['fixture-model'])
            task=self.task(profile)
            until(lambda:self.core.task(task)['status'] in ('failed','succeeded'),10)
            self.assertEqual(self.core.task(task)['status'],'succeeded',self.core.events(task))
        finally:runtime.close();server.shutdown();server.server_close();until(lambda:not runtime.runs)

    @unittest.skipIf(os.name=='nt','Local POSIX command fixture')
    def test_openai_to_ssh_task_end_to_end(self):
        fixture,executor,_=self.ssh_fixture()
        # The helper created a reserved task; release it before creating the runtime task.
        self.core.fail(executor.task,'fixture preparation')
        self.catalog.release_scene(executor.task)
        executor.close()
        class API(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                results=[m for m in body['messages'] if m['role']=='tool']
                if len(results)==0:arguments={'action':'exec','role':'controller','command':'printf READY'}
                else:arguments={'action':'finish','summary':'Verified SSH execution'}
                response={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'call'+str(len(results)), 'type':'function', 'function':{'name':'testagent','arguments':json.dumps(arguments)}}]}}]}
                raw=json.dumps(response).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(raw)
        api=ThreadingHTTPServer(('127.0.0.1',0),API);threading.Thread(target=api.serve_forever,daemon=True).start()
        runtime=Runtime(self.catalog)
        try:
            profile=self.profile(config={'base_url':f'http://127.0.0.1:{api.server_port}/v1'})
            runtime.start(0)
            task=self.task(profile)
            until(lambda:self.core.task(task)['status'] in ('succeeded','failed'),10)
            self.assertEqual(self.core.task(task)['status'],'succeeded',self.core.events(task))
            outputs=[e['payload']['output'] for e in self.core.events(task) if e['kind']=='tool.finished']
            self.assertEqual(outputs[0]['stdout'],'READY')
        finally:
            runtime.close();until(lambda:not runtime.runs)
            api.shutdown();api.server_close();fixture.close()

    def test_recovery_reserves_devices_and_requires_verification(self):
        package=example_package();package['version']='with-recovery'
        contract=json.loads(package['files']['contract.json']);contract['recovery']='Restore original file from backup then verify.'
        package['files']['contract.json']=json.dumps(contract);self.core.import_skill(package)
        task=self.core.create_task('restore-test',self.env,package['id'],package['version'],self.profile(),'fixture-model')
        self.core.claim(task);self.core.fail(task,'failure')
        runtime=Runtime(self.catalog);ident=runtime.propose_recovery(task)
        until(lambda:runtime.recovery_list(task)[0]['status']=='proposed')
        child=runtime.approve_recovery(ident)
        self.assertFalse(self.core.claim(self.task()))
        self.assertTrue(self.core.claim(child))
        with self.assertRaises(DomainError):self.core.finish(child)
        self.assertEqual(self.core.task(task)['status'],'failed')

    def test_recovery_authorization_does_not_change_parent_result(self):
        # Skill-defined proposal avoids model access, then simulate an explicitly authorized recovery.
        task=self.core.create_task('simulation',self.env,'diagnostic-demo','1.0.0')
        self.core.claim(task);self.core.fail(task,'fixture')
        runtime=Runtime(self.catalog)
        ident=runtime.propose_recovery(task)
        until(lambda:runtime.recovery_list(task)[0]['status']=='proposed')
        self.assertEqual(self.core.task(task)['status'],'failed')
        child=runtime.approve_recovery(ident)
        self.assertEqual(self.core.task(task)['status'],'failed')
        self.assertEqual(self.core.task(task)['scene'],'restored')
        self.assertEqual(self.core.task(child)['status'],'succeeded')
        with self.assertRaises(DomainError):runtime.approve_recovery(ident)

    def test_update_manifest_rejects_unlisted_files_and_tampering(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('update-manifest.json',json.dumps({'product':'pangea-testagent','schema_version':1,'version':'1.0.3','files':{}}))
            archive.writestr('app/PangeaTestagent.exe',b'tampered')
        with self.assertRaises(DomainError):Updates(self.catalog).inspect(stream.getvalue())

    def test_tool_failure_blocks_acp_retry(self):
        task=self.task();self.core.claim(task)
        cancelled=threading.Event();gateway=Gateway(Runtime(self.catalog),task,cancelled)
        with self.assertRaises(DomainError):gateway({'action':'exec','role':'unbound','command':'true'})
        self.assertEqual(self.core.task(task)['status'],'failed')
        self.assertTrue(cancelled.is_set())
        with self.assertRaises(Cancelled):gateway({'action':'finish'})

    def test_tool_output_redacts_credentials_before_model_receives_it(self):
        task=self.task();self.core.claim(task)
        gateway=Gateway(Runtime(self.catalog),task,threading.Event())
        file=self.catalog.add_file(task,'fixture.txt',b'password=fixture-secret')
        result=gateway({'action':'file_read','file_id':file})
        self.assertNotIn('fixture-secret',result['text'])
        self.assertIn('[已隐藏凭据]',result['text'])

    def test_scene_release_waits_for_executor_cleanup(self):
        task=self.task();self.core.claim(task);self.core.fail(task,'fixture')
        runtime=Runtime(self.catalog);runtime.runs[task]={'cancel':threading.Event()}
        with self.assertRaises(DomainError):runtime.release_scene(task)
        with self.assertRaises(DomainError):runtime.propose_recovery(task)
        runtime.runs.clear();runtime.release_scene(task)

    def test_expected_cli_disconnect_requires_later_verification(self):
        fixture,executor,_=self.ssh_fixture()
        try:
            opened=executor.shell_open(self.device,3,'device>')
            result=executor.shell_send(self.device,opened['session_id'],'reboot',None,3,True)
            self.assertEqual(result['completion'],'expected_disconnect')
            self.assertFalse(result['reboot_verified'])
        finally:executor.close();fixture.close()

    def test_embedded_update_manifest_validates_and_stages(self):
        import hashlib
        files={'app/runtime/pythonw.exe':b'fixture','app/runtime/python.exe':b'fixture','app/entry.py':b'pass','app/portable.json':b'{}'}
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            for name,body in files.items():archive.writestr(name,body)
            archive.writestr('update-manifest.json',json.dumps({'product':'pangea-testagent','schema_version':1,'version':'1.0.3','files':{name:hashlib.sha256(body).hexdigest() for name,body in files.items()}}))
        result=Updates(self.catalog).inspect(stream.getvalue())
        self.assertEqual(result['version'],'1.0.3')
        self.assertEqual((Path(result['stage'])/'app/entry.py').read_bytes(),b'pass')

    def test_update_package_type_errors_and_pending_reset(self):
        updater=Updates(self.catalog)
        for name,message in [('pangea-testagent-1.0.2-windows-x64-update.zip','外层 ZIP'),
                             ('app/portable.json','完整运行包没有升级清单'),
                             ('source.py','缺少 update-manifest.json')]:
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as archive:archive.writestr(name,b'fixture')
            updater.pending={'version':'stale'}
            with self.assertRaisesRegex(DomainError,message):updater.inspect(stream.getvalue())
            self.assertIsNone(updater.pending)
        with self.assertRaisesRegex(DomainError,'有效的 testagent ZIP'):updater.inspect(b'invalid')

    def test_portable_update_stages_only_verified_app_files(self):
        import hashlib
        files={'app/runtime/pythonw.exe':b'fixture','app/runtime/python.exe':b'fixture',
               'app/entry.py':b'pass','app/portable.json':b'{}',
               'Start-Testagent.cmd':b'launcher','README.md':b'usage'}
        def package(tamper=False):
            stream=io.BytesIO()
            manifest={'product':'pangea-testagent','schema_version':1,'version':'1.0.3',
                      'files':{name:hashlib.sha256(body).hexdigest() for name,body in files.items()}}
            with zipfile.ZipFile(stream,'w') as archive:
                for name,body in files.items():archive.writestr(name,b'changed' if tamper and name=='README.md' else body)
                archive.writestr('update-manifest.json',json.dumps(manifest))
            return stream.getvalue()
        updater=Updates(self.catalog)
        result=updater.inspect(package())
        self.assertEqual((Path(result['stage'])/'app/entry.py').read_bytes(),b'pass')
        self.assertFalse((Path(result['stage'])/'Start-Testagent.cmd').exists())
        with self.assertRaisesRegex(DomainError,'升级文件校验失败'):updater.inspect(package(True))
        self.assertIsNone(updater.pending)

    def ssh_fixture(self):
        remote=self.root/'remote';remote.mkdir()
        fixture=SSHFixture(remote)
        self.catalog.save_device({'id':self.device,'name':'fixture','address':'127.0.0.1','username':'tester','port':fixture.port})
        task=self.task();self.core.claim(task)
        executor=SSHExecutor(self.catalog,task,threading.Event(),lambda *_:None)
        return fixture,executor,remote

    def test_real_ssh_session_and_sftp(self):
        fixture,executor,remote=self.ssh_fixture()
        try:
            opened=executor.shell_open(self.device,3,'device>')
            result=executor.shell_send(self.device,opened['session_id'],'diagnose','diagnose>',3)
            self.assertEqual(result['completion'],'prompt_match')
            result=executor.shell_send(self.device,opened['session_id'],'show test-config','diagnose>',3)
            self.assertIn('flag=enabled',result['output'])
            (remote/'config').write_text('before')
            result=executor.write_file(self.device,'/config',b'after')
            self.assertEqual(executor.read_file(self.device,'/config'),b'after')
            self.assertTrue(result['backup_file_id'])
            self.assertTrue(self.catalog.state()['devices'][0]['fingerprint'].startswith('SHA256:'))
        finally:executor.close();fixture.close()

    @unittest.skipIf(os.name=='nt','POSIX process groups require the Linux integration fixture')
    def test_real_ssh_exec_and_force_termination(self):
        fixture,executor,_=self.ssh_fixture()
        errors=[]
        try:
            result=executor.exec(self.device,'printf testagent-ok',5)
            self.assertEqual(result['stdout'],'testagent-ok')
            def run():
                try:executor.exec(self.device,'sleep 60 & wait',90)
                except Exception as exc:errors.append(exc)
            thread=threading.Thread(target=run);thread.start()
            until(lambda:len(executor.operations)>0)
            time.sleep(.2)
            executor.cancelled.set();result=executor.stop();thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(result['processes'])
            self.assertEqual(result['processes'][0]['result'],'stopped')
            self.assertTrue(result['confirmed'])
            # Unknown/active must never be reported as a confirmed stop.
            if result['processes'][0]['result']!='stopped':self.assertFalse(result['confirmed'])
        finally:executor.close();fixture.close()

if __name__=='__main__':unittest.main()
