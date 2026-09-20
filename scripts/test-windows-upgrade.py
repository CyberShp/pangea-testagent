"""Exercise released 1.0.2 HTTP import + PowerShell replacement on Windows."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import Request, urlopen

root=Path(__file__).resolve().parents[1]
work=root/'build'/'升级 测试'
app=work/'install'/'app'
data=work/'data'
shutil.copytree(root/'dist/portable/app',app)
old=root/'baseline-1.0.2'
shutil.rmtree(app/'src')
shutil.copytree(old/'src',app/'src')
shutil.copy2(old/'scripts/apply-update.ps1',app/'apply-update.ps1')
meta=json.loads((app/'portable.json').read_text());meta['version']='1.0.2'
(app/'portable.json').write_text(json.dumps(meta))
data.mkdir(parents=True)
(data/'preserve.txt').write_text('user data')
env=dict(os.environ,TESTAGENT_DATA_DIR=str(data))
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
    wait_version('1.0.2')
    token=get('/api/state')['token']
    def post(path,body):
        with urlopen(Request(base+path,data=body,headers={'X-Testagent-Token':token}),timeout=60) as response:return json.load(response)
    # Same root structure produced by upload-artifact, with no nested ZIP.
    artifact=shutil.make_archive(str(work/'downloaded-patch'),'zip',root/'dist/patch-artifact')
    assert post('/api/updates/import',Path(artifact).read_bytes())['result']['version']=='1.0.3'
    post('/api/updates/apply',b'{}')
    wait_version('1.0.3')
    for _ in range(20):
        if (data/'updates/update.log').exists() and 'update succeeded' in (data/'updates/update.log').read_text(encoding='utf-8-sig',errors='replace'):break
        time.sleep(1)
    else:raise AssertionError('Upgrade success log missing')
    assert (data/'preserve.txt').read_text()=='user data'
    assert (app/'apply-update.ps1').read_bytes().startswith(b'\xef\xbb\xbf')
    print('PASS: 1.0.2 -> 1.0.3 direct downloaded patch import, real Windows replacement, data retained, BOM helper installed')
finally:
    # Terminate only processes executing this isolated test installation.
    escaped=str(work).replace("'","''")
    subprocess.run(['powershell.exe','-NoProfile','-Command',f"Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -like '{escaped}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"])
    log.close()
