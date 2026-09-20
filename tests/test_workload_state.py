import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from testagent.core import Core,DomainError
from testagent.catalog import Catalog
from testagent.workloads import Workloads
from testagent.server import example_package
from testagent.execution import validate_operation


class WorkloadStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.core=Core(Path(self.temp.name)/'db.sqlite3');self.addCleanup(self.core.db.close)
        self.catalog=Catalog(self.core,Path(self.temp.name)/'data')
        device=self.catalog.save_device({'name':'node','address':'sim://node'})
        env=self.catalog.save_environment({'name':'env','roles':{'controller':device},'policy':'automatic'})
        package=example_package();contract=json.loads(package['files']['contract.json'])
        contract.update(steps=[],checks=[],critical_actions=[],workloads=[{'id':'test','role':'controller','driver':'custom',
            'parser':'jsonl','metrics':[{'name':'iops','unit':'iops','field':'iops'}],
            'start':'start','status':'status','stop':'stop','recovery':'stop and verify'}])
        package['files']['contract.json']=json.dumps(contract);self.core.import_skill(package)
        self.task=self.core.create_task('test',env,package['id'],package['version'])
        self.core.claim(self.task)
        preview=self.core.propose_preview(self.task,'read','none','observe',[]);self.core.approve_preview(self.task,preview)
        self.loads=Workloads(self.catalog)
        self.spec={'name':'round1','driver':'custom','duration':5,'command':'test','parser':'jsonl',
            'metrics':[{'name':'iops','unit':'iops','field':'iops'}]}
        with self.core.tx():self.core.db.execute('INSERT INTO workloads VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('load1',self.task,'controller',device,'round1',json.dumps(self.spec),'/tmp/test-fixture','running','{}',0,time.time(),'operation'))

    def test_active_load_blocks_finish_and_scene_release(self):
        with self.assertRaisesRegex(DomainError,'负载'):self.core.finish(self.task)
        self.core.fail(self.task,'fixture')
        with self.assertRaisesRegex(DomainError,'负载'):self.catalog.release_scene(self.task)

    def test_samples_persist_and_report_contains_original_evidence(self):
        text='{"iops":123}\n'
        self.loads.store('load1',{'state':'running','text':text,'offset':len(text)})
        jobs=self.loads.list(self.task)
        self.assertEqual(jobs[0]['samples'][0]['value'],123)
        self.assertEqual(jobs[0]['offset'],len(text))
        self.loads.store('load1',{'state':'succeeded','confirmed_exit':True,'offset':len(text),'text':''})
        self.assertTrue(self.loads.settled(self.task))
        self.core.finish(self.task)
        import io,zipfile
        with zipfile.ZipFile(io.BytesIO(self.catalog.report(self.task))) as archive:
            self.assertEqual(archive.read('workloads/round1.log').decode(),text)
            self.assertEqual(json.loads(archive.read('workloads/round1.json'))['samples'][0]['value'],123)

    def test_remote_disconnect_marks_unknown_without_losing_samples(self):
        with patch.object(self.loads,'remote',side_effect=OSError('offline')):
            with self.assertRaises(OSError):self.loads.refresh(self.task,'round1')
        self.assertEqual(self.loads.list(self.task)[0]['state'],'unknown')
        self.assertFalse(self.loads.settled(self.task))

    def test_confirmed_exit_waits_until_log_is_drained(self):
        self.loads.store('load1',{'state':'succeeded','confirmed_exit':True})
        self.assertFalse(self.loads.settled(self.task))
        self.loads.store('load1',{'state':'succeeded','confirmed_exit':True,'offset':10,'more':True})
        self.assertFalse(self.loads.settled(self.task))
        self.loads.store('load1',{'state':'succeeded','confirmed_exit':True,'offset':20,'more':False})
        self.assertTrue(self.loads.settled(self.task))

    def test_load_operation_uses_preview_scope(self):
        operation={'action':'load_start','role':'controller','load':self.spec}
        validate_operation(operation,self.core.task(self.task)['snapshot'])
        with self.assertRaises(DomainError):self.core.request_operation(self.task,'other',json.dumps(operation))


if __name__=='__main__':unittest.main()
