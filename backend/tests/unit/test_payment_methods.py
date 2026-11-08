from urllib.parse import urlsplit,parse_qs
import hashlib
import pytest
from tests.fakes.app import make_harness
from tests.fakes.paypal import FakePayPalTransport

def setup():
    t=FakePayPalTransport()
    h=make_harness(paypal_transport=t,paypal_client_id='fake',paypal_client_secret='fake-secret',public_base_url='https://clawpay.test')
    uid=h.new_user(payment_method=False)
    headers={'Authorization':'Bearer '+h.jwt(uid)}
    return h,t,uid,headers

def begin(h,t,headers):
    r=h.client.post('/api/v1/payment-methods/paypal/setup',headers=headers)
    assert r.status_code==201,r.text
    call=next(c for c in reversed(t.calls) if c.path=='/v3/vault/setup-tokens')
    url=call.body['payment_source']['paypal']['experience_context']['return_url']
    return r.json(),parse_qs(urlsplit(url).query)['state'][0]

def test_link_uses_server_reference_and_hashed_state():
    h,t,uid,headers=setup()
    result,state=begin(h,t,headers)
    pm=h.store.payment_methods[0]
    assert pm['setup_state_hash']==hashlib.sha256(state.encode()).hexdigest()
    assert 'state_token' not in pm and pm['setup_token_reference']
    r=h.client.get('/api/v1/payment-methods/paypal/return',params={'state':state,'token':'attacker-token'})
    assert r.status_code==200 and 'linked' in r.text
    assert r.headers['cache-control']=='no-store' and r.headers['referrer-policy']=='no-referrer'
    get=next(c for c in t.calls if c.method=='GET' and '/setup-tokens/' in c.path)
    assert get.path.endswith(pm['setup_token_reference']) and 'attacker-token' not in get.path
    assert pm['status']=='active' and pm['setup_state_hash'] is None
    assert pm['payer_email_masked']=='j***@example.com'
    assert 'john@example.com' not in str(pm)
    assert h.client.get('/api/v1/payment-methods/paypal/return',params={'state':state}).status_code==400
    data=h.client.get('/api/v1/payment-methods',headers=headers)
    assert data.status_code==200 and 'vault_token_reference' not in data.text
    r=h.client.delete('/api/v1/payment-methods/'+pm['id'],headers=headers)
    assert r.status_code==200,r.text
    assert h.client.delete('/api/v1/payment-methods/'+pm['id'],headers=headers).status_code==200

@pytest.mark.parametrize('cancelled',[True,False])
def test_cancel_and_expiry(cancelled):
    h,t,uid,headers=setup()
    _,state=begin(h,t,headers)
    if not cancelled:
        h.clock.advance(1801)
    r=h.client.get('/api/v1/payment-methods/paypal/return',params={'state':state,'cancelled':'1' if cancelled else '0'})
    assert r.status_code==(200 if cancelled else 400)
    if cancelled:
        assert h.store.payment_methods[0]['status']=='failed'

def test_setup_rate_auth_return_limit_and_conflict_retry():
    h,t,uid,headers=setup()
    assert h.client.post('/api/v1/payment-methods/paypal/setup').status_code==401
    _,state=begin(h,t,headers)
    h.store.fail_next('activate_payment_method','23505')
    assert h.client.get('/api/v1/payment-methods/paypal/return',params={'state':state}).status_code==200
    for _ in range(4):
        begin(h,t,headers)
    assert h.client.post('/api/v1/payment-methods/paypal/setup',headers=headers).status_code==429
    for _ in range(10):
        assert h.client.get('/api/v1/payment-methods/paypal/return?state=invalid').status_code==400
    assert h.client.get('/api/v1/payment-methods/paypal/return?state=invalid').status_code==429
