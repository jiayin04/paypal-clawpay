"""Opt-in sandbox credential/vault smoke: no charge or capture."""
import os
import uuid
import pytest
from app.core.config import Settings
from app.integrations.paypal import PayPalClient,PayPalService
_ENABLED = os.getenv('CLAWPAY_LIVE_SMOKE') == '1'
_ID = os.getenv('CLAWPAY_SMOKE_PAYPAL_CLIENT_ID')
_SECRET = os.getenv('CLAWPAY_SMOKE_PAYPAL_CLIENT_SECRET')

@pytest.mark.skipif(not (_ENABLED and _ID and _SECRET),reason='Explicit sandbox smoke opt-in and isolated credentials required')
@pytest.mark.anyio
async def test_paypal_live_oauth_and_setup_only():
    settings = Settings(_env_file=None,paypal_env='sandbox',paypal_client_id=_ID,paypal_client_secret=_SECRET)
    gateway=PayPalService(PayPalClient(settings))
    await gateway.ensure_access_token()
    token=await gateway.create_setup_token(return_url='https://example.com/return',cancel_url='https://example.com/cancel',request_id='smoke-'+str(uuid.uuid4()))
    assert token.id and token.approve_url
