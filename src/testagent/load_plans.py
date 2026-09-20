"""Deterministic load stages. Switching boundaries remain visible in results."""
import ipaddress
import random
import shlex
from jsonschema import validate
from .load_schema import LOAD, PLAN


def stages(plan):
    validate(plan, PLAN)
    low, high = plan.get('low', 0), plan.get('high', 0)
    if high < low:
        raise ValueError('负载上限不能低于下限')
    shape = plan['shape']
    required = {'constant':['rate'], 'square':['low','high'],
                'surge':['low','high','surge_start','surge_duration'], 'random':['low','high','seed']}[shape]
    if any(k not in plan for k in required):
        raise ValueError('负载计划缺少参数：' + ','.join(required))
    rng = random.Random(plan.get('seed'))
    result, elapsed = [], 0
    while elapsed < plan['duration']:
        length = min(plan['interval'], plan['duration'] - elapsed)
        if shape == 'constant': rate = plan['rate']
        elif shape == 'square': rate = high if len(result) % 2 else low
        elif shape == 'random': rate = rng.uniform(low, high)
        else:
            start,end=plan['surge_start'],plan['surge_start']+plan['surge_duration']
            rate = high if start <= elapsed < end else low
            boundary = start if elapsed < start else end
            if elapsed < boundary: length = min(length, boundary-elapsed)
        result.append({'duration':length,'rate':round(rate,3),'offset':elapsed})
        elapsed += length
        if len(result)>1000: raise ValueError('负载阶段超过 1000，请增加变化间隔')
    return result


def compile_load(spec, directory):
    validate(spec,LOAD)
    driver=spec['driver']
    plan=spec.get('plan')
    if plan and plan['duration'] != spec['duration']:raise ValueError('计划与负载持续时间必须一致')
    sequence=stages(plan) if plan else [{'duration':spec['duration'],'rate':None,'offset':0}]
    files={}; compiled=[]
    if spec.get('external'):
        if driver!='custom' or not spec.get('status_command') or not spec.get('stop_command'):raise ValueError('外部负载需要自研工具及状态、停止命令')
        if plan:raise ValueError('外部工具负载计划由内网启动命令实现，平台管理其启停与采集')
    quote=shlex.quote
    if driver=='iperf3':
        cfg=spec.get('iperf')
        if not cfg:raise ValueError('缺少 iperf 参数')
        bind=str(ipaddress.ip_address(cfg['bind']))
        if cfg.get('server'):
            if plan:raise ValueError('服务端通过客户端施加负载计划')
        elif not cfg.get('peer'):raise ValueError('缺少对端测试 IP')
    if driver=='vdbench':
        cfg=spec.get('vdbench')
        if not cfg:raise ValueError('缺少 Vdbench 参数')
        if cfg['read_pct']<100 and not cfg.get('write_confirmed'):raise ValueError('写 IO 必须明确确认测试设备可写')
        if len(set(cfg['targets']))!=len(cfg['targets']):raise ValueError('测试设备不能重复')
    for i,stage in enumerate(sequence):
        rate=stage['rate'];duration=stage['duration']
        if rate == 0:
            command='sleep '+str(duration)
        elif driver=='custom':
            command=spec.get('command','')
            if not command:raise ValueError('自研工具需要具体 command')
            if plan and ('{rate}' not in command or '{duration}' not in command):
                raise ValueError('分段自研负载命令必须包含 {rate} 和 {duration}')
            command=command.replace('{duration}',str(duration))
            if rate is not None:command=command.replace('{rate}',str(rate))
        elif driver=='iperf3':
            cfg=spec['iperf'];path=spec.get('tool_path','iperf3')
            args=[path,'-B',cfg['bind'],'-p',str(cfg['port']),'--forceflush']
            if cfg.get('server'):
                args+=['-s']
                if cfg.get('one_off',True):args+=['-1']
            else:
                args+=['-c',str(ipaddress.ip_address(cfg['peer'])),'-P',str(cfg.get('parallel',4)),
                       '-t',str(duration),'-O',str(cfg.get('warmup',0)), '--json-stream','--get-server-output']
                if cfg.get('reverse'):args+=['-R']
                # iperf3's -b is per stream; convert the whole-pair target.
                target=rate if rate is not None else cfg.get('bitrate')
                if target is not None:args+=['-b',str(int(target/cfg.get('parallel',4)))]
            command=shlex.join(args)
        else:
            cfg=spec['vdbench']; path=spec.get('tool_path','vdbench')
            content=[]
            for n,target in enumerate(cfg['targets'],1):
                if '..' in target.split('/'):raise ValueError('测试设备路径无效')
                content.append(f'sd=sd{n},lun={target},threads={cfg["threads"]},openflags=o_direct')
            content += [f'wd=wd1,sd=sd*,xfersize={cfg["block_kib"]}k,rdpct={cfg["read_pct"]},seekpct={cfg["seek_pct"]}',
                        f'rd=rd1,wd=wd1,iorate={max(1,int(rate if rate is not None else cfg["rate"]))},elapsed={duration},interval=1']
            config=directory+f'/vdbench-{i}.conf';files[config]='\n'.join(content)+'\n'
            command=shlex.join([path,'-f',config,'-o',directory+f'/vdbench-{i}'])
        if spec.get('cpus'):command='taskset -c '+quote(spec['cpus'])+' sh -c '+quote(command)
        compiled.append({**stage,'command':command})
    return {'stages':compiled,'files':files,'transition':'restart_between_stages' if len(sequence)>1 else 'continuous',
            'parser':spec.get('parser',driver if driver in ('iperf3','vdbench') else 'none'),
            'metrics':spec.get('metrics',[]),'sample_file':spec.get('sample_file'),
            'external':spec.get('external',False),'status_command':spec.get('status_command'),
            'stop_command':spec.get('stop_command')}
