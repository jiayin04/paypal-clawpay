import base64
import json
import httpx
import pytest
from tests.fakes.app import make_harness, tool_payload
from tests.fakes.merchants import ScriptedMerchant

@pytest.mark.parametrize('change', ['revoke', 'expire'])
def test_passport_must_still_allow_order_when_quote_returns(change):
    state = {}
    async def merchant(request):
        h = state['h']
        if change == 'revoke':
            await h.services.passports.revoke(delegation_id=state['grant'], user_id=state['user'])
        else:
            h.clock.advance(2)
        return ScriptedMerchant('grocer', h.clock)(request)
    h = make_harness(agent_passport_signing_key=base64.urlsafe_b64encode(bytes(range(32))).rstrip(b'=').decode(),
                     merchant_transports={'grocer': httpx.MockTransport(merchant)})
    token, device, user = h.new_device()
    issued = h.client.post('/api/v1/delegations', headers={'Authorization': 'Bearer '+h.jwt(user)},
        json={'device_id':device,'merchants':['grocer'],'max_order_minor':5000,'expires_in_hours':1})
    assert issued.status_code == 201
    state.update(h=h, grant=issued.json()['id'], user=user)
    if change == 'expire':
        h.clock.advance(3599)
    error, result = tool_payload(h.call_tool(token, 'create_order', {
        'merchant_id':'grocer','items':[{'sku':'GRC-MILK-1L','qty':1}]}))
    assert error and result['error']=='delegation_denied', (
        f'{change}: order accepted after permission ended; '
        f'order_count={len(h.store.orders)}, receipt_decision={h.store.agent_receipts[-1]["decision"]}')
    assert not h.store.orders
    assert h.store.agent_receipts[-1]["decision"] == "denied"
    assert h.store.agent_receipts[-1]["reason"] == ("delegation_revoked" if change == "revoke" else "delegation_expired")


@pytest.mark.parametrize("had_grant", [False, True])
def test_new_or_replaced_grant_during_quote_requires_retry(had_grant):
    state = {}
    async def merchant(request):
        h = state["h"]
        if had_grant:
            await h.services.passports.revoke(delegation_id=state["grant"], user_id=state["user"])
        await h.services.passports.create(user_id=state["user"], device_id=state["device"],
                                         merchants=["grocer"], max_order_minor=500, expires_in_hours=1)
        return ScriptedMerchant("grocer", h.clock)(request)
    h = make_harness(agent_passport_signing_key=base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode(),
                     merchant_transports={"grocer": httpx.MockTransport(merchant)})
    token, device, user = h.new_device()
    state.update(h=h, device=device, user=user)
    if had_grant:
        issued = h.client.post("/api/v1/delegations", headers={"Authorization": "Bearer " + h.jwt(user)},
                              json={"device_id": device, "merchants": ["grocer"], "max_order_minor": 5000})
        state["grant"] = issued.json()["id"]
    error, result = tool_payload(h.call_tool(token, "create_order", {
        "merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 1}]}))
    assert error and result["error"] == "delegation_denied"
    assert not h.store.orders
    assert h.store.agent_receipts[-1]["decision"] == "denied"
    assert h.store.agent_receipts[-1]["reason"] == "delegation_changed"


def test_revocation_at_rpc_boundary_is_not_missed_by_preflight_checks():
    h = make_harness(agent_passport_signing_key=base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode())
    token, device, user = h.new_device()
    issued = h.client.post("/api/v1/delegations", headers={"Authorization": "Bearer " + h.jwt(user)},
                          json={"device_id": device, "merchants": ["grocer"], "max_order_minor": 5000})
    assert issued.status_code == 201
    h.store.before_rpc("create_order_from_quote", lambda store: store.delegations[0].update(revoked_at=store.now()))
    error, result = tool_payload(h.call_tool(token, "create_order", {
        "merchant_id": "grocer", "items": [{"sku": "GRC-MILK-1L", "qty": 1}]}))
    assert error and result["error"] == "delegation_denied"
    assert not h.store.orders
    assert h.store.agent_receipts[-1]["reason"] == "delegation_revoked"
