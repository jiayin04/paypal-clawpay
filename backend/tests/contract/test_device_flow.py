from tests.fakes.flow import setup_flow,create_and_sign,approve
from tests.fakes.app import tool_payload

def test_device_contract_software_p256_to_paid():
    h,paypal,uid,key,device=setup_flow()
    oid,body=create_and_sign(h,key,device)
    r=approve(h,device,oid,body)
    assert r.status_code==200 and r.json()['status']=='paid',r.text
    error,status=tool_payload(h.call_tool(device['device_token'],'get_order_status',{'order_id':oid}))
    assert not error and status['status']=='paid' and status['paid_at']
    assert approve(h,device,oid,body).status_code==409
    assert len([c for c in paypal.calls if c.path=='/v2/checkout/orders'])==1
