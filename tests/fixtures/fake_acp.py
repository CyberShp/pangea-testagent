"""Protocol fixture, not a real model. Exercises stdio MCP end to end."""
import json
import os
import subprocess
import sys

bridge=None;seq=0

def output(value):print(json.dumps(value),flush=True)
def mcp(method,params):
    global seq
    seq+=1
    bridge.stdin.write(json.dumps({'jsonrpc':'2.0','id':seq,'method':method,'params':params})+'\n');bridge.stdin.flush()
    return json.loads(bridge.stdout.readline())

for line in sys.stdin:
    req=json.loads(line);method=req.get('method');params=req.get('params',{})
    if method=='initialize':result={'protocolVersion':1,'agentCapabilities':{},'agentInfo':{'name':'fixture','version':'1'}}
    elif method=='session/new':
        servers=params.get('mcpServers',[])
        if servers:
            server=servers[0];env=os.environ.copy();env.update({x['name']:x['value'] for x in server['env']})
            bridge=subprocess.Popen([server['command'],*server['args']],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True,env=env)
            mcp('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'fixture','version':'1'}})
            tools=mcp('tools/list',{})
            assert tools['result']['tools'][0]['name']=='testagent'
        result={'sessionId':'fixture-session','models':{'availableModels':[{'modelId':'fixture-model','name':'Fixture'}],'currentModelId':'fixture-model'}}
    elif method=='session/set_model':result={}
    elif method=='session/prompt':
        if bridge:
            reply=mcp('tools/call',{'name':'testagent','arguments':{'action':'files'}})
            assert not reply.get('error'), reply
            mcp('tools/call',{'name':'testagent','arguments':{'action':'finish','summary':'Fixture protocol complete'}})
        output({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'fixture-session','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'Fixture completed'}}}})
        result={'stopReason':'end_turn'}
    elif method=='session/cancel':break
    else:result={}
    if 'id' in req:output({'jsonrpc':'2.0','id':req['id'],'result':result})
if bridge:bridge.terminate()
