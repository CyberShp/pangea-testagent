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


def read_archive(data):
    if len(data)>LIMIT:raise DomainError('工具包限 64 MiB')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files={};seen=set();total=0
            for item in archive.infolist():
                if stat.S_ISLNK(item.external_attr>>16):raise DomainError('工具包包含链接')
                if item.is_dir():continue
                name=safe_relative(item.filename);total+=item.file_size
                if name.casefold() in seen:raise DomainError('工具包路径重复')
                if total>LIMIT or len(seen)>=4000:raise DomainError('工具包展开超过限制')
                seen.add(name.casefold());files[name]=archive.read(item)
            return files
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise DomainError('无法读取工具 ZIP：文件损坏、加密或压缩格式不支持') from exc


def inspect(data):
    files=read_archive(data)
    if 'tool.json' not in files:raise DomainError('工具包缺少根目录 tool.json；Vdbench 原始 ZIP 请通过导入工具包入口导入并填写版本')
    try:manifest=json.loads(files.pop('tool.json'))
    except (ValueError, UnicodeError) as exc:raise DomainError('tool.json 不是有效的 JSON') from exc
    if not isinstance(manifest,dict):raise DomainError('tool.json 必须是 JSON 对象')
    for key in ('name','version'):
        if not isinstance(manifest.get(key),str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}',manifest[key]):raise DomainError('工具名称或版本无效')
    if manifest.get('architecture') not in ('x86_64','aarch64'):raise DomainError('工具架构必须为 x86_64 或 aarch64')
    if manifest.get('os')!='linux':raise DomainError('工具包必须面向 Linux')
    if manifest.get('driver') not in ('iperf3','vdbench','custom'):raise DomainError('工具驱动无效')
    hashes=manifest.get('files')
    if not isinstance(hashes,dict) or set(hashes)!=set(files):raise DomainError('工具校验清单不完整')
    for name,body in files.items():
        if hashlib.sha256(body).hexdigest()!=hashes[name]:raise DomainError('工具文件校验失败：'+name)
    if not isinstance(manifest.get('entrypoint'),str):raise DomainError('tool.json 缺少有效的 entrypoint')
    entry=safe_relative(manifest['entrypoint'])
    if entry not in files:raise DomainError('工具入口不存在')
    executable=manifest.get('executables',[entry])
    if not isinstance(executable,list) or any(not isinstance(n,str) or n not in files for n in executable) or entry not in executable:raise DomainError('可执行文件列表无效')
    manifest['executables']=executable
    manifest['id']=hashlib.sha256(data).hexdigest()
    manifest['bytes']=len(data)
    return manifest


def normalize_package(data, version='', architecture=''):
    files=read_archive(data)
    if 'tool.json' in files:
        inspect(data)
        return data
    # Strip a common wrapper only when it contains the entire payload.
    manifests=[n for n in files if n.endswith('/tool.json')]
    entries=manifests or [n for n in files if n=='vdbench' or n.endswith('/vdbench')]
    if len(entries)!=1:raise DomainError('无法识别工具包：需包含 tool.json，或唯一的 vdbench 启动脚本及 vdbench.jar')
    prefix=entries[0].rsplit('/',1)[0]+'/' if '/' in entries[0] else ''
    if prefix:
        if not all(n.startswith(prefix) for n in files):raise DomainError('工具包包含多个目录，请只打包工具所在目录')
        files={n[len(prefix):]:body for n,body in files.items()}
    if not manifests:
        if not files.get('vdbench') or not files.get('vdbench.jar'):raise DomainError('Vdbench 包缺少 vdbench 启动脚本或 vdbench.jar')
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}',version):raise DomainError('请填写 Vdbench 实际版本（例如 5.04.07），然后重新导入')
        detected=set()
        for name,body in files.items():
            if name.split('/')[0].lower().startswith('linux') and body[:4]==b'\x7fELF' and len(body)>=20 and body[5] in (1,2):
                machine=int.from_bytes(body[18:20], 'little' if body[5]==1 else 'big')
                detected.add({62:'x86_64',183:'aarch64'}.get(machine,'unsupported'))
        if not architecture:
            if len(detected)!=1 or 'unsupported' in detected:raise DomainError('无法唯一识别 Linux 架构，请选择与本地库匹配的 x86_64 或 ARM64')
            architecture=next(iter(detected))
        if architecture not in ('x86_64','aarch64'):raise DomainError('工具架构必须为 x86_64 或 aarch64')
        if detected and architecture not in detected:raise DomainError('所选架构与 Vdbench Linux 本地库不一致')
        manifest={'name':'vdbench','version':version,'os':'linux','architecture':architecture,
                  'driver':'vdbench','entrypoint':'vdbench','executables':['vdbench'],
                  'files':{n:hashlib.sha256(body).hexdigest() for n,body in files.items()}}
        files['tool.json']=json.dumps(manifest,ensure_ascii=False).encode()
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,body in sorted(files.items()):
            item=zipfile.ZipInfo(name);item.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(item,body)
    normalized=output.getvalue()
    inspect(normalized)
    return normalized


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

    def import_zip(self,data,version='',architecture=''):
        data=normalize_package(data,version,architecture)
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
