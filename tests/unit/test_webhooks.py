import json
import pytest
from tests.fakes.app import make_harness
from tests.fakes.paypal import FakePayPalTransport
from tests.fakes.payments import seed_order_and_attempt as _seed_order_and_attempt

from tests.fakes.webhooks import HEADERS, event, send

def setup():
    transport = FakePayPalTransport()
    h = make_harness(paypal_transport=transport,paypal_client_id='fake',paypal_client_secret='fake-secret',paypal_webhook_id='WH-1')
    return h,transport

def test_verification_raw_bytes_and_minimal_payload():
    h,t = setup()
    body = json.dumps(event(),indent=2).encode()
    r = h.client.post('/api/v1/webhooks/paypal',content=body,headers=HEADERS)
    assert r.status_code == 200, r.text
    verify = next(c for c in t.calls if 'verify-webhook' in c.path)
    assert b'"webhook_event":'+body+b'}' in verify.raw_body
    payload = h.store.webhook_events['EV-1']['payload']
    assert set(payload) == {'event_type','resource_type','resource_id','custom_id','status','summary'}
    assert 'private@example.com' not in json.dumps(payload)
    assert send(h,event()).json() == {'status':'duplicate'}

@pytest.mark.parametrize('body,headers', [([],HEADERS),({},HEADERS),(event(),{}),({'id':3,'event_type':'x'},HEADERS)])
def test_invalid_webhooks(body,headers):
    h,t = setup()
    assert send(h,body,headers).status_code == 400
    assert not h.store.webhook_events and not t.calls

def test_config_and_verification_errors():
    h = make_harness()
    assert send(h,event()).status_code == 503
    h,t = setup()
    t.webhook_verify_success = False
    for _ in range(60):
        assert send(h,event()).status_code == 400
    count = len(t.calls)
    t.webhook_verify_success = True
    assert send(h,event()).status_code == 429
    assert len(t.calls) == count and not h.store.webhook_events
    assert len(h.store.audit_events('webhook.signature_failed')) == 1
    h,t = setup()
    t.custom_routes['/v1/notifications/verify-webhook-signature'] = (500,{})
    assert send(h,event()).status_code == 503
    assert not h.store.webhook_events

@pytest.mark.anyio
async def test_ordering_duplicate_refund_and_reapply():
    h,t = setup()
    oid,att = _seed_order_and_attempt(h.store)
    assert send(h,event(oid)).status_code == 200
    assert h.store.orders[oid]['status'] == 'PAID'
    assert send(h,event(oid,'PENDING','EV-2')).status_code == 200
    assert h.store.orders[oid]['status'] == 'PAID'
    for status in ('REFUNDED','REVERSED'):
        assert send(h,event(None,status,'EV-'+status,id='REF1',links=[{'rel':'up','href':'https://api.sandbox.paypal.com/v2/payments/captures/CAP1'}])).status_code == 200
    assert len([r for r in h.store.spending_records if r['status'] == 'committed']) == 1
    assert len(h.store.audit_events('payment.refund_recorded')) == 2
    # A crash after persistence must reapply from the current verified body.
    h,t = setup()
    oid,att = _seed_order_and_attempt(h.store)
    await h.services.repos.webhooks.persist_event('EV-1','PAYMENT.CAPTURE.COMPLETED',{})
    assert send(h,event(oid)).status_code == 200
    assert h.store.orders[oid]['status'] == 'PAID'

@pytest.mark.parametrize('poll_first',[True,False])
@pytest.mark.anyio
async def test_reconciler_webhook_converge(poll_first):
    h,t = setup()
    oid,att = _seed_order_and_attempt(h.store)
    row = h.store.payment_attempts[att.id]
    row.update(provider_order_id='ORD-FAKE-001',first_sent_at=h.clock.utc_now())
    h.clock.advance(61)
    # Use the exact provider fixture capture identity and amount.
    t.custom_routes['/v2/checkout/orders/ORD-FAKE-001'] = (200, {'id':'ORD-FAKE-001','status':'COMPLETED','purchase_units':[{'payments':{'captures':[{'id':'CAP1','status':'COMPLETED','amount':{'value':'25.00','currency_code':'MYR'}}]}}]})
    if poll_first:
        await h.services.reconciliation.run_once()
    assert send(h,event(oid)).status_code == 200
    if not poll_first:
        await h.services.reconciliation.run_once()
    assert h.store.orders[oid]['status'] == 'PAID'
    assert len([s for s in h.store.spending_records if s['status'] == 'committed']) == 1

def test_amount_mismatch_exhausted_and_checkout_order():
    h,t = setup()
    oid,att = _seed_order_and_attempt(h.store)
    h.store.payment_attempts[att.id]['exhausted_at'] = h.clock.utc_now()
    assert send(h,event(oid,amount={'value':'0.01','currency_code':'MYR'})).status_code == 200
    assert h.store.orders[oid]['status'] == 'REQUIRES_RECONCILIATION'
    assert h.store.audit_events('payment.conflicting_result')
    assert send(h,event(oid,event_id='EV-2')).status_code == 200
    assert h.store.orders[oid]['status'] == 'PAID'
    h,t = setup()
    oid,att = _seed_order_and_attempt(h.store)
    h.store.payment_attempts[att.id]['provider_order_id']='ORD1'
    e = {'id':'CHECK1','event_type':'CHECKOUT.ORDER.COMPLETED','resource':{'id':'ORD1','status':'COMPLETED','purchase_units':[{'payments':{'captures':[event(oid)['resource']]}}]}}
    assert send(h,e).status_code == 200
    assert h.store.orders[oid]['status'] == 'PAID'


def test_reversed_custom_id_and_capture_fallback_and_body_limit():
    for custom in (True,False):
        h,t=setup(); oid,att=_seed_order_and_attempt(h.store)
        h.store.payment_attempts[att.id]['provider_capture_id']='CAP1'
        e=event(oid if custom else None,'REVERSED',id='unrelated' if custom else 'CAP1')
        assert send(h,e).status_code==200
        assert h.store.audit_events('payment.refund_recorded')
        assert h.store.spending_records[0]['status']=='reserved'
    h,t=setup()
    r=h.client.post('/api/v1/webhooks/paypal',content=b'x'*(256*1024+1),headers=HEADERS)
    assert r.status_code==413 and not t.calls
