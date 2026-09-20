"""Offline, content-addressed Linux tool packages; no network on target devices."""
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import stat
import zipfile
from .paths import ROOT,safe_relative
from .core import DomainError

LIMIT=64*1024*1024


def inspect(data):
    if len(data)>LIMIT:raise DomainError('工具包限 64 MiB')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files={};seen=set();total=0
        for item in archive.infolist():
            if item.is_dir():continue
            name=safe_relative(item.filename);total+=item.file_size
            if name.casefold() in seen or stat.S_ISLNK(item.external_attr>>16):raise DomainError('工具包路径重复或包含链接')
            if total>LIMIT or len(seen)>4000:raise DomainError('工具包展开超过限制')
            seen.add(name.casefold());files[name]=archive.read(item)
        manifest=json.loads(files.pop('tool.json'))
        for key in ('name','version'):
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}',manifest.get(key,'')):raise DomainError('工具名称或版本无效')
        if manifest.get('architecture') not in ('x86_64','aarch64'):raise DomainError('工具架构必须为 x86_64 或 aarch64')
        if manifest.get('os')!='linux':raise DomainError('工具包必须面向 Linux')
        if manifest.get('driver') not in ('iperf3','vdbench','custom'):raise DomainError('工具驱动无效')
        if set(manifest.get('files',{}))!=set(files):raise DomainError('工具校验清单不完整')
        for name,body in files.items():
            if hashlib.sha256(body).hexdigest()!=manifest['files'][name]:raise DomainError('工具文件校验失败：'+name)
        entry=safe_relative(manifest['entrypoint'])
        if entry not in files:raise DomainError('工具入口不存在')
        executable=manifest.get('executables',[entry])
        if not isinstance(executable,list) or any(n not in files for n in executable):raise DomainError('可执行文件列表无效')
        manifest['executables']=executable
        manifest['id']=hashlib.sha256(data).hexdigest()
        manifest['bytes']=len(data)
        return manifest


class ToolLibrary:
    def __init__(self,catalog):
        self.catalog=catalog;self.root=catalog.root/'tools';self.root.mkdir(exist_ok=True)

    def paths(self):
        return list((ROOT/'tool-bundles').glob('*.zip'))+list(self.root.glob('*.zip'))

    def list(self):
        found={}
        for path in self.paths():
            value=inspect(path.read_bytes());found[value['id']]=value
        return list(found.values())

    def import_zip(self,data):
        value=inspect(data);path=self.root/(value['id']+'.zip')
        if not path.exists():
            temp=path.with_suffix('.tmp');temp.write_bytes(data);temp.replace(path)
        return value

    def deploy(self,ident,executor,device):
        path=next((p for p in self.paths() if hashlib.sha256(p.read_bytes()).hexdigest()==ident),None)
        if path is None:raise DomainError('工具包不存在')
        data=path.read_bytes();manifest=inspect(data)
        arch=executor.exec(device,'uname -m',15)['stdout'].strip()
        if arch!=manifest['architecture']:raise DomainError('工具架构与设备不一致')
        directory='/tmp/testagent-tools-'+ident
        archive_path=directory+'.zip'
        executor.write_file(device,archive_path,data)
        # All archive names and hashes were validated before deployment. Recheck on target.
        code='''import hashlib,json,os,pathlib,sys,zipfile
archive_path,directory=sys.argv[1:]
root=pathlib.Path(directory); root.mkdir(mode=0o700,exist_ok=True)
with zipfile.ZipFile(archive_path) as z:
 m=json.loads(z.read('tool.json'))
 for name,digest in m['files'].items():
  path=root/name
  if not path.resolve().is_relative_to(root.resolve()): raise ValueError('path')
  data=z.read(name)
  if hashlib.sha256(data).hexdigest()!=digest: raise ValueError('checksum')
  path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
  path.chmod(0o700 if name in m.get('executables',[m['entrypoint']]) else 0o600)
print(str(root/m['entrypoint']))
'''.replace("if not path.resolve().is_relative_to(root.resolve()):", "if root.resolve() not in path.resolve().parents:")
        result=executor.exec(device,shlex.join(['python3','-c',code,archive_path,directory]),60)
        entry=result['stdout'].strip()
        if manifest['driver']=='iperf3':
            checked=executor.exec(device,shlex.join([entry,'--version']),15)
        elif manifest['driver']=='vdbench':
            checked=executor.exec(device,'java -version',15)
            checked['note']='Java 可用；Vdbench 本地库需在首次实际负载中验证'
        else:checked={'stdout':'已完成传输与架构检查；运行依赖由场景检查'}
        return {'tool':manifest,'path':entry,'verification':checked}
