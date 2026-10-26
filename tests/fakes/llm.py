import json

import httpx


class FakeLLM:
    def __init__(self):
        self.calls=[]
        self.status=200
        self.tokens=7
        self.error='Schema error'
        self.timeout=False
        self.stream = b': comment\n\ndata: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: {"choices":[],"usage":{"total_tokens":7}}\n\ndata: [DONE]\n\n'
    def handle(self, request):
        self.calls.append((dict(request.headers),json.loads(request.content)))
        if self.timeout:
            raise httpx.ReadTimeout('timeout',request=request)
        headers={'set-cookie':'bad=1','x-request-id':'secret','openai-organization':'org-1'}
        if self.status != 200:
            return httpx.Response(self.status,json={'error':{'message':self.error}},headers=headers)
        if self.calls[-1][1].get('stream'):
            return httpx.Response(200,content=self.stream,headers=headers)
        return httpx.Response(200,json={'model':'provider-model','choices':[],'usage':{'total_tokens':self.tokens}},headers=headers)
    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
