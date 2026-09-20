#!/usr/bin/env python3
"""Apply/restore only the declared NIC IRQs, with durable original values."""
import json
import os
from pathlib import Path
import subprocess
import sys


def cpus(text):
    result=set()
    for item in text.split(','):
        bounds=item.split('-');first=int(bounds[0]);last=int(bounds[-1])
        if first<0 or last<first or last-first>4096:raise ValueError('CPU 范围无效')
        result.update(range(first,last+1))
    return result


def active():
    result=subprocess.run(['systemctl','is-active','irqbalance'],capture_output=True,text=True)
    if result.returncode not in (0,3,4):raise RuntimeError('无法核对 irqbalance')
    return result.stdout.strip()=='active'


def save(path,value):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(value));os.replace(tmp,path)


def main():
    action,path=sys.argv[1],Path(sys.argv[2])
    if action=='capture':
        if path.exists():raise RuntimeError('原值备份已存在')
        spec=json.loads(sys.argv[3]);nic=Path('/sys/class/net')/spec['interface']/'device/msi_irqs'
        allowed={p.name for p in nic.iterdir()}
        online=cpus(Path('/sys/devices/system/cpu/online').read_text().strip())
        before={}
        for irq,value in spec['irqs'].items():
            if irq not in allowed:raise ValueError('IRQ 不属于指定网卡')
            if not cpus(value)<=online:raise ValueError('CPU 不在线')
            before[irq]=Path('/proc/irq',irq,'smp_affinity_list').read_text().strip()
        balance=active()
        if balance and not spec.get('pause_irqbalance'):raise ValueError('irqbalance 正在运行，需明确确认暂停后再调整')
        record={'spec':spec,'before':before,'irqbalance':balance,'restored':False,'phase':'captured',
                'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
        save(path,record)
        print(json.dumps(record));return
    record=json.loads(path.read_text())
    if action=='apply':
        spec=record['spec'];balance=record['irqbalance']
        for irq,value in record['before'].items():
            if cpus(Path('/proc/irq',irq,'smp_affinity_list').read_text().strip())!=cpus(value):raise RuntimeError('原值已变化，需重新生成方案')
        if active()!=balance:raise RuntimeError('irqbalance 状态已变化')
        record['phase']='applying';save(path,record)
        if balance:subprocess.run(['systemctl','stop','irqbalance'],check=True)
        for irq,value in spec['irqs'].items():
            target=Path('/proc/irq',irq,'smp_affinity_list');target.write_text(value)
            if cpus(target.read_text().strip())!=cpus(value):raise RuntimeError('IRQ 写入回读不一致')
        print(json.dumps(record));return
    record=json.loads(path.read_text())
    if record['phase']=='captured':
        record['restored']=True;save(path,record);print(json.dumps(record));return
    if record['boot_id']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip():raise RuntimeError('设备已重启，需重新核对 IRQ，不能套用旧编号')
    allowed={p.name for p in (Path('/sys/class/net')/record['spec']['interface']/'device/msi_irqs').iterdir()}
    if not set(record['before'])<=allowed:raise RuntimeError('IRQ 归属已变化')
    for irq,value in record['before'].items():
        target=Path('/proc/irq',irq,'smp_affinity_list');target.write_text(value)
        if cpus(target.read_text().strip())!=cpus(value):raise RuntimeError('IRQ 恢复回读不一致')
    if record['irqbalance']:subprocess.run(['systemctl','start','irqbalance'],check=True)
    if active()!=record['irqbalance']:raise RuntimeError('irqbalance 恢复状态不一致')
    record['restored']=True;save(path,record);print(json.dumps(record))


if __name__=='__main__':main()
