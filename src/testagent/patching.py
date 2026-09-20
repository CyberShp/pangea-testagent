"""Rebuild an application-only patch with the installed, verified runtime."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import sys
import zipfile


def rebuild_patch(data, app):
    app=Path(app).resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        entries=source.infolist()
        if len(entries)>20000 or sum(e.file_size for e in entries)>2*1024**3:
            raise ValueError('补丁展开大小超限')
        names=set()
        for entry in entries:
            name=entry.filename
            if entry.is_dir():continue
            parts=name.split('/')
            if (name in names or name.casefold() in {n.casefold() for n in names} or '\\' in name or ':' in name
                or any(p in ('','..','.') for p in parts) or stat.S_ISLNK(entry.external_attr>>16)):
                raise ValueError('补丁路径无效或重复')
            names.add(name)
        manifest=json.loads(source.read('update-manifest.json'))
        if manifest.get('product')!='pangea-testagent' or manifest.get('schema_version')!=2 or manifest.get('kind')!='application-patch':
            raise ValueError('不是 testagent 应用补丁')
        files=manifest.get('files',{});runtime=manifest.get('runtime_files',{})
        if not isinstance(files,dict) or not isinstance(runtime,dict) or not runtime:
            raise ValueError('补丁清单无效')
        if set(files)!=names-{'update-manifest.json'}:raise ValueError('补丁清单不完整')
        if not {'app/entry.py','app/portable.json','app/src/testagent/paths.py'}.issubset(files):
            raise ValueError('补丁缺少程序文件')
        if not {'app/runtime/python.exe','app/runtime/pythonw.exe'}.issubset(runtime):
            raise ValueError('补丁缺少运行时要求')
        for name,digest in files.items():
            if not name.startswith('app/') or name.startswith('app/runtime/'):
                raise ValueError('补丁只能包含应用文件')
            if hashlib.sha256(source.read(name)).hexdigest()!=digest:raise ValueError('补丁文件校验失败：'+name)
        installed=json.loads((app/'portable.json').read_text(encoding='utf-8'))
        if installed.get('product')!='pangea-testagent':raise ValueError('请选择 testagent 安装目录')
        if installed.get('runtime')!=manifest.get('runtime'):raise ValueError('运行时不兼容，请使用完整包升级')
        buffers={}
        for name,digest in runtime.items():
            parts=PurePosixPath(name).parts
            if not name.startswith('app/runtime/') or '\\' in name or ':' in name or any(p in ('','..','.') for p in name.split('/')):
                raise ValueError('运行时路径无效')
            path=app.joinpath(*parts[1:]).resolve()
            if not path.is_relative_to(app):raise ValueError('运行时路径越界')
            if not path.is_file():raise ValueError('运行时不完整，请使用完整包升级')
            body=path.read_bytes()
            if hashlib.sha256(body).hexdigest()!=digest:raise ValueError('运行时不兼容，请使用完整包升级：'+name)
            buffers[name]=body
        result=io.BytesIO()
        with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as target:
            for name in files:target.writestr(name,source.read(name))
            for name,body in buffers.items():target.writestr(name,body)
            target.writestr('update-manifest.json',json.dumps({'product':'pangea-testagent','schema_version':1,
                'version':manifest['version'],'files':{**files,**runtime}}))
        return result.getvalue()


def main():
    parser=argparse.ArgumentParser(description='为旧版生成可导入的升级包，不修改安装文件或用户数据')
    parser.add_argument('--app',type=Path,required=True)
    parser.add_argument('--patch',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.write_bytes(rebuild_patch(args.patch.read_bytes(),args.app))
    print('已生成升级包，请在网页导入：'+str(args.output.resolve()))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    try:main()
    except Exception as exc:raise SystemExit(str(exc))
