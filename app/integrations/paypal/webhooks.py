"""Signature verification preserves the exact received webhook event bytes."""
import json

async def verify_webhook(client, *, headers, raw_body, webhook_id):
    headers = {k.lower(): v for k,v in headers.items()}
    fields = {name: headers['paypal-'+name.replace('_','-')] for name in
              ('auth_algo','cert_url','transmission_id','transmission_sig','transmission_time')}
    fields['webhook_id'] = webhook_id
    body = json.dumps(fields, separators=(',',':')).encode()[:-1] + b',"webhook_event":' + raw_body + b'}'
    response = await client.request('POST','/v1/notifications/verify-webhook-signature',
                                    content=body, headers={'Content-Type':'application/json'})
    return response.json().get('verification_status') == 'SUCCESS'
