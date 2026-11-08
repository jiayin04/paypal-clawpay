import pytest
from tests.fakes.flow import setup_flow,create_and_sign,approve,CART
from tests.fakes.app import tool_payload
from tests.fakes.webhooks import send, event

def test_full_flow_explicit_capture_dashboard_spending_and_webhook_dedup():
    h,paypal,uid,key,device=setup_flow('approved_no_capture')
    oid,body=create_and_sign(h,key,device)
    r=approve(h,device,oid,body)
    assert r.status_code==200 and r.json()['status']=='paid',r.text
    error,status=tool_payload(h.call_tool(device['device_token'],'get_order_status',{'order_id':oid}))
    assert not error and status['status']=='paid' and status['paid_at']
    error,spending=tool_payload(h.call_tool(device['device_token'],'get_spending_summary',{}))
    assert not error and spending['committed_minor']==1380 and spending['reserved_minor']==0
    headers={'Authorization':'Bearer '+h.jwt(uid)}
    detail=h.client.get('/api/v1/orders/'+oid,headers=headers).json()
    assert detail['status']=='paid' and detail['approvals'][0]['verification_status']=='verified'
    timeline=h.client.get('/api/v1/orders/'+oid+'/timeline',headers=headers).json()['events']
    assert {'approvals','payment_attempts','a2a_messages','audit_logs'} <= {r['source'] for r in timeline}
    e=event(oid,id='CAP-EXPLICIT-001',amount={'value':'13.80','currency_code':'MYR'})
    assert send(h,e).status_code==200
    assert send(h,e).json()['status']=='duplicate'
    creates=[c for c in paypal.calls if c.path=='/v2/checkout/orders']
    captures=[c for c in paypal.calls if c.path.endswith('/capture')]
    assert len(creates)==len(captures)==1
    assert creates[0].headers['paypal-request-id']+'-capture'==captures[0].headers['paypal-request-id']

@pytest.mark.parametrize('mode,expected,spending',[('declined','payment_failed','released'),('completed','paid','committed')])
def test_payment_outcomes_and_approval_replay(mode,expected,spending):
    h,t,uid,key,d=setup_flow(mode)
    oid,body=create_and_sign(h,key,d)
    assert approve(h,d,oid,body).json()['status']==expected
    assert approve(h,d,oid,body).status_code==409
    assert h.store.spending_records[0]['status']==spending
    assert len([c for c in t.calls if c.path=='/v2/checkout/orders'])==1

@pytest.mark.anyio
async def test_capture_timeout_then_reconciler_get_paid_one_create():
    h,t,uid,key,d=setup_flow('approved_no_capture')
    t.capture_mode='timeout'
    oid,body=create_and_sign(h,key,d)
    assert approve(h,d,oid,body).json()['status']=='payment_pending'
    h.clock.advance(61)
    await h.services.reconciliation.run_once()
    assert h.store.orders[oid]['status']=='PAID'
    assert h.store.spending_records[0]['status']=='committed'
    assert len([c for c in t.calls if c.path=='/v2/checkout/orders'])==1

@pytest.mark.parametrize('policy',['cap','phone-create','phone-approval','revoked'])
def test_policy_and_revocation_failure_paths(policy):
    h,t,uid,key,d=setup_flow()
    if policy=='cap':
        h.store.add_spending(uid,50000)
        error,data=tool_payload(h.call_tool(d['device_token'],'create_order',CART))
        assert error and data['error']=='daily_limit_exceeded'
    elif policy=='phone-create':
        h.settings.phone_approval_threshold_minor=1000
        error,data=tool_payload(h.call_tool(d['device_token'],'create_order',CART))
        assert error and data['error']=='phone_approval_unavailable'
    else:
        oid,body=create_and_sign(h,key,d)
        if policy=='phone-approval':
            h.settings.phone_approval_threshold_minor=1000
            r=approve(h,d,oid,body)
            assert r.status_code==409 and r.json()['error']['code']=='phone_approval_required'
        else:
            headers={'Authorization':'Bearer '+h.jwt(uid)}
            assert h.client.delete('/api/v1/devices/'+d['device_id'],headers=headers).status_code==200
            assert approve(h,d,oid,body).status_code==401
    assert not [c for c in t.calls if c.path=='/v2/checkout/orders']
