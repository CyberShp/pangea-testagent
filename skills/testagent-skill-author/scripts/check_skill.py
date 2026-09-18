"""Use the running local application's validator, then package exactly those files."""
import argparse
import json
from pathlib import Path
from urllib.request import Request,urlopen
import zipfile


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('folder',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--server',default='http://127.0.0.1:8765')
    args=parser.parse_args()
    root=args.folder.resolve();files={}
    for path in root.rglob('*'):
        if path.is_symlink():raise ValueError('不允许符号链接')
        if path.is_file():files[path.relative_to(root).as_posix()]=path.read_text(encoding='utf-8-sig')
    metadata=json.loads(files['skill.json'])
    package={k:metadata[k] for k in ('id','name','version')}|{'files':files}
    from urllib.parse import urlsplit
    url=urlsplit(args.server)
    if url.scheme!='http' or url.hostname!='127.0.0.1':raise ValueError('校验仅连接本机 testagent')
    with urlopen(args.server+'/api/state',timeout=10) as response:token=json.load(response)['token']
    request=Request(args.server+'/api/skills/validate',data=json.dumps(package,ensure_ascii=False).encode(),headers={'Content-Type':'application/json','X-Testagent-Token':token})
    with urlopen(request,timeout=30) as response:result=json.load(response)['result']
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if result['mode']=='invalid':raise SystemExit(1)
    if args.output:
        with zipfile.ZipFile(args.output,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,text in files.items():archive.writestr(name,text)

if __name__=='__main__':main()
