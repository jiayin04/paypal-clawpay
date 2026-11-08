import uuid
import pytest
from tests.fakes.app import make_harness,tool_payload
from tests.fakes.approvals import create_order as _create_order
FORBIDDEN = {'token','token_hash','llm_token_hash','vault_token_reference','provider_customer_reference','payload','nonce','canonical','public_key','idempotency_key','provider_order_id','provider_capture_id','details','actor_id'}

def assert_safe(value):
    if isinstance(value,dict):
        assert not (FORBIDDEN & value.keys()),value
        for v in value.values():
            assert_safe(v)
    elif isinstance(value,list):
        for v in value:
            assert_safe(v)

def test_all_dashboard_routes_scope_two_users_and_hide_columns():
    h=make_harness()
    token,did,a=h.new_device()
    token_b,did_b,b=h.new_device()
    oid=_create_order(h,token)
    oid_b=_create_order(h,token_b)
    cid=str(uuid.uuid4()); cid_b=str(uuid.uuid4())
    for uid,conv,order,device in [(a,cid,oid,did),(b,cid_b,oid_b,did_b)]:
        h.store.conversations[conv]={'id':conv,'user_id':uid,'device_id':device,'source':'esp_claw','status':'active','created_at':h.clock.utc_now(),'updated_at':h.clock.utc_now()}
        h.store.conversation_events.append({'id':str(uuid.uuid4()),'conversation_id':conv,'order_id':order,'event_type':'tool.called','summary':{'tool':'create_order','token':'hidden'},'payload':{'secret':'hidden'},'created_at':h.clock.utc_now()})
        h.store.a2a_messages.append({'id':str(uuid.uuid4()),'user_id':uid,'order_id':order,'summary':{'status':'ok','payload':'hidden'},'payload':{'secret':'hidden'},'created_at':h.clock.utc_now()})
        h.store._audit(uid,'user',uid,'test.event','order',order,{'private':'hidden'})
    paths=['/api/v1/orders','/api/v1/orders/'+oid,'/api/v1/orders/'+oid+'/timeline','/api/v1/orders/'+oid+'/a2a-messages',
           '/api/v1/conversations','/api/v1/conversations/'+cid+'/events','/api/v1/spending/summary','/api/v1/audit-events','/api/v1/devices','/api/v1/payment-methods']
    headers={'Authorization':'Bearer '+h.jwt(a)}
    for path in paths:
        r=h.client.get(path,headers=headers)
        assert r.status_code==200,(path,r.text)
        assert_safe(r.json())
        assert b not in r.text and oid_b not in r.text and cid_b not in r.text and did_b not in r.text
        assert h.client.get(path).status_code==401,path
    for path in ['/api/v1/orders/'+oid_b,'/api/v1/orders/'+oid_b+'/timeline','/api/v1/orders/'+oid_b+'/a2a-messages','/api/v1/conversations/'+cid_b+'/events']:
        assert h.client.get(path,headers=headers).status_code==404
    mcp=tool_payload(h.call_tool(token,'get_spending_summary',{}))[1]
    assert h.client.get('/api/v1/spending/summary',headers=headers).json()==mcp
    events=h.client.get('/api/v1/orders/'+oid+'/timeline',headers=headers).json()['events']
    assert {'conversation_events','a2a_messages','audit_logs'} <= {r['source'] for r in events}
    times=[r['created_at'] for r in events]
    assert times==sorted(times,reverse=True)
    # Missing publishable key consistently precedes JWT checks.
    h.settings.supabase_publishable_key=None
    for path in paths:
        assert h.client.get(path,headers=headers).json()['error']['code']=='dashboard_not_configured'

@pytest.mark.parametrize('suffix',['?limit=0','?limit=101','?limit=no','?before=invalid'])
def test_dashboard_pagination_validation(suffix):
    h=make_harness(); token,_,uid=h.new_device(); oid=_create_order(h,token)
    headers={'Authorization':'Bearer '+h.jwt(uid)}
    for path in ['/api/v1/orders','/api/v1/orders/'+oid+'/timeline','/api/v1/orders/'+oid+'/a2a-messages','/api/v1/conversations','/api/v1/conversations/'+str(uuid.uuid4())+'/events','/api/v1/audit-events']:
        assert h.client.get(path+suffix,headers=headers).status_code==422

def test_before_keyset_excludes_newer_rows():
    h=make_harness(); token,_,uid=h.new_device()
    first=_create_order(h,token); h.clock.advance(61); second=_create_order(h,token)
    headers={'Authorization':'Bearer '+h.jwt(uid)}
    rows=h.client.get('/api/v1/orders',params={'before':h.store.orders[second]['created_at'].isoformat(),'limit':1},headers=headers).json()['orders']
    assert [r['id'] for r in rows]==[first]
