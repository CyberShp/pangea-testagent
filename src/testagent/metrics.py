"""Evidence-derived samples; missing fields are absent, never fabricated zeros."""
import json
import math
import re
import time


def field(value, path):
    for key in path.split('.'):
        if not isinstance(value,dict) or key not in value:return None
        value=value[key]
    return value


def number(value):
    return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value)


def parse_line(line, parser, definitions=(), received=None):
    received=received or time.time()
    if parser=='none' or not line.strip():return []
    if parser=='vdbench':
        tokens=line.split()
        if len(tokens)<8 or not re.fullmatch(r'\d\d:\d\d:\d\d(?:\.\d+)?',tokens[0]) or not tokens[1].isdigit():return []
        # Vdbench summary interval: timestamp interval iops MB/sec bytes read% resp(ms).
        try:values={'iops':float(tokens[2]),'bandwidth':float(tokens[3])*1024**2,'latency':float(tokens[6])}
        except ValueError:return []
        units={'iops':'iops','bandwidth':'B/s','latency':'ms'}
        return [{'name':name,'value':value,'unit':units[name],'at':received,'clock':'collection',
                 'source':'vdbench','interval':int(tokens[1])} for name,value in values.items() if number(value) and value>=0]
    try:data=json.loads(line)
    except ValueError:return []
    if not isinstance(data,dict):return []
    if parser=='jsonl':
        at=data.get('timestamp',received)
        if not number(at):at=received
        samples=[]
        for definition in definitions:
            value=field(data,definition['field'])
            if number(value):
                scaled=value*definition.get('scale',1)
                if number(scaled):samples.append({'name':definition['name'],'value':scaled,'unit':definition['unit'],
                    'at':at,'clock':'source' if 'timestamp' in data else 'collection','source':'jsonl'})
        return samples
    if data.get('event')!='interval':return []
    body=data.get('data',{});summary=body.get('sum',{})
    if summary.get('omitted'):return []
    samples=[]
    for key,name,unit in [('bits_per_second','bandwidth','bit/s'),('retransmits','retransmits','count'),
                          ('jitter_ms','jitter','ms'),('lost_percent','loss','%')]:
        value=summary.get(key)
        if number(value):samples.append({'name':name,'value':value,'unit':unit,'at':received,'clock':'collection',
            'source':'iperf3','start':summary.get('start'),'end':summary.get('end'),
            'sender':summary.get('sender')})
    return samples


def compare_rounds(jobs, metric='bandwidth'):
    """Compare same-unit measured series; no automatic configuration search."""
    results=[]
    for job in jobs:
        series=[s for s in job.get('samples',[]) if s['name']==metric]
        units={s['unit'] for s in series}
        if len(units)!=1:continue
        results.append({'name':job['name'],'unit':series[0]['unit'],
                        'mean':sum(s['value'] for s in series)/len(series),'samples':len(series)})
    return results
