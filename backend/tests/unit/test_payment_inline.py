from fastapi.testclient import TestClient
from app.main import create_app
from tests.fakes.flow import setup_flow,create_and_sign,approve

def test_inline_oauth_timeout_leaves_unsent_attempt():
    h,t,uid,key,d=setup_flow()
    t.oauth_timeout=True
    oid,body=create_and_sign(h,key,d)
    r=approve(h,d,oid,body)
    assert r.status_code==200 and r.json()['status']=='payment_pending'
    attempt=next(iter(h.store.payment_attempts.values()))
    assert attempt['first_sent_at'] is None and attempt['status']=='initiated'
    assert h.store.spending_records[0]['status']=='reserved'
    assert not [c for c in t.calls if c.path=='/v2/checkout/orders']

def test_lifespan_background_disabled():
    h,t,uid,key,d=setup_flow()
    async def unexpected(**kwargs):
        raise AssertionError('Background work must be disabled')
    h.services.reconciliation.run_forever=unexpected
    with TestClient(create_app(h.settings,services=h.services,start_background=False)) as client:
        assert client.get('/health').status_code==200
