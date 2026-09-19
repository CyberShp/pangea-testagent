"""MCP stdio proxy: the Agent receives only a task-scoped gateway token, never SSH secrets."""
import json
import os
import sys
from urllib.request import Request,urlopen
from .tools import TOOL


def main():
    # MCP stdio is UTF-8 regardless of the Windows console code page.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        request=None
        try:
            request=json.loads(line)
            if 'id' not in request: continue
            method=request.get('method');params=request.get('params',{})
            if method=='initialize':
                result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'testagent','version':'1.0.0'}}
            elif method=='ping': result={}
            elif method=='tools/list': result={'tools':[TOOL]}
            elif method=='tools/call':
                if params.get('name')!='testagent': raise ValueError('Unknown tool')
                req=Request(os.environ['TESTAGENT_TOOL_URL'],data=json.dumps(params.get('arguments',{})).encode(),headers={
                    'Content-Type':'application/json','Authorization':'Bearer '+os.environ['TESTAGENT_TOOL_TOKEN']})
                # Human approval can wait indefinitely; server cancellation closes the operation.
                with urlopen(req,timeout=None) as response: data=json.load(response)
                if data.get('error'): result={'content':[{'type':'text','text':data['error']}],'isError':True}
                else: result={'content':[{'type':'text','text':json.dumps(data['result'],ensure_ascii=False)}]}
            else:
                print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32601,'message':'Unknown method'}}),flush=True);continue
            print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result},ensure_ascii=False),flush=True)
        except Exception as exc:
            if isinstance(request,dict) and 'id' in request:
                print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32603,'message':str(exc)}}),flush=True)

if __name__=='__main__': main()
