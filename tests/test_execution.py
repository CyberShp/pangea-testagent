import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
import zipfile

from testagent.core import Core, DomainError
from testagent.catalog import Catalog
from testagent.runtime import Runtime
from testagent.tools import Gateway
from testagent.execution import comparisons
from testagent.server import example_package
from testagent.server import make_server
from urllib.request import urlopen
from testagent.skills import validate
from fixtures.ssh_server import SSHFixture


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.core=Core(self.root/'db.sqlite3')
        self.catalog=Catalog(self.core,self.root/'data')
        self.device=self.catalog.save_device({'name':'交换机','address':'127.0.0.1','username':'tester','password':'fixture-secret'})
        self.env=self.catalog.save_environment({'name':'环境','roles':{'controller':self.device},'policy':'automatic'})
        self.package=example_package()
        self.contract={'schema_version':1,'roles':[{'id':'controller'}], 'steps':[], 'checks':[], 'critical_actions':[],
                       'comparisons':[{'id':'config','name':'设备配置','role':'controller','operation':{'action':'remote_read','path':'/config'}}]}
        self.package['files']['contract.json']=json.dumps(self.contract)
        self.core.import_skill(self.package)
        self.profile=self.catalog.save_profile({'name':'模型','kind':'openai','config':{'base_url':'http://127.0.0.1:1/v1'}})
        self.runtime=Runtime(self.catalog)

    def tearDown(self):
        self.core.db.close();self.temp.cleanup()

    def task(self, policy='automatic'):
        env=self.env if policy=='automatic' else self.catalog.save_environment({'name':'确认环境','roles':{'controller':self.device},'policy':'confirm'})
        task=self.core.create_task('配置任务',env,self.package['id'],self.package['version'],self.profile,'test')
        self.core.claim(task)
        return task

    def approve(self,task,operations):
        plan=self.core.propose_preview(task,'更新配置','配置文件发生变化','回读并对比',operations)
        self.core.approve_preview(task,plan)
        return plan

    def gateway(self,task):
        gateway=Gateway(self.runtime,task,threading.Event())
        gateway.executor=Mock()
        return gateway

    def test_both_policies_block_ssh_before_preview(self):
        for policy in ('automatic','confirm'):
            with self.subTest(policy=policy):
                task=self.task(policy);gateway=self.gateway(task)
                with self.assertRaisesRegex(DomainError,'必须确认'):
                    gateway({'action':'shell_open','role':'controller'})
                gateway.executor.shell_open.assert_not_called()
                self.catalog.release_scene(task)

    def test_preview_tool_waits_and_requires_explicit_approval(self):
        task=self.task();gateway=self.gateway(task);results=[]
        thread=threading.Thread(target=lambda:results.append(gateway({'action':'preview','summary':'查看配置','impact':'只读','verification':'回读','operations':[]})))
        thread.start()
        try:
            for _ in range(100):
                if self.core.previews(task):break
                time.sleep(.01)
            self.assertEqual(results,[])
            self.assertEqual(self.core.task(task)['status'],'waiting_user')
            plan=self.core.previews(task)[0]['id'];self.core.approve_preview(task,plan)
            thread.join(2)
            self.assertEqual(results,[{'preview_id':plan,'approved':True}])
            self.core.finish(task)
        finally:
            gateway.cancelled.set();thread.join(2)

    def test_exact_order_target_and_replay_are_enforced(self):
        task=self.task();gateway=self.gateway(task)
        read={'action':'remote_read','role':'controller','path':'/config'}
        write={'action':'remote_write','role':'controller','path':'/config','text':'new'}
        self.approve(task,[read,write])
        with self.assertRaisesRegex(DomainError,'不一致'):
            self.core.request_operation(task,self.device,json.dumps(write))
        with self.assertRaisesRegex(DomainError,'不一致'):
            self.core.request_operation(task,'other-device',json.dumps(read))
        gateway.executor.read_file.return_value=b'old'
        first=gateway(read)
        with self.assertRaises(DomainError):self.core.consume_operation(task,first['operation_id'])
        with self.assertRaisesRegex(DomainError,'未执行'):
            self.core.finish(task)
        gateway.executor.write_file.return_value={'verified':True}
        gateway(write)
        self.assertEqual(self.core.previews(task)[0]['position'],2)
        with self.assertRaisesRegex(DomainError,'超出'):
            self.core.request_operation(task,self.device,json.dumps(write))
        self.core.finish(task)

    def test_user_message_invalidates_confirmed_scope(self):
        task=self.task();operation={'action':'remote_read','role':'controller','path':'/config'}
        plan=self.approve(task,[operation])
        self.catalog.message(task,'改为只检查另一个配置')
        self.assertEqual(self.core.previews(task)[0]['status'],'superseded')
        with self.assertRaises(DomainError):self.core.request_operation(task,self.device,json.dumps(operation))
        self.assertEqual(self.catalog.drain_messages(task),['改为只检查另一个配置'])
        self.approve(task,[]);self.core.finish(task)

    def test_pending_preview_new_message_returns_to_agent(self):
        task=self.task();gateway=self.gateway(task);results=[]
        thread=threading.Thread(target=lambda:results.append(gateway({'action':'preview','summary':'检查','impact':'只读','verification':'回读','operations':[]})))
        thread.start()
        try:
            for _ in range(100):
                if self.core.previews(task):break
                time.sleep(.01)
            self.catalog.message(task,'先等一下，修改计划')
            thread.join(2)
            self.assertFalse(results[0]['approved'])
            with self.assertRaises(DomainError):self.core.approve_preview(task,self.core.previews(task)[0]['id'])
        finally:gateway.cancelled.set();thread.join(2)

    def test_stopped_or_other_task_preview_cannot_be_approved(self):
        task=self.task();plan=self.core.propose_preview(task,'检查','只读','回读',[])
        with self.assertRaises(DomainError):self.core.approve_preview('missing',plan)
        self.core.fail(task,'用户拒绝')
        with self.assertRaises(DomainError):self.core.approve_preview(task,plan)
        self.assertEqual(self.core.previews(task)[0]['position'],0)

    def test_real_ssh_capture_diff_and_report_evidence(self):
        remote=self.root/'remote';remote.mkdir();(remote/'config').write_text('vlan 10\n')
        fixture=SSHFixture(remote)
        self.catalog.save_device({'id':self.device,'name':'交换机','address':'127.0.0.1','port':fixture.port,'username':'tester'})
        task=self.task();gateway=Gateway(self.runtime,task,threading.Event())
        before={'action':'remote_read','role':'controller','path':'/config','capture_id':'config','capture_phase':'before'}
        change={'action':'remote_write','role':'controller','path':'/config','text':'vlan 20\n'}
        after={**before,'capture_phase':'after'}
        self.approve(task,[before,change,after])
        try:
            first=gateway(before);self.assertFalse(comparisons(self.core,task)[0]['available'])
            gateway(change);last=gateway(after);self.core.finish(task)
            comparison=comparisons(self.core,task)[0]
            self.assertTrue(comparison['available']);self.assertTrue(comparison['changed'])
            self.assertIn('-vlan 10',comparison['diff']);self.assertIn('+vlan 20',comparison['diff'])
            self.assertEqual(comparison['before']['operation_id'],first['operation_id'])
            self.assertEqual(comparison['after']['operation_id'],last['operation_id'])
            with zipfile.ZipFile(io.BytesIO(self.catalog.report(task))) as archive:
                self.assertEqual(archive.read('comparisons/1/before.txt'),b'vlan 10\n')
                self.assertIn(b'+vlan 20',archive.read('comparisons/1/change.diff'))
                self.assertEqual(json.loads(archive.read('previews.json'))[0]['position'],3)
        finally:gateway.executor.close();fixture.close()

    def test_capture_command_must_match_skill_and_cannot_be_overwritten(self):
        task=self.task();gateway=self.gateway(task)
        op={'action':'remote_read','role':'controller','path':'/config','capture_id':'config','capture_phase':'before'}
        with self.assertRaisesRegex(DomainError,'采集必须'):
            self.core.propose_preview(task,'检查','只读','回读',[{**op,'path':'/other'}])
        self.approve(task,[op,op]);gateway.executor.read_file.return_value=b'password=fixture-secret\n'
        gateway(op)
        self.assertNotIn('fixture-secret',comparisons(self.core,task)[0]['before']['text'])
        with self.assertRaisesRegex(DomainError,'不能覆盖'):gateway(op)
        self.assertEqual(gateway.executor.read_file.call_count,1)

    def test_old_skill_valid_and_invalid_topology_rejected(self):
        self.assertEqual(validate(example_package())['mode'],'structured')
        self.contract['topology']=[{'from':'controller','to':'missing'}]
        self.package['files']['contract.json']=json.dumps(self.contract)
        self.assertEqual(validate(self.package)['mode'],'invalid')
        self.contract['topology']=[]
        self.contract['comparisons'][0]['operation']={'action':'shell_send','text':'display current-configuration'}
        self.package['files']['contract.json']=json.dumps(self.contract)
        self.assertEqual(validate(self.package)['mode'],'invalid')

    def test_preview_persists_restart_and_cannot_resume(self):
        task=self.task();self.approve(task,[])
        self.core.db.close();self.core=Core(self.root/'db.sqlite3')
        self.core.recover_startup()
        self.assertEqual(self.core.task(task)['status'],'failed')
        self.assertEqual(self.core.previews(task)[0]['data']['summary'],'更新配置')
        with self.assertRaises(DomainError):self.core.finish(task)

    def test_preview_rejection_and_stale_rejection(self):
        task=self.task();plan=self.core.propose_preview(task,'检查','只读','回读',[])
        self.catalog.message(task,'换一个检查')
        self.catalog.drain_messages(task)
        current=self.core.propose_preview(task,'新检查','只读','回读',[])
        with self.assertRaises(DomainError):self.core.reject_preview(task,plan)
        self.assertEqual(self.core.task(task)['status'],'waiting_user')
        self.core.reject_preview(task,current)
        self.assertEqual(self.core.task(task)['status'],'failed')
        self.assertEqual(self.core.previews(task)[-1]['status'],'rejected')
        self.assertFalse(any(e['kind']=='tool.started' for e in self.core.events(task)))

    def test_event_stream_notifies_new_events_and_reconnect_cursor(self):
        task=self.task();cursor=self.core.events(task)[-1]['seq']
        server=make_server(self.core,0,self.catalog,self.runtime)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/stream?task_id={task}&after={cursor}',timeout=3) as response:
                self.assertIn('text/event-stream',response.headers['Content-Type'])
                with self.core.tx():self.core.emit(task,'agent.chunk',{'text':'实时更新'})
                lines=[]
                for _ in range(6):
                    line=response.readline().decode();lines.append(line)
                    if line.startswith('data:'):break
                self.assertIn(f'data: {cursor+1}\n',lines)
        finally:
            self.runtime.closed.set();server.shutdown();server.server_close();thread.join()
