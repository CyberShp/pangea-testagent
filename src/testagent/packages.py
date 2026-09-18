"""Bounded folder/ZIP import with one validation implementation."""
import io
import json
from pathlib import Path
import stat
import zipfile
from .paths import safe_relative
from .skills import validate

MAX_PACKAGE = 32 * 1024 * 1024


def from_files(files, identity=None):
    cleaned = {}
    total = 0
    for name, content in files.items():
        name = safe_relative(name)
        total += len(content.encode('utf-8'))
        if total > MAX_PACKAGE:
            raise ValueError('Skill 文本文件总量超过 32 MiB')
        if name.casefold() in {n.casefold() for n in cleaned}:
            raise ValueError('Skill 存在大小写重复路径')
        cleaned[name] = content
    metadata = json.loads(cleaned.get('skill.json', '{}'))
    if not metadata and cleaned.get('SKILL.md'):
        import re,hashlib
        body=cleaned['SKILL.md']
        front=body.split('---',2)[1] if body.startswith('---') and body.count('---')>=2 else ''
        fields={m.group(1):m.group(2).strip().strip('\"\'') for m in re.finditer(r'(?m)^(name|version):\s*(.+)$',front)}
        name=fields.get('name') or next((line.lstrip('# ').strip() for line in body.splitlines() if line.startswith('# ')), '普通 Skill')
        ident=re.sub(r'[^a-z0-9_-]+','-',name.lower()).strip('-')[:64]
        if not ident: ident='skill-'+hashlib.sha256(name.encode()).hexdigest()[:12]
        metadata={'id':ident,'name':name,'version':fields.get('version','1.0.0')}

    if identity:
        metadata.update(identity)
    for key in ('id', 'name', 'version'):
        if not metadata.get(key):
            raise ValueError('缺少 skill.json 元信息：' + key)
    return {k: metadata[k] for k in ('id', 'name', 'version')} | {'files': cleaned}


def from_zip(data):
    if len(data) > MAX_PACKAGE:
        raise ValueError('Skill 包过大')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = [x for x in archive.infolist() if not x.is_dir()]
        if len(infos) > 2000 or sum(x.file_size for x in infos) > MAX_PACKAGE:
            raise ValueError('Skill 展开后超过限额')
        names = [safe_relative(x.filename) for x in infos]
        prefix = ''
        if 'SKILL.md' not in names:
            roots = [n[:-len('SKILL.md')] for n in names if n.endswith('/SKILL.md')]
            if len(roots) == 1 and all(n.startswith(roots[0]) for n in names):
                prefix = roots[0]
        files = {}
        for entry, name in zip(infos, names):
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('Skill 不允许符号链接')
            name = name[len(prefix):]
            if name in files:
                raise ValueError('Skill 存在重复文件')
            files[name] = archive.read(entry).decode('utf-8-sig')
        return from_files(files)


def from_folder(path):
    root = Path(path).resolve()
    files = {}
    size = 0
    for entry in root.rglob('*'):
        if entry.is_symlink():
            raise ValueError('Skill 不允许符号链接')
        if entry.is_file():
            size += entry.stat().st_size
            if size > MAX_PACKAGE or len(files) >= 2000:
                raise ValueError('Skill 包过大')
            files[entry.relative_to(root).as_posix()] = entry.read_text(encoding='utf-8-sig')
    return from_files(files)


def to_zip(package):
    files = dict(package['files'])
    files['skill.json'] = json.dumps({k: package[k] for k in ('id', 'name', 'version')}, ensure_ascii=False, indent=2)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(safe_relative(name), content)
    return stream.getvalue()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('folder')
    parser.add_argument('--output')
    args = parser.parse_args()
    package = from_folder(args.folder)
    result = validate(package)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result['mode'] == 'invalid':
        raise SystemExit(1)
    if args.output:
        Path(args.output).write_bytes(to_zip(package))

if __name__ == '__main__':
    main()
