#!/usr/bin/env python3
"""Read-only block target checks immediately before Vdbench launch."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

spec=json.loads(sys.argv[1]);targets=[]
for value in spec['targets']:
    path=Path(value).resolve(strict=True);info=path.stat()
    if not stat.S_ISBLK(info.st_mode):raise ValueError('目标不是块设备：'+value)
    devices=subprocess.check_output(['lsblk','-nrpo','NAME,MOUNTPOINT',str(path)],text=True)
    if spec['read_pct']<100:
        lines=[line.split(None,1) for line in devices.splitlines() if line.strip()]
        if any(len(line)>1 for line in lines):raise ValueError('写 IO 目标或其子设备已挂载：'+value)
        swaps={str(Path(line.split()[0]).resolve()) for line in Path('/proc/swaps').read_text().splitlines()[1:]}
        if any(str(Path(line[0]).resolve()) in swaps for line in lines):raise ValueError('目标含正在使用的 swap')
        holders=Path('/sys/dev/block')/f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}'/'holders'
        if any(holders.iterdir()):raise ValueError('目标被其他块设备使用，请核对多路径或卷管理映射')
    targets.append({'requested':value,'resolved':str(path),'major':os.major(info.st_rdev),'minor':os.minor(info.st_rdev),'lsblk':devices})
print(json.dumps({'targets':targets}))
