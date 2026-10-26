import anyio
import pytest
from tests.fakes.payments import make_payment_service as _setup_service, seed_order_and_attempt as _seed_order_and_attempt
from tests.fakes.payments import make_reconciler as reconciler

@pytest.mark.anyio
async def test_lease_minimum_age_and_competing_executors():
    svc,s,t,c=_setup_service(); oid,att=_seed_order_and_attempt(s)
    assert await s.payments.claim_reconcilable_attempt(oid) is None
    c.advance(31)
    assert await s.payments.claim_reconcilable_attempt(oid) is None
    c.advance(30)
    async with anyio.create_task_group() as tg:
        tg.start_soon(svc.reconcile_order,oid)
        tg.start_soon(reconciler(svc,s).run_once)
    assert len([call for call in t.calls if call.path=='/v2/checkout/orders'])==1
    assert s.orders[oid]['status']=='PAID'
