#!/usr/bin/env python3
"""Linux-only detached supervisor. Stdlib, Python >=3.8, private task directory."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def save(path, value):
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False))
    os.replace(temp,path)


def members(group, marker):
    found=[]
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():continue
        try:
            stat=(path/'stat').read_text().rsplit(')',1)[1].split()
            if int(stat[2])!=group or stat[0]=='Z':continue
            environment=(path/'environ').read_bytes().split(b'\0')
            if ('TESTAGENT_LOAD='+marker).encode() not in environment:
                raise RuntimeError('进程组身份无法确认')
            found.append(int(path.name))
        except (FileNotFoundError,ProcessLookupError):continue
    return found


def stop_group(group, marker):
    if not group:return True
    if members(group,marker):
        os.killpg(group,signal.SIGTERM)
        for _ in range(20):
            if not members(group,marker):return True
            time.sleep(.05)
        if members(group,marker):
            os.killpg(group,signal.SIGKILL)
            for _ in range(20):
                if not members(group,marker):return True
                time.sleep(.05)
    return not members(group,marker)


def control(command,marker):
    child=subprocess.Popen(['sh','-c',command],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        env=dict(os.environ,TESTAGENT_LOAD=marker),start_new_session=True)
    try:
        out,err=child.communicate(timeout=15)
        if child.returncode:raise RuntimeError(err.decode(errors='replace')[-2000:] or '控制命令失败')
        return out.decode(errors='replace')
    finally:
        stop_group(child.pid,marker)
        child.wait(timeout=5)


def run(directory):
    os.umask(0o077)
    config=json.loads((directory/'config.json').read_text())
    marker=config['marker'];status={'state':'running','runner':os.getpid(),'group':None,'marker':marker,
                                  'started':time.time(),'stage':0,'exit_code':None}
    stopping=False
    def cancel(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,cancel);signal.signal(signal.SIGINT,cancel)
    save(directory/'status.json',status)
    history=[]
    try:
        for i,stage in enumerate(config['stages']):
            if stopping:break
            started=time.time()
            with (directory/'output.log').open('ab',buffering=0) as output:
                child=subprocess.Popen(['sh','-c',stage['command']],stdout=output,stderr=subprocess.STDOUT,
                                       env=dict(os.environ,TESTAGENT_LOAD=marker),start_new_session=True)
                status.update(group=child.pid,stage=i,stage_started=started,target=stage['rate'])
                save(directory/'status.json',status)
                deadline=time.monotonic()+stage['duration']+config.get('grace',30)
                timed_out=False
                while child.poll() is None:
                    status['heartbeat']=time.time();save(directory/'status.json',status)
                    if stopping or time.monotonic()>deadline:
                        timed_out=not stopping
                        if not stop_group(child.pid,marker):raise RuntimeError('负载进程组尚未退出')
                        break
                    if (directory/'output.log').stat().st_size>64*1024*1024:
                        raise RuntimeError('负载日志超过 64 MiB 限额')
                    time.sleep(.2)
                child.wait(timeout=5)
                clean=stop_group(child.pid,marker)
                history.append({'index':i,'target':stage['rate'],'started':started,'ended':time.time(),
                                'exit_code':child.returncode,'timeout':timed_out})
                status.update(group=None,exit_code=child.returncode,history=history)
                save(directory/'status.json',status)
                if not clean:raise RuntimeError('负载子进程未确认结束')
                if timed_out:raise RuntimeError('负载超过规定时间')
                if child.returncode and not stopping:raise RuntimeError('负载退出码 '+str(child.returncode))
            if config.get('external'):
                deadline=time.monotonic()+stage['duration']
                while not stopping and time.monotonic()<deadline:
                    raw=control(config['status_command'],marker)
                    observed=json.loads(raw)
                    if not isinstance(observed.get('running'),bool):raise RuntimeError('状态命令必须返回 JSON running 布尔值')
                    with (directory/'output.log').open('a') as stream:stream.write(json.dumps(observed)+'\n')
                    if not observed['running']:raise RuntimeError('自研外部负载提前退出')
                    status['heartbeat']=time.time();save(directory/'status.json',status)
                    time.sleep(1)
        status['state']='stopped' if stopping else 'succeeded'
    except BaseException as exc:
        status.update(state='failed',error=str(exc))
    finally:
        try:
            if config.get('external'):
                control(config['stop_command'],marker)
                observed=json.loads(control(config['status_command'],marker))
                if observed.get('running') is not False:raise RuntimeError('自研外部负载停止尚未核实')
                status['external_stopped']=True
            if status.get('group') and stop_group(status['group'],marker):status['group']=None
        except Exception as exc:status.update(state='unknown',error=str(exc))
        status['ended']=time.time();save(directory/'status.json',status)


def inspect(directory, stop=False):
    path=directory/'status.json'
    if not path.exists():return {'state':'unknown','error':'尚未取得远端启动证据'}
    status=json.loads(path.read_text());marker=status['marker']
    runner=status['runner']
    try:
        alive=bool(members(runner,marker))
        if stop and alive:
            # Runner has its own session; only signal the supervisor, which owns cleanup.
            os.kill(runner,signal.SIGTERM)
            for _ in range(50):
                time.sleep(.1)
                status=json.loads(path.read_text())
                if status['state'] in ('stopped','failed','succeeded') and not status.get('group'):break
        status=json.loads(path.read_text())
        if stop and status.get('group'):
            if stop_group(status['group'],marker):
                status.update(state='stopped',group=None,ended=time.time())
                save(path,status)
        alive=bool(members(runner,marker))
        config=json.loads((directory/'config.json').read_text())
        if stop and not alive and config.get('external') and not status.get('external_stopped'):
            control(config['stop_command'],marker)
            if json.loads(control(config['status_command'],marker)).get('running') is not False:
                raise RuntimeError('自研外部负载停止尚未核实')
            status.update(external_stopped=True,state='stopped',ended=time.time());save(path,status)
        children=bool(status.get('group') and members(status['group'],marker))
        if not alive and status['state']=='running':
            status.update(state='unknown',error='远端监督进程退出，需核对负载')
        status['confirmed_exit']=not alive and not children and status['state'] in ('succeeded','failed','stopped')
    except Exception as exc:status.update(state='unknown',error=str(exc),confirmed_exit=False)
    return status


def main():
    action=sys.argv[1];directory=Path(sys.argv[2])
    if not Path('/proc/self/stat').exists():raise RuntimeError('需要 Linux /proc')
    if action=='run':run(directory);return
    if action=='start':
        config=json.loads((directory/'config.json').read_text())
        if (directory/'status.json').exists():raise RuntimeError('负载目录已使用')
        with (directory/'supervisor.log').open('ab') as log:
            subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'run',str(directory)],
                             stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,
                             env=dict(os.environ,TESTAGENT_LOAD=config['marker']))
        for _ in range(50):
            if (directory/'status.json').exists():break
            time.sleep(.1)
    status=inspect(directory,action=='stop')
    if action=='read':
        offset=int(sys.argv[3]);sample=json.loads((directory/'config.json').read_text()).get('sample_file')
        path=Path(sample) if sample else directory/'output.log'
        if path.exists():
            size=path.stat().st_size
            if size<offset:offset=0;status['rotated']=True
            with path.open('rb') as stream:
                stream.seek(offset);data=stream.read(256*1024)
            # Only consume whole lines; a final partial line is returned at termination.
            if not status.get('confirmed_exit') and data and not data.endswith(b'\n'):
                data=data[:data.rfind(b'\n')+1] if b'\n' in data else b''
            status.update(offset=offset+len(data),text=data.decode('utf-8',errors='replace'),more=offset+len(data)<size)
        else:status.update(offset=offset,text='')
    print(json.dumps(status,ensure_ascii=False))


if __name__=='__main__':main()
