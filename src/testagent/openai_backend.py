"""OpenAI-compatible Chat Completions and models, no SDK or automatic retries."""
import http.client
import json
import ssl
import threading
from urllib.parse import urlsplit
from .core import DomainError
from .ssh import Cancelled
from .tools import TOOL


class OpenAIBackend:
    def __init__(self, profile, key, cancelled, emit):
        self.config,self.key,self.cancelled,self.emit=profile['config'],key,cancelled,emit
        self.connection=None
        self.lock=threading.Lock()

    def request(self, endpoint, payload=None):
        url=urlsplit(self.config['base_url'].rstrip('/'))
        base=url.path.rstrip('/') or '/v1'
        timeout=float(self.config.get('timeout',120))
        if url.scheme=='https':
            context=ssl.create_default_context(cafile=self.config.get('ca_file') or None)
            conn=http.client.HTTPSConnection(url.hostname,url.port,timeout=timeout,context=context)
        else: conn=http.client.HTTPConnection(url.hostname,url.port,timeout=timeout)
        with self.lock: self.connection=conn
        headers={'Content-Type':'application/json','Accept':'application/json'}
        if self.key: headers['Authorization']='Bearer '+self.key
        if self.cancelled.is_set(): raise Cancelled('任务已停止')
        conn.request('POST' if payload is not None else 'GET',base+endpoint,
                     body=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None,headers=headers)
        response=conn.getresponse()
        if response.status>=400:
            body=response.read(4096).decode(errors='replace');conn.close()
            raise DomainError(f'模型 API 返回 {response.status}：{body}')
        return response

    def models(self):
        response=self.request('/models')
        try: return [x['id'] for x in json.load(response).get('data',[]) if isinstance(x.get('id'),str)]
        finally: self.close()

    def completion(self,messages,model,tools=True):
        payload={'model':model,'messages':messages,'stream':True}
        if tools: payload['tools']=[{'type':'function','function':{'name':TOOL['name'],'description':TOOL['description'],'parameters':TOOL['inputSchema']}}]
        if self.config.get('max_tokens'): payload['max_tokens']=int(self.config['max_tokens'])
        response=self.request('/chat/completions',payload)
        try:
            if 'text/event-stream' not in response.getheader('Content-Type',''):
                result=json.load(response)
                msg=result['choices'][0]['message']
                if msg.get('content'): self.emit('agent.message',{'text':msg['content']})
                return msg
            content=''; calls={}; finish=None
            while True:
                if self.cancelled.is_set(): raise Cancelled('任务已停止')
                line=response.readline()
                if not line: break
                if not line.startswith(b'data:'): continue
                raw=line[5:].strip()
                if raw==b'[DONE]': break
                event=json.loads(raw)
                if event.get('error'): raise DomainError(str(event['error']))
                for choice in event.get('choices',[]):
                    delta=choice.get('delta',{})
                    if delta.get('content'):
                        content+=delta['content'];self.emit('agent.chunk',{'text':delta['content']})
                    for call in delta.get('tool_calls',[]):
                        current=calls.setdefault(call['index'],{'id':'','type':'function','function':{'name':'','arguments':''}})
                        if call.get('id'): current['id']=call['id']
                        for key in ('name','arguments'):
                            if call.get('function',{}).get(key): current['function'][key]+=call['function'][key]
                    if choice.get('finish_reason'): finish=choice['finish_reason']
            if finish not in ('stop','tool_calls'):
                raise DomainError('模型输出未正常结束：'+str(finish))
            message={'role':'assistant','content':content or None}
            if calls: message['tool_calls']=[calls[i] for i in sorted(calls)]
            return message
        finally: self.close()

    def run(self,prompt,model,gateway,messages):
        history=[{'role':'system','content':prompt}]
        for _ in range(int(self.config.get('max_turns',200))):
            if self.cancelled.is_set(): raise Cancelled('任务已停止')
            for text in messages(): history.append({'role':'user','content':text})
            response=self.completion(history,model)
            history.append(response)
            calls=response.get('tool_calls',[])
            if not calls:
                if gateway.finished or gateway.readonly: return response.get('content','')
                raise DomainError('模型结束了会话，但未调用 finish；任务不自动续接')
            for call in calls:
                if call['function']['name']!='testagent': raise DomainError('模型调用未知工具')
                result=gateway(json.loads(call['function']['arguments']))
                history.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(result,ensure_ascii=False)})
            if gateway.finished: return response.get('content','') or '任务完成'
        raise DomainError('达到配置的最大模型轮数，任务已终止')

    def close(self):
        with self.lock:
            if self.connection:
                self.connection.close();self.connection=None

    cancel=close
