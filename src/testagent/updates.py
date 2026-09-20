"""Offline portable update validation and staged handoff, never overwrite user data."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from .core import DomainError
from .paths import safe_relative,VERSION,ROOT


class Updates:
    def __init__(self,catalog):
        self.catalog,self.core=catalog,catalog.core
        self.pending=None

    def inspect(self,data):
        self.pending=None
        if not zipfile.is_zipfile(io.BytesIO(data)): raise DomainError('请选择有效的 testagent ZIP 升级包')
        if len(data)>800*1024*1024: raise DomainError('升级包超过 800 MiB')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries=[e for e in archive.infolist() if not e.is_dir()]
            if len(entries)>20000 or sum(e.file_size for e in entries)>2*1024**3: raise DomainError('升级包展开大小超限')
            seen=set()
            for entry in entries:
                name=safe_relative(entry.filename)
                if name.casefold() in seen or stat.S_ISLNK(entry.external_attr>>16): raise DomainError('升级包路径重复或含链接')
                seen.add(name.casefold())
            names={e.filename for e in entries}
            if 'update-manifest.json' not in names:
                if any(name.endswith('-windows-x64-update.zip') for name in names):
                    raise DomainError('这是构建产物外层 ZIP。请先解压，再选择其中的 windows-x64-update.zip 导入')
                if 'app/portable.json' in names:
                    raise DomainError('此完整运行包没有升级清单。请使用同次构建的 windows-x64-update.zip；不要直接导入旧版 portable.zip')
                raise DomainError('缺少 update-manifest.json，请选择 testagent 升级包或带升级清单的完整运行包')
            try:
                manifest=json.loads(archive.read('update-manifest.json'))
            except (ValueError,UnicodeError) as exc:
                raise DomainError('升级清单不是有效的 JSON') from exc
            if not isinstance(manifest,dict) or not isinstance(manifest.get('files'),dict):
                raise DomainError('升级清单格式错误')
            if manifest.get('product')!='pangea-testagent' or manifest.get('schema_version')!=1: raise DomainError('不是 testagent 升级包')
            if manifest.get('version')==VERSION: raise DomainError('此版本已经安装')
            if set(manifest.get('files',{}))!={e.filename for e in entries if e.filename!='update-manifest.json'}: raise DomainError('升级清单不完整')
            entries_set=set(manifest['files'])
            embedded={'app/runtime/pythonw.exe','app/runtime/python.exe','app/entry.py','app/portable.json'}
            if 'app/PangeaTestagent.exe' not in entries_set and not embedded.issubset(entries_set): raise DomainError('升级包缺少程序入口')
            for name,expected in manifest['files'].items():
                if not name.startswith('app/') and name not in ('Start-Testagent.cmd','README.md'): raise DomainError('升级包包含不支持的文件：'+name)
                if hashlib.sha256(archive.read(name)).hexdigest()!=expected: raise DomainError('升级文件校验失败：'+name)
            stage=self.catalog.root/'updates'/'staged'
            if stage.exists():shutil.rmtree(stage)
            stage.mkdir(parents=True)
            for entry in entries:
                if entry.filename.startswith('app/'):
                    destination=stage/entry.filename;destination.parent.mkdir(parents=True,exist_ok=True)
                    with archive.open(entry) as src,destination.open('wb') as dst:shutil.copyfileobj(src,dst)
            self.pending={'version':manifest['version'],'stage':str(stage),'current':VERSION}
            return self.pending

    def apply(self,port):
        if not self.pending:raise DomainError('请先导入升级包')
        with self.core.lock:
            if self.core.db.execute("SELECT 1 FROM tasks WHERE status IN ('queued','running','waiting_user','stopping')").fetchone() or self.core.db.execute('SELECT 1 FROM reservations').fetchone():
                raise DomainError('存在活动任务或现场待处理设备，不能升级')
            if self.core.db.execute("SELECT 1 FROM recovery WHERE status IN ('planning','running')").fetchone():raise DomainError('恢复处理进行中')
        if sys.platform!='win32':raise DomainError('原地升级仅在 Windows 完整运行包中可用')
        app=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else ROOT
        if not getattr(sys,'frozen',False) and not (app/'portable.json').is_file():raise DomainError('需要完整运行包')
        if app.name!='app':raise DomainError('程序必须位于完整运行包 app 目录')
        helper=app/'apply-update.ps1'
        if not helper.exists():raise DomainError('升级辅助程序缺失')
        copied=self.catalog.root/'updates'/'apply-update.ps1';shutil.copy2(helper,copied)
        plan=self.catalog.root/'updates'/'plan.json'
        import os
        plan.write_text(json.dumps({**self.pending,'app':str(app),'data':str(self.catalog.root),'pid':os.getpid(),'port':port}),encoding='utf-8')
        subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(copied),'-Plan',str(plan)],
                         creationflags=subprocess.CREATE_NO_WINDOW)
        return {'restarting':True}
