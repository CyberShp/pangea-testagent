"""Exercise released versions through HTTP import and PowerShell replacement on Windows."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import Request, urlopen

root=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--baseline',default='1.0.2',choices=['1.0.2','1.0.3','1.1.0','1.1.1'])
args=parser.parse_args()
import sys
sys.path.insert(0,str(root/'src'))
from testagent.paths import VERSION
work=root/'build'/('升级 测试 '+args.baseline)
app=work/'install'/'app'
data=work/'data'
shutil.copytree(root/'dist/portable/app',app)
old=root/('baseline-'+args.baseline)
shutil.rmtree(app/'src')
shutil.copytree(old/'src',app/'src')
for name in ('web','skills','examples'):
    shutil.rmtree(app/name)
    if (old/name).exists():shutil.copytree(old/name,app/name)
shutil.copy2(old/'scripts/apply-update.ps1',app/'apply-update.ps1')
meta=json.loads((app/'portable.json').read_text());meta['version']=args.baseline
(app/'portable.json').write_text(json.dumps(meta))
data.mkdir(parents=True)
(data/'preserve.txt').write_text('user data')
# Seed with the old application's own classes before starting it.
seed=work/'seed.py'
seed.write_text('''import json, sys
from pathlib import Path
from testagent.core import Core
from testagent.catalog import Catalog
from testagent.server import example_package
root=Path(sys.argv[1]); core=Core(root/'testagent.sqlite3'); catalog=Catalog(core,root)
device=catalog.save_device({'name':'保留设备','address':'sim://upgrade','username':'tester','password':'upgrade-fixture-secret'})
env=catalog.save_environment({'name':'保留环境','policy':'confirm','roles':{'controller':device}})
core.import_skill(example_package())
task=core.create_task('保留历史任务',env,'diagnostic-demo','1.0.0')
core.stop_simulation(task)
catalog.add_file(task,'evidence.txt',b'preserved evidence','output')
(root/'expected.json').write_text(json.dumps({'device':device,'environment':env,'task':task,'snapshot':core.task(task)['snapshot']}))
core.db.close()
''',encoding='utf-8')
env=dict(os.environ,TESTAGENT_DATA_DIR=str(data))
subprocess.run([str(app/'runtime/python.exe'),str(seed),str(data)],env=env,check=True)
expected=json.loads((data/'expected.json').read_text())
log=(work/'startup.log').open('wb')
process=subprocess.Popen([str(app/'runtime/python.exe'),str(app/'entry.py'),'--serve','--port','18766'],env=env,stdout=log,stderr=log)
base='http://127.0.0.1:18766'
def get(path):
    with urlopen(base+path,timeout=2) as response:return json.load(response)
def wait_version(version):
    for _ in range(120):
        try:
            if get('/api/health')['version']==version:return
        except Exception:pass
        time.sleep(1)
    raise AssertionError('Version not ready: '+version)
try:
    wait_version(args.baseline)
    token=get('/api/state')['token']
    def post(path,body):
        with urlopen(Request(base+path,data=body,headers={'X-Testagent-Token':token}),timeout=60) as response:return json.load(response)
    # Same root structure produced by upload-artifact, with no nested ZIP.
    artifact=shutil.make_archive(str(work/'downloaded-patch'),'zip',root/'dist/patch-artifact')
    assert post('/api/updates/import',Path(artifact).read_bytes())['result']['version']==VERSION
    post('/api/updates/apply',b'{}')
    wait_version(VERSION)
    for _ in range(20):
        if (data/'updates/update.log').exists() and 'update succeeded' in (data/'updates/update.log').read_text(encoding='utf-8-sig',errors='replace'):break
        time.sleep(1)
    else:raise AssertionError('Upgrade success log missing')
    assert (data/'preserve.txt').read_text()=='user data'
    assert (app/'apply-update.ps1').read_bytes().startswith(b'\xef\xbb\xbf')
    state=get('/api/state')
    assert any(d['id']==expected['device'] and d['has_password'] for d in state['devices'])
    assert any(e['id']==expected['environment'] for e in state['environments'])
    assert any(s['id']=='diagnostic-demo' and s['version']=='1.0.0' for s in state['skills'])
    assert any(t['id']==expected['task'] and t['status']=='stopped' for t in state['tasks'])
    # Verify persisted snapshot, artifact bytes, and decryptability using upgraded code.
    verify=work/'verify.py'
    verify.write_text('''import json, sys
from pathlib import Path
from testagent.core import Core
from testagent.catalog import Catalog
root=Path(sys.argv[1]); expected=json.loads((root/'expected.json').read_text())
core=Core(root/'testagent.sqlite3'); catalog=Catalog(core,root)
assert core.task(expected['task'])['snapshot']==expected['snapshot']
assert any(p.read_bytes()==b'preserved evidence' for p in (root/'tasks'/expected['task']).rglob('*') if p.is_file())
assert catalog.vault.get('device-'+expected['device'])=='upgrade-fixture-secret'
core.db.close()
''',encoding='utf-8')
    subprocess.run([str(app/'runtime/python.exe'),str(verify),str(data)],env=env,check=True)
    token=state['token']
    # Confirm the imported patch actually exposes the corrected HTTP import path.
    import io, zipfile
    raw_tool=io.BytesIO()
    with zipfile.ZipFile(raw_tool,'w') as archive:
        archive.writestr('vdbench50407/vdbench',b'#!/bin/sh\nexit 0\n')
        archive.writestr('vdbench50407/vdbench.jar',b'import-fixture-only')
    imported=post('/api/tools/import?version=5.04.07&architecture=x86_64',raw_tool.getvalue())['result']
    assert imported['driver']=='vdbench' and imported['version']=='5.04.07'
    assert any(t['id']==imported['id'] for t in get('/api/tools'))
    assert any(s['id']=='nic-bandwidth' for s in state['scenarios'])
    package=get('/api/scenarios/detail?id=nic-bandwidth')
    assert 'scripts/inspect.sh' in package['files']
    assert '整卡双端口' in package['files']['contract.json']
    print(f'PASS: {args.baseline} -> {VERSION}, device credentials, environments, Skill, task snapshot and artifact retained; built-in scenario available')
finally:
    # Terminate only processes executing this isolated test installation.
    escaped=str(work).replace("'","''")
    subprocess.run(['powershell.exe','-NoProfile','-Command',f"Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -like '{escaped}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"])
    log.close()
