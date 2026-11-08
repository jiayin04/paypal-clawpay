import pytest
from tests.fakes.payments import make_payment_service as _setup_service, seed_order_and_attempt as _seed_order_and_attempt
from tests.fakes.payments import make_reconciler as reconciler

@pytest.mark.anyio
async def test_never_sent_retries_before_window_and_releases_after():
    svc,s,t,c=_setup_service(); oid,att=_seed_order_and_attempt(s)
    t.oauth_status=401
    c.advance(61)
    await reconciler(svc,s).run_once()
    assert s.payment_attempts[att.id]['first_sent_at'] is None
    assert s.spending_records[0]['status']=='reserved'
    c.advance(901)
    await reconciler(svc,s).run_once()
    assert s.spending_records[0]['status']=='released' and s.orders[oid]['status']=='PAYMENT_FAILED'
    assert not [call for call in t.calls if call.path=='/v2/checkout/orders']

@pytest.mark.anyio
async def test_replay_decline_does_not_release_and_exhausts():
    svc,s,t,c=_setup_service(); oid,att=_seed_order_and_attempt(s,first_sent_at=s.now())
    t.orders_mode='instrument_declined'
    c.advance(61)
    await reconciler(svc,s).run_once()
    assert s.orders[oid]['status']=='REQUIRES_RECONCILIATION'
    assert s.spending_records[0]['status']=='reserved'
    c.advance(901)
    await reconciler(svc,s).run_once()
    assert s.payment_attempts[att.id]['exhausted_at']
    calls=len(t.calls)
    c.advance(61); await reconciler(svc,s).run_once()
    assert len(t.calls)==calls

@pytest.mark.anyio
async def test_known_provider_get_capture_and_expiry_sweep():
    svc,s,t,c=_setup_service(); oid,att=_seed_order_and_attempt(s)
    s.payment_attempts[att.id].update(provider_order_id='ORD-001',first_sent_at=s.now())
    t.custom_routes['/v2/checkout/orders/ORD-001']=(200,{'id':'ORD-001','status':'APPROVED'})
    # Capture fixture must use the same provider id and amount.
    t.custom_routes['/v2/checkout/orders/ORD-001/capture']=(201,{'id':'ORD-001','status':'COMPLETED','purchase_units':[{'payments':{'captures':[{'id':'CAP1','status':'COMPLETED','amount':{'value':'25.00','currency_code':'MYR'}}]}}]})
    c.advance(61)
    await reconciler(svc,s).run_once()
    assert s.orders[oid]['status']=='PAID'
    assert not [call for call in t.calls if call.path=='/v2/checkout/orders']
    user=s.add_user(); device=s.add_device(user)
    expired=s.create_order(user_id=user,device_id=device,merchant_id=s.add_merchant(),total_minor=100,currency='MYR',status='AWAITING_APPROVAL',quote_expires_at=s.now())
    await reconciler(svc,s).run_once()
    assert s.orders[expired]['status']=='EXPIRED'

@pytest.mark.anyio
async def test_poll_timeout_no_state_change_and_amount_missing_uncertain():
    svc,s,t,c=_setup_service(); oid,att=_seed_order_and_attempt(s)
    s.payment_attempts[att.id].update(provider_order_id='ORD-001',first_sent_at=s.now())
    t.custom_routes['/v2/checkout/orders/ORD-001']=(500,{})
    c.advance(61); await reconciler(svc,s).run_once()
    assert s.payment_attempts[att.id]['status']=='initiated'
    assert s.spending_records[0]['status']=='reserved'
    t.custom_routes['/v2/checkout/orders/ORD-001']=(200,{'id':'ORD-001','status':'COMPLETED','purchase_units':[{'payments':{'captures':[{'id':'CAP1','status':'COMPLETED'}]}}]})
    c.advance(61); await reconciler(svc,s).run_once()
    assert s.orders[oid]['status']=='REQUIRES_RECONCILIATION'
