"""Full HTTP device flow, including real software signature and mocked PayPal."""
from tests.fakes.app import ADMIN_TOKEN, make_harness, tool_payload
from tests.fakes.approvals import (
    generate_p256 as _generate_p256,
    get_challenge as _get_challenge,
    sign_canonical as _sign_canonical,
)
from tests.fakes.paypal import FakePayPalTransport

CART={'merchant_id':'grocer','items':[{'sku':'GRC-MILK-1L','qty':2}]}

def setup_flow(mode='completed',**settings):
    paypal=FakePayPalTransport(); paypal.orders_mode=mode
    h=make_harness(paypal_transport=paypal,paypal_client_id='fake',paypal_client_secret='fake-secret',paypal_webhook_id='WH-1',**settings)
    uid=h.new_user(); private,pem=_generate_p256()
    r=h.client.post('/api/v1/devices/register',headers={'Authorization':'Bearer '+ADMIN_TOKEN},json={'user_id':uid,'device_name':'flow-device','public_key_pem':pem,'key_version':1})
    assert r.status_code==201,r.text
    device=r.json()
    return h,paypal,uid,private,device

def create_and_sign(h,private,device):
    error,search=tool_payload(h.call_tool(device['device_token'],'search_products',{'query':'milk','merchant_id':'grocer'}))
    assert not error and search['results']
    error,order=tool_payload(h.call_tool(device['device_token'],'create_order',CART))
    assert not error,order
    oid=order['order_id']; ch=_get_challenge(h,device['device_token'],oid)
    approval={'challenge_id':ch['challenge_id'],'device_id':device['device_id'],'key_version':ch['key_version'],'nonce':ch['nonce'],'decision':'approved','signature':_sign_canonical(private,ch['canonical'])}
    return oid,approval

def approve(h,device,oid,body):
    return h.client.post('/api/v1/orders/'+oid+'/gadget-approval',json=body,headers={'Authorization':'Bearer '+device['device_token']})
