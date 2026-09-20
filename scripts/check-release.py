"""Static release integrity checks; explicitly not a Windows execution test."""
import hashlib
import json
from pathlib import Path
import struct
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from testagent.paths import VERSION


def main():
    portable=ROOT/'dist'/f'pangea-testagent-{VERSION}-windows-x64-portable.zip'
    update=ROOT/'dist'/f'pangea-testagent-{VERSION}-windows-x64-update.zip'
    for file in (portable,update):
        expected=file.with_suffix('.zip.sha256').read_text().split()[0]
        assert hashlib.sha256(file.read_bytes()).hexdigest()==expected
        with zipfile.ZipFile(file) as archive:assert archive.testzip() is None
    from testagent.patching import rebuild_patch
    delta=ROOT/'dist'/f'pangea-testagent-{VERSION}-windows-x64-patch.zip'
    with zipfile.ZipFile(delta) as archive:
        assert not any(n.startswith('app/runtime/') for n in archive.namelist())
    rebuilt=rebuild_patch(delta.read_bytes(),ROOT/'dist/portable/app')
    import io
    with zipfile.ZipFile(io.BytesIO(rebuilt)) as actual,zipfile.ZipFile(update) as expected:
        assert set(actual.namelist())==set(expected.namelist())
        for name in actual.namelist():
            if name=='update-manifest.json':assert json.loads(actual.read(name))==json.loads(expected.read(name))
            else:assert actual.read(name)==expected.read(name),name
    from testagent.tool_library import inspect as inspect_tool
    bundles=[inspect_tool(p.read_bytes()) for p in (ROOT/'dist/portable/app/tool-bundles').glob('*.zip')]
    assert {b['architecture'] for b in bundles if b['driver']=='iperf3'}=={'x86_64','aarch64'}
    helper=(ROOT/'dist/portable/app/apply-update.ps1').read_bytes()
    assert helper.startswith(b'\xef\xbb\xbf'), 'Windows PowerShell requires UTF-8 BOM'
    if sys.platform=='win32':
        import subprocess
        subprocess.run(['powershell.exe','-NoProfile','-Command',
            "$e=$null;$t=$null;[System.Management.Automation.Language.Parser]::ParseFile('dist/portable/app/apply-update.ps1',[ref]$t,[ref]$e)|Out-Null;if($e.Count){$e|Out-String|Write-Error;exit 1}"],check=True)
    print('PASS: runtime-free patch rebuilds the complete update byte-for-byte per app file.')
    with zipfile.ZipFile(portable) as full,zipfile.ZipFile(update) as patch:
        names=set(full.namelist())
        for name in ('Start-Testagent.cmd','app/entry.py','app/runtime/python312.zip','app/runtime/python312.dll','app/runtime/LICENSE.txt','app/src/testagent/server.py','app/src/testagent/scenarios.py','app/web/app.js','app/skills/testagent-skill-author/SKILL.md','app/skills/nic-bandwidth/SKILL.md','app/skills/nic-bandwidth/contract.json','app/skills/nic-bandwidth/scripts/inspect.sh','app/apply-update.ps1'):
            assert name in names,name
        for exe in ('python.exe','pythonw.exe'):
            data=full.read('app/runtime/'+exe)
            assert data[:2]==b'MZ'
            pe=struct.unpack_from('<I',data,0x3c)[0]
            assert data[pe:pe+4]==b'PE\0\0'
            assert struct.unpack_from('<H',data,pe+4)[0]==0x8664,'Expected AMD64'
        pth=full.read('app/runtime/python312._pth').decode().splitlines()
        assert all(x in pth for x in ('../src','Lib/site-packages','import site'))
        full_manifest=json.loads(full.read('update-manifest.json'))
        assert full_manifest['version']==VERSION
        assert set(full_manifest['files'])==names-{'update-manifest.json'}
        for name,digest in full_manifest['files'].items():
            assert hashlib.sha256(full.read(name)).hexdigest()==digest,name
        manifest=json.loads(patch.read('update-manifest.json'))
        assert manifest['version']==VERSION
        assert set(manifest['files'])==set(patch.namelist())-{'update-manifest.json'}
        for name,digest in manifest['files'].items():
            assert hashlib.sha256(patch.read(name)).hexdigest()==digest,name
            assert full.read(name)==patch.read(name),name
        metadata=[n for n in names if n.endswith('.dist-info/METADATA')]
        assert len(metadata)==13,len(metadata)
        print(f'PASS: portable/update ZIP CRC and SHA-256, {len(manifest["files"])} matching app files, Windows x64 PE, embedded search paths, {len(metadata)} distributions, entrypoints and licenses.')
        print('NOT EXECUTED: Windows startup, DPAPI, notifications, native ACP commands and update swap/rollback. Windows CI and internal acceptance are pending.')

if __name__=='__main__':main()
