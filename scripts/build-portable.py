"""Assemble a self-contained Windows x64 runtime from official embedded CPython + wheels.
Run on Windows or Linux. Building on Linux does not replace Windows acceptance testing.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import urlretrieve
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from testagent.paths import VERSION,safe_relative


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--python-zip',type=Path)
    parser.add_argument('--wheels',type=Path)
    args=parser.parse_args()
    cache=ROOT/'build'/'windows-inputs';cache.mkdir(parents=True,exist_ok=True)
    python_zip=args.python_zip or cache/'python-3.12.10-embed-amd64.zip'
    if not python_zip.exists():urlretrieve('https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip',python_zip)
    wheels=args.wheels or cache/'wheels'
    if args.wheels is None:
        wheels.mkdir(exist_ok=True)
        subprocess.run([sys.executable,'-m','pip','download','--platform','win_amd64','--python-version','312','--implementation','cp','--abi','cp312','--only-binary=:all:','-r',str(ROOT/'requirements-windows.lock'), '--dest',str(wheels)],check=True)
    wheel_files=sorted(wheels.glob('*.whl'))
    if not wheel_files:raise SystemExit('No dependency wheels')
    stage=ROOT/'dist'/'portable'
    if stage.exists():shutil.rmtree(stage)
    app=stage/'app';runtime=app/'runtime';runtime.mkdir(parents=True)
    with zipfile.ZipFile(python_zip) as archive:
        for item in archive.infolist():
            if not item.is_dir():
                target=runtime/safe_relative(item.filename);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(item))
    site=runtime/'Lib'/'site-packages';site.mkdir(parents=True)
    inputs={python_zip.name:hashlib.sha256(python_zip.read_bytes()).hexdigest()}
    for wheel in wheel_files:
        if not wheel.name.endswith(('win_amd64.whl','none-any.whl')):raise ValueError('Unexpected wheel platform: '+wheel.name)
        inputs[wheel.name]=hashlib.sha256(wheel.read_bytes()).hexdigest()
        with zipfile.ZipFile(wheel) as archive:
            for item in archive.infolist():
                if item.is_dir():continue
                name=safe_relative(item.filename)
                parts=name.split('/')
                if parts[0].endswith('.data'):
                    if len(parts)<3 or parts[1] not in ('purelib','platlib'):continue
                    name='/'.join(parts[2:])
                target=site/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.read(item))
    (runtime/'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n../src\nimport site\n',encoding='utf-8')
    for name in ('src','web','examples','skills'):
        shutil.copytree(ROOT/name,app/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('entry.py','apply-update.ps1'):shutil.copy2(ROOT/'scripts'/name,app/name)
    (app/'portable.json').write_text(json.dumps({'product':'pangea-testagent','version':VERSION,'runtime':'cpython-3.12.10-win-amd64','inputs_sha256':inputs},indent=2),encoding='utf-8')
    (stage/'Start-Testagent.cmd').write_bytes(b'@echo off\r\nstart "" "%~dp0app\\runtime\\pythonw.exe" "%~dp0app\\entry.py"\r\n')
    shutil.copy2(ROOT/'README.md',stage/'README.md')
    for suffix,full in [('windows-x64-portable',True),('windows-x64-update',False)]:
        target=ROOT/'dist'/f'pangea-testagent-{VERSION}-{suffix}.zip'
        manifest={'product':'pangea-testagent','schema_version':1,'version':VERSION,'files':{}}
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(stage.rglob('*')):
                if not file.is_file():continue
                name=file.relative_to(stage).as_posix()
                if not full and not name.startswith('app/'):continue
                archive.write(file,name)
                manifest['files'][name]=hashlib.sha256(file.read_bytes()).hexdigest()
            archive.writestr('update-manifest.json',json.dumps(manifest,indent=2))
        target.with_suffix('.zip.sha256').write_text(hashlib.sha256(target.read_bytes()).hexdigest()+'  '+target.name+'\n')
        print(target)

    patch=ROOT/'dist'/f'pangea-testagent-{VERSION}-windows-x64-patch.zip'
    all_files={f.relative_to(stage).as_posix():f for f in app.rglob('*') if f.is_file()}
    manifest={'product':'pangea-testagent','schema_version':2,'kind':'application-patch','version':VERSION,
              'runtime':'cpython-3.12.10-win-amd64','files':{},'runtime_files':{}}
    with zipfile.ZipFile(patch,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,file in sorted(all_files.items()):
            digest=hashlib.sha256(file.read_bytes()).hexdigest()
            if name.startswith('app/runtime/'):manifest['runtime_files'][name]=digest
            else:
                archive.write(file,name);manifest['files'][name]=digest
        archive.writestr('update-manifest.json',json.dumps(manifest,indent=2))
    patch.with_suffix('.zip.sha256').write_text(hashlib.sha256(patch.read_bytes()).hexdigest()+'  '+patch.name+'\n')
    helpers=ROOT/'dist'
    shutil.copy2(ROOT/'src/testagent/patching.py',helpers/'prepare-patch.py')
    (helpers/'Prepare-Patch.ps1').write_text(r"""$ErrorActionPreference = 'Stop'
$install = Read-Host 'Testagent install folder (contains app)'
$app = Join-Path $install.Trim('"') 'app'
& (Join-Path $app 'runtime\python.exe') (Join-Path $PSScriptRoot 'prepare-patch.py') --app $app --patch (Join-Path $PSScriptRoot 'PATCHNAME') --output (Join-Path $PSScriptRoot 'compatible-update.zip')
if ($LASTEXITCODE -ne 0) { throw 'Patch preparation failed. No installation files were changed.' }
Read-Host 'Import compatible-update.zip in the running Testagent web UI. Press Enter to close'
""".replace('PATCHNAME',patch.name),encoding='utf-8-sig')
    (helpers/'Prepare-Patch.cmd').write_bytes(b'@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Prepare-Patch.ps1"\r\nif errorlevel 1 pause\r\n')
    (helpers/'PATCH-README.txt').write_text('1.0.2 及以上：网页直接导入 patch.zip。\n1.0.0/1.0.1：先启动原程序，双击 Prepare-Patch.cmd，填写原安装目录；生成 compatible-update.zip 后在原网页导入。\n补丁复用原运行时，只更新应用文件；运行时不匹配时请使用完整包。准备脚本不会修改原安装或用户数据。\n',encoding='utf-8-sig')
    print(patch)

if __name__=='__main__':main()
