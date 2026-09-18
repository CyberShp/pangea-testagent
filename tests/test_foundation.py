import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from testagent.backends import launch_spec
from testagent.core import Core, DomainError
from testagent.server import example_package, make_server
from testagent.simulation import DiagnosticSession, Simulator
from testagent.skills import validate


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.core = Core(Path(self.tmp.name) / 'db.sqlite3')
        self.device = self.core.device('控制器 A', 'sim://a')
        self.environment = self.core.environment('环境 A', {'controller': self.device})
        self.core.import_skill(example_package())

    def tearDown(self):
        self.core.db.close()
        self.tmp.cleanup()

    def task(self, environment=None):
        return self.core.create_task('诊断检查', environment or self.environment, 'diagnostic-demo', '1.0.0')

    def test_duplicate_device_alias_cannot_bypass_reservation(self):
        with self.assertRaises(DomainError):
            self.core.device('别名', 'SIM://A')

    def test_atomic_claim_under_concurrency_and_distinct_devices(self):
        tasks = [self.task() for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(self.core.claim, tasks)), 1)
        other = self.core.device('控制器 B', 'sim://b')
        env = self.core.environment('环境 B', {'controller': other})
        self.assertTrue(self.core.claim(self.task(env)))

    def test_waiting_retains_reservation_and_approval_is_one_use(self):
        task = self.task()
        self.core.claim(task)
        operation = self.core.request_operation(task, self.device, 'diagnose', 'enter-diagnostic')
        self.assertEqual(self.core.task(task)['status'], 'waiting_user')
        self.assertFalse(self.core.claim(self.task()))
        with self.assertRaises(DomainError):
            self.core.consume_operation(task, operation)
        self.core.approve(task, operation)
        self.assertEqual(self.core.consume_operation(task, operation)['command'], 'diagnose')
        with self.assertRaises(DomainError):
            self.core.consume_operation(task, operation)

    def test_cross_task_approval_and_device_rejected(self):
        task = self.task()
        self.core.claim(task)
        with self.assertRaises(DomainError):
            self.core.request_operation(task, 'unbound', 'diagnose')
        operation = self.core.request_operation(task, self.device, 'diagnose', 'enter-diagnostic')
        with self.assertRaises(DomainError):
            self.core.approve(self.task(), operation)

    def test_required_step_and_check_gate(self):
        task = self.task()
        self.core.claim(task)
        with self.assertRaises(DomainError):
            self.core.finish(task)
        with self.assertRaises(DomainError):
            self.core.check(task, 'flag-present', True, 'invented-evidence')

    def test_simulation_full_chain_and_evidence(self):
        task = self.task()
        self.core.claim(task)
        sim = Simulator(self.core)
        sim.prepare(task)
        request = self.core.events(task)[-1]['payload']['id']
        sim.execute(task)
        self.assertEqual(self.core.task(task)['status'], 'waiting_user')
        self.core.approve(task, request)
        sim.execute(task)
        self.assertEqual(self.core.task(task)['status'], 'succeeded')
        events = self.core.events(task)
        output = next(e for e in events if e['kind']=='tool.finished')
        check = next(e for e in events if e['kind']=='check.result')
        self.assertIn('flag=enabled', output['payload']['output'])
        self.assertEqual(check['payload']['evidence'], output['payload']['id'])
        self.assertTrue(self.core.claim(self.task()))

    def test_background_scheduler_without_browser(self):
        environment = self.core.environment('自动模拟环境', {'controller': self.device}, 'automatic')
        first, second = self.task(environment), self.task(environment)
        simulator = Simulator(self.core)
        simulator.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if all(self.core.task(t)['status'] == 'succeeded' for t in (first, second)):
                    break
                time.sleep(.02)
            self.assertEqual(self.core.task(first)['status'], 'succeeded')
            self.assertEqual(self.core.task(second)['status'], 'succeeded')
        finally:
            simulator.close()

    def test_failure_never_retries_and_does_not_release_unknown_scene(self):
        task = self.task()
        self.core.claim(task)
        self.core.fail(task, '连接中断')
        self.assertFalse(self.core.claim(task))
        self.assertFalse(self.core.claim(self.task()))

    def test_restart_preserves_reservation_marks_failed(self):
        task = self.task()
        self.core.claim(task)
        path = Path(self.tmp.name) / 'db.sqlite3'
        self.core.db.close()
        self.core = Core(path)
        self.core.recover_startup()
        self.assertEqual(self.core.task(task)['status'], 'failed')
        self.assertEqual(self.core.task(task)['scene'], 'unknown')
        self.assertFalse(self.core.claim(self.task()))

    def test_skill_snapshot_immutable_and_overwrite_rejected(self):
        task = self.task()
        package = example_package()
        with self.assertRaises(DomainError):
            self.core.import_skill(package)
        package['version']='2.0.0'
        package['files']['SKILL.md']='新说明'
        self.core.import_skill(package)
        self.assertEqual(self.core.task(task)['snapshot']['skill']['version'], '1.0.0')

    def test_event_cursor_and_task_isolation(self):
        first, second = self.task(), self.task()
        cursor = self.core.events(first)[-1]['seq']
        self.core.claim(first)
        self.assertTrue(all(e['task_id']==first and e['seq']>cursor for e in self.core.events(first,cursor)))
        self.assertEqual(len(self.core.events(second)),1)

    def test_stop_invalidates_pending_approval(self):
        task=self.task()
        self.core.claim(task)
        operation=self.core.request_operation(task,self.device,'diagnose','enter-diagnostic')
        self.core.stop_simulation(task)
        with self.assertRaises(DomainError):
            self.core.approve(task,operation)

    def test_contract_modes_and_references(self):
        package=example_package()
        self.assertEqual(validate(package)['mode'],'structured')
        package['files']['contract.json']='{'
        self.assertEqual(validate(package)['mode'],'invalid')
        del package['files']['contract.json']
        self.assertEqual(validate(package)['mode'],'ordinary')
        package['files']['../escape']='unsafe'
        self.assertEqual(validate(package)['mode'],'invalid')

    def test_contract_duplicate_and_missing_script(self):
        package=example_package()
        contract=json.loads(package['files']['contract.json'])
        contract['steps']*=2
        contract['scripts']=['not-present.sh']
        package['files']['contract.json']=json.dumps(contract)
        result=validate(package)
        self.assertEqual(result['mode'],'invalid')
        self.assertGreaterEqual(len(result['errors']),2)

    def test_session_context_and_reconnect(self):
        session=DiagnosticSession()
        with self.assertRaises(DomainError):
            session.send('show test-config')
        session.send('diagnose')
        self.assertIn('flag=enabled',session.send('show test-config'))
        with self.assertRaises(DomainError):
            DiagnosticSession().send('show test-config')

    def test_windows_provider_launch_contract(self):
        spec=launch_spec('codeagent',platform='nt',env={})
        self.assertEqual(spec.env['CODEAGENT3_WINDOWS_SHELL_TYPE'],'powershell')
        self.assertEqual(launch_spec('nga').args,('acp',))
        self.assertIn('--print-logs',launch_spec('opencode').args)

    def test_loopback_http_and_csrf_boundary(self):
        server=make_server(self.core)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        root=f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(root+'/api/state') as response:
                state=json.load(response)
            body=json.dumps({'name':'B','address':'sim://b'}).encode()
            with self.assertRaises(HTTPError) as failed:
                urlopen(Request(root+'/api/devices',data=body,headers={'Content-Type':'application/json'}))
            self.assertEqual(failed.exception.code,403)
            headers={'Content-Type':'application/json','X-Testagent-Token':state['token']}
            with urlopen(Request(root+'/api/devices',data=body,headers=headers)) as response:
                self.assertEqual(response.status,200)
            headers['Origin']='https://untrusted.example'
            with self.assertRaises(HTTPError) as failed:
                urlopen(Request(root+'/api/devices',data=body,headers=headers))
            self.assertEqual(failed.exception.code,403)
            with urlopen(root+'/') as response:
                self.assertIn('测试环境工作台',response.read().decode())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__=='__main__':
    unittest.main()
