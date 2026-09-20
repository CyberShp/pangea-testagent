"""Shared load, metric and tuning declarations used by import and execution."""
ID = {'type':'string','pattern':'^[a-z][a-z0-9_-]{0,63}$'}
METRICS = {'type':'array','maxItems':32,'items':{'type':'object','additionalProperties':False,
    'properties':{'name':ID,'unit':{'enum':['iops','B/s','bit/s','ms','us','%','count']},
                  'field':{'type':'string','minLength':1},'scale':{'type':'number','exclusiveMinimum':0}},
    'required':['name','unit','field']}}
PLAN = {'type':'object','additionalProperties':False,'properties':{
    'shape':{'enum':['constant','square','surge','random']},
    'duration':{'type':'integer','minimum':1,'maximum':86400},
    'interval':{'type':'integer','minimum':1,'maximum':3600},
    'low':{'type':'number','minimum':0},'high':{'type':'number','minimum':0},
    'rate':{'type':'number','minimum':0},'seed':{'type':'integer'},
    'surge_start':{'type':'integer','minimum':0},'surge_duration':{'type':'integer','minimum':1}},
    'required':['shape','duration','interval']}
LOAD = {'type':'object','additionalProperties':False,'properties':{
    'name':ID,'driver':{'enum':['iperf3','vdbench','custom']},
    'command':{'type':'string','minLength':1,'maxLength':65536},
    'duration':{'type':'integer','minimum':1,'maximum':86400},
    'parser':{'enum':['iperf3','vdbench','jsonl','none']},'metrics':METRICS,
    'external':{'type':'boolean'},'status_command':{'type':'string','minLength':1,'maxLength':65536},
    'stop_command':{'type':'string','minLength':1,'maxLength':65536},
    'sample_file':{'type':'string','pattern':'^/'},
    'plan':PLAN,'cpus':{'type':'string','pattern':'^[0-9]+(?:[-,][0-9]+)*$'},
    'tool_path':{'type':'string','pattern':'^/'},
    'iperf':{'type':'object','additionalProperties':False,'properties':{
        'server':{'type':'boolean'},'one_off':{'type':'boolean'},'bind':{'type':'string','minLength':1},
        'peer':{'type':'string','minLength':1},'port':{'type':'integer','minimum':1024,'maximum':65535},
        'parallel':{'type':'integer','minimum':1,'maximum':128},'reverse':{'type':'boolean'},
        'warmup':{'type':'integer','minimum':0,'maximum':30},
        'bitrate':{'type':'number','minimum':0}},'required':['bind','port']},
    'vdbench':{'type':'object','additionalProperties':False,'properties':{
        'targets':{'type':'array','minItems':1,'maxItems':64,'items':{'type':'string','pattern':'^/dev/[A-Za-z0-9_./-]+$'}},
        'read_pct':{'type':'integer','minimum':0,'maximum':100},
        'seek_pct':{'type':'integer','minimum':0,'maximum':100},
        'block_kib':{'type':'integer','minimum':1,'maximum':1024},
        'threads':{'type':'integer','minimum':1,'maximum':1024},
        'rate':{'type':'integer','minimum':1},'write_confirmed':{'type':'boolean'}},
        'required':['targets','read_pct','seek_pct','block_kib','threads','rate']}},
    'required':['name','driver','duration']}
WORKLOAD_DECLARATIONS={'type':'array','maxItems':64,'items':{'type':'object','additionalProperties':False,
    'properties':{'id':ID,'role':{'type':'string'},'driver':{'enum':['iperf3','vdbench','custom']},
        'parser':{'enum':['iperf3','vdbench','jsonl','none']},'metrics':METRICS,
        'start':{'type':'string'},'status':{'type':'string'},'stop':{'type':'string'},
        'recovery':{'type':'string','minLength':1}},'required':['id','role','driver','recovery']}}
TUNING={'type':'object','additionalProperties':False,'properties':{
    'name':ID,'interface':{'type':'string','pattern':'^[A-Za-z0-9_.:-]+$'},
    'irqs':{'type':'object','minProperties':1,'maxProperties':256,'patternProperties':{
        '^[0-9]+$':{'type':'string','pattern':'^[0-9]+(?:[-,][0-9]+)*$'}},'additionalProperties':False},
    'pause_irqbalance':{'type':'boolean'}},'required':['name','interface','irqs']}
