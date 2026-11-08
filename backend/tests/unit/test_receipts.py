"""The receipt is persisted cart/payment truth; retailer fulfillment is explicitly simulated."""
from tests.fakes.app import make_harness,tool_payload
from tests.fakes.approvals import create_order


def test_device_receipt_has_saved_items_and_never_claims_retailer_submission():
    h=make_harness()
    token,_,uid=h.new_device()
    oid=create_order(h,token)
    route=f'/api/v1/orders/{oid}/receipt'
    headers={'Authorization':f'Bearer {token}'}
    response=h.client.get(route,headers=headers)
    assert response.status_code==200
    receipt=response.json()
    assert receipt['order_id']==oid and receipt['status']=='awaiting_device_approval'
    assert receipt['items'][0]['name']=='Fresh milk 1L' and receipt['items'][0]['qty']==2
    assert receipt['merchant']=='Grocer Demo'
    assert receipt['payment_environment']=='sandbox'
    assert receipt['fulfillment_mode']=='simulated' and receipt['retailer_order_status']=='not_submitted'
    assert h.client.get(route).status_code==401
    other,_,_=h.new_device(uid)
    assert h.client.get(route,headers={'Authorization':f'Bearer {other}'}).status_code==404
    foreign,_,_=h.new_device()
    assert h.client.get(route,headers={'Authorization':f'Bearer {foreign}'}).status_code==404
    assert h.client.get('/api/v1/orders/no/receipt',headers=headers).status_code==404
    forbidden={'token','nonce','canonical','cart_sha256','provider_capture_id','vault_token_reference'}
    assert not forbidden.intersection(receipt)


def test_receipt_reports_paid_only_from_the_persisted_order_status():
    h=make_harness()
    token,_,_=h.new_device()
    oid=create_order(h,token)
    for status in ['APPROVED','PAYMENT_PENDING','PAID']:
        h.store.set_order_status(oid,status)
    response=h.client.get(f'/api/v1/orders/{oid}/receipt',headers={'Authorization':f'Bearer {token}'})
    assert response.status_code==200 and response.json()['status']=='paid'
    assert response.json()['items'][0]['line_total']=='RM13.80'
    assert response.json()['retailer_order_status']=='not_submitted'
