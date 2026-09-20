import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

from testagent.load_plans import stages,compile_load
from testagent.metrics import parse_line
from testagent.tool_library import inspect
from testagent.paths import ROOT
from testagent.skills import validate
from testagent.packages import from_folder


class LoadTests(unittest.TestCase):
    def test_waveforms_and_iperf_total_rate(self):
        plan={'shape':'square','duration':10,'interval':3,'low':10,'high':100}
        self.assertEqual([(s['duration'],s['rate']) for s in stages(plan)],[(3,10),(3,100),(3,10),(1,100)])
        random={**plan,'shape':'random','seed':42}
        self.assertEqual(stages(random),stages(random))
        surge={**plan,'shape':'surge','surge_start':2,'surge_duration':2}
        self.assertEqual([(s['duration'],s['rate']) for s in stages(surge)][:3],[(2,10),(2,100),(3,10)])
        compiled=compile_load({'name':'test','driver':'iperf3','duration':10,'plan':plan,
            'iperf':{'bind':'192.0.2.1','peer':'192.0.2.2','port':5201,'parallel':2}},'/tmp/test')
        self.assertIn('-b 5',compiled['stages'][0]['command'])
        self.assertEqual(compiled['transition'],'restart_between_stages')

    def test_vdbench_configuration_and_write_confirmation(self):
        spec={'name':'disk','driver':'vdbench','duration':10,'vdbench':{'targets':['/dev/disk/by-id/test'],
            'read_pct':0,'seek_pct':100,'block_kib':4,'threads':8,'rate':1000}}
        with self.assertRaises(ValueError):compile_load(spec,'/tmp/test')
        spec['vdbench']['write_confirmed']=True
        compiled=compile_load(spec,'/tmp/test')
        self.assertIn('rdpct=0',next(iter(compiled['files'].values())))
        self.assertIn('elapsed=10',next(iter(compiled['files'].values())))

    def test_metric_parsers_do_not_invent_missing_values(self):
        line=json.dumps({'event':'interval','data':{'sum':{'bits_per_second':1e9,'start':1,'end':2}}})
        samples=parse_line(line,'iperf3',received=100)
        self.assertEqual([(s['name'],s['unit'],s['value']) for s in samples],[('bandwidth','bit/s',1e9)])
        omitted=json.dumps({'event':'interval','data':{'sum':{'omitted':True,'bits_per_second':1e9}}})
        self.assertEqual(parse_line(omitted,'iperf3'),[])
        self.assertEqual(parse_line('not json','jsonl'),[])
        data=parse_line('{"timestamp":100,"io":{"rate":123}}','jsonl',[{'name':'iops','unit':'iops','field':'io.rate'}])
        self.assertEqual(data[0]['value'],123)
        self.assertEqual(data[0]['clock'],'source')
        data=parse_line('12:00:01.123 1 1000 4.00 4096 100 0.250 0.250','vdbench')
        self.assertEqual(data[1]['value'],4*1024**2)
        self.assertEqual(data[2]['unit'],'ms')

    def test_offline_package_integrity_and_paths(self):
        def bundle(tampered=False,path='bin/tool'):
            data=b'payload';manifest={'name':'tool','version':'1','os':'linux','architecture':'aarch64',
                'driver':'custom','entrypoint':path,'files':{path:hashlib.sha256(data).hexdigest()}}
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w') as archive:
                archive.writestr(path,b'bad' if tampered else data);archive.writestr('tool.json',json.dumps(manifest))
            return output.getvalue()
        self.assertEqual(inspect(bundle())['architecture'],'aarch64')
        with self.assertRaises(ValueError):inspect(bundle(True))
        with self.assertRaises(ValueError):inspect(bundle(path='../escape'))

    def test_invalid_custom_contract_is_rejected(self):
        package=from_folder(ROOT/'skills/nic-bandwidth')
        contract=json.loads(package['files']['contract.json'])
        contract['workloads']=[{'id':'nas','role':'client','driver':'custom','recovery':'stop'}]
        package['files']['contract.json']=json.dumps(contract)
        self.assertEqual(validate(package)['mode'],'invalid')

    @unittest.skipUnless(sys.platform.startswith('linux'),'Linux /proc process identity test')
    def test_native_supervisor_completion_stop_and_identity(self):
        runner=ROOT/'src/testagent/remote/load_runner.py'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            def call(action):
                result=subprocess.run([sys.executable,str(runner),action,directory,'0'],capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
                return json.loads(result.stdout)
            config={'marker':'fixture-owned-marker','stages':[{'command':'sleep 30','duration':30,'rate':None}],'grace':1}
            (root/'config.json').write_text(json.dumps(config))
            result=call('start');self.assertEqual(result['state'],'running',result)
            try:
                result=call('stop')
                for _ in range(20):
                    if result.get('confirmed_exit'):break
                    time.sleep(.1);result=call('read')
                self.assertTrue(result['confirmed_exit'],result)
                self.assertEqual(result['state'],'stopped')
            finally:call('stop')


if __name__=='__main__':unittest.main()
