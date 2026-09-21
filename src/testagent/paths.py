from pathlib import Path, PurePosixPath
import os
import sys

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
VERSION = '1.1.2'


def data_root():
    return Path(os.environ.get('TESTAGENT_DATA_DIR', Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'PangeaTestagent'))


def safe_relative(name):
    name = str(name)
    p = PurePosixPath(name)
    if (not name or name.startswith('/') or '\\' in name or ':' in name or any(ord(c)<32 or c in '<>"|?*' for c in name)
            or any(part in ('', '.', '..') for part in name.split('/'))
            or any(part.endswith((' ', '.')) for part in p.parts)
            or any(part.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]} for part in p.parts)):
        raise ValueError('不合法的相对文件路径')
    return p.as_posix()


def contained(root, name):
    base = Path(root).resolve()
    path = base.joinpath(safe_relative(name)).resolve()
    if not path.is_relative_to(base):
        raise ValueError('文件路径超出任务目录')
    return path
