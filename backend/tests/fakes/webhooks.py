"""Signed-delivery fixtures for mocked PayPal webhook tests."""
HEADERS = {h:'test' for h in ('PAYPAL-TRANSMISSION-ID','PAYPAL-TRANSMISSION-TIME','PAYPAL-TRANSMISSION-SIG','PAYPAL-CERT-URL','PAYPAL-AUTH-ALGO')}

def event(order_id=None,status='COMPLETED',event_id='EV-1',**resource):
    return {'id':event_id,'event_type':'PAYMENT.CAPTURE.'+status,'resource_type':'capture','summary':'Payment status changed',
            'resource':{'id':'CAP1','status':status,'custom_id':order_id,'amount':{'value':'25.00','currency_code':'MYR'},
                        'payer':{'name':'Private Person','email_address':'private@example.com'},**resource}}



def send(h,body,headers=HEADERS):
    return h.client.post('/api/v1/webhooks/paypal',json=body,headers=headers)
