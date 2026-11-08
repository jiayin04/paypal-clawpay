import pytest
from app.core.security import generate_llm_token,hash_token
from tests.fakes.app import make_harness,PEPPER
from tests.fakes.llm import FakeLLM

def setup(**settings):
    llm=FakeLLM()
    h=make_harness(llm_client=llm.client(),llm_proxy_upstream_base_url='https://llm.test/v1',llm_proxy_upstream_api_key='upstream-secret',llm_proxy_model='pinned',**settings)
    cpd,did,_=h.new_device()
    token=generate_llm_token()
    h.store.devices[did]['llm_token_hash']=hash_token(PEPPER,token)
    return h,llm,token,cpd,did

def send(h,token,**body):
    return h.client.post('/llm/v1/chat/completions',json={'messages':[{'role':'user','content':'private prompt'}],**body},headers={'Authorization':'Bearer '+token})

def test_model_allowlist_headers_and_clamps(caplog):
    h,llm,token,cpd,did=setup()
    r=send(h,token,model='attacker',max_tokens=99999,max_completion_tokens=88888,n=9,user='private',stream_options={'include_usage':False,'other':True})
    assert r.status_code==200,r.text
    headers,body=llm.calls[0]
    assert headers['authorization']=='Bearer upstream-secret' and token not in str(headers)
    assert body['model']=='pinned' and body['max_tokens']==body['max_completion_tokens']==1024
    assert 'n' not in body and 'user' not in body
    assert body['stream_options']=={'include_usage':False}
    assert set(r.headers) <= {'content-type','content-length','cache-control','date','server'}
    assert r.headers['cache-control']=='no-store' and r.json()['model']=='provider-model'
    assert 'private prompt' not in caplog.text
    assert send(h,cpd).status_code==401
    assert 'type' in send(h,cpd).json()['error']
    h.store.devices[did]['status']='revoked'
    assert send(h,token).status_code==401

@pytest.mark.parametrize('wants_usage',[True,False])
def test_stream_relay_and_budget(wants_usage):
    h,llm,token,_,_=setup(llm_proxy_daily_token_limit=7)
    r=send(h,token,stream=True,stream_options={'include_usage':wants_usage})
    assert r.status_code==200,r.text
    assert llm.calls[0][1]['stream_options']=={'include_usage':True}
    expected=llm.stream if wants_usage else llm.stream.replace(b'data: {"choices":[],"usage":{"total_tokens":7}}\n\n',b'')
    assert r.content==expected
    assert send(h,token).json()['error']['code']=='budget_exceeded'
    assert len(llm.calls)==1
    h.clock.advance(86400)
    assert send(h,token).status_code==200

@pytest.mark.parametrize('status,expected,code',[(401,502,'upstream_error'),(500,502,'upstream_error'),(429,429,'rate_limited'),(400,400,'invalid_request_error')])
def test_upstream_errors(status,expected,code):
    h,llm,token,_,_=setup()
    llm.status=status
    r=send(h,token)
    assert r.status_code==expected and r.json()['error']['code']==code
    assert r.json()['error'].get('type')
    if status==400:
        assert r.json()['error']['message']=='Schema error'
        llm.error='x'*301
        assert send(h,token).json()['error']['message']=='Invalid upstream request.'

def test_timeout_nonstream_budget_default_rate_and_body():
    h,llm,token,_,_=setup(llm_proxy_daily_token_limit=7)
    assert send(h,token).status_code==200
    assert llm.calls[0][1]['max_tokens']==1024
    assert send(h,token).json()['error']['code']=='budget_exceeded'
    h,llm,token,_,_=setup()
    llm.timeout=True
    assert send(h,token).status_code==502
    h,llm,token,_,_=setup(llm_proxy_requests_per_minute=1)
    assert send(h,token).status_code==200
    assert send(h,token).status_code==429
    h,llm,token,_,_=setup(llm_proxy_max_body_bytes=1024)
    assert send(h,token,messages=[{'content':'x'*1100}]).status_code==413
    assert not llm.calls

@pytest.mark.parametrize('body',[{'messages':[]},{'messages':'x'},{'stream_options':{'include_usage':'yes'}},{'max_tokens':True},{'max_tokens':-1}])
def test_bad_requests(body):
    h,llm,token,_,_=setup()
    assert send(h,token,**body).status_code==400
    assert not llm.calls

def test_not_configured_and_fallback_usage():
    h,llm,token,_,_=setup(llm_proxy_max_tokens=10,llm_proxy_daily_token_limit=10)
    h.services.llm.settings.llm_proxy_model=None
    assert send(h,token).json()['error']['code']=='proxy_not_configured'
    h.services.llm.settings.llm_proxy_model='pinned'
    llm.stream=b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n'
    assert send(h,token,stream=True).status_code==200
    assert send(h,token).json()['error']['code']=='budget_exceeded'


@pytest.mark.parametrize('wants_usage',[True,False])
def test_chunk_boundaries_preserve_stream_and_usage(wants_usage):
    import httpx
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in (b'data: {"choices":[1]}\n\n',b'data: {"cho',b'ices":[],"usage":{"total_tokens":7}}\n',b'\ndata: [DONE]\n\n'):
                yield part
    h,llm,token,_,_=setup(llm_proxy_daily_token_limit=7)
    def handler(request):
        return httpx.Response(200,stream=Chunks())
    h.services.llm.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response=send(h,token,stream=True,stream_options={'include_usage':wants_usage})
    expected=b'data: {"choices":[1]}\n\n'+(b'data: {"choices":[],"usage":{"total_tokens":7}}\n\n' if wants_usage else b'')+b'data: [DONE]\n\n'
    assert response.content==expected
    assert send(h,token).json()['error']['code']=='budget_exceeded'
