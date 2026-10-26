"""Verified, deduplicated PayPal notifications; polling remains primary."""
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ApiError, RateLimited, ServiceUnavailable
from app.core.security import SlidingWindowLimiter
from app.integrations.paypal import classify
from app.integrations.paypal.gateway import PayPalGateway
from app.integrations.paypal.orders import parse_provider_order
from app.repositories.errors import with_rpc_retry
from app.repositories.ports.payments import PaymentAttemptRecord, PaymentRepository
from app.repositories.ports.webhooks import WebhookRepository
from app.services.audit_service import AuditService


HEADERS = ('paypal-transmission-id','paypal-transmission-time','paypal-transmission-sig','paypal-cert-url','paypal-auth-algo')
EVENTS = {'PAYMENT.CAPTURE.'+s for s in ('COMPLETED','PENDING','DENIED','DECLINED','REFUNDED','REVERSED')} | {'CHECKOUT.ORDER.COMPLETED'}

class WebhookService:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        repo: WebhookRepository,
        payments: PaymentRepository,
        paypal: PayPalGateway,
        audit: AuditService,
    ) -> None:
        self.settings,self.clock,self.repo,self.payments,self.paypal,self.audit = settings,clock,repo,payments,paypal,audit
        self.payments = with_rpc_retry(payments)
        self.failures = SlidingWindowLimiter(60, monotonic=clock.monotonic)
        self.audit_limit = SlidingWindowLimiter(1, monotonic=clock.monotonic)

    async def receive(self, headers: Mapping[str, str], raw_body: bytes, ip: str) -> dict[str, str]:
        retry = await self.failures.is_over(ip)
        if retry is not None:
            raise RateLimited(retry)
        if not self.settings.paypal_configured or not self.settings.paypal_webhook_id:
            raise ServiceUnavailable('PayPal webhooks are not configured.', 'webhooks_not_configured')
        async def invalid(code='invalid_webhook'):
            await self.failures.record(ip)
            raise ApiError(400,code,'Invalid PayPal webhook.')
        if any(not headers.get(h) or len(headers[h]) > 500 for h in HEADERS):
            await invalid()
        try:
            event = json.loads(raw_body)
        except (ValueError, UnicodeError):
            await invalid()
        if not isinstance(event,dict) or not all(isinstance(event.get(k),str) and event[k] for k in ('id','event_type')):
            await invalid()
        try:
            valid = await self.paypal.verify_webhook(headers=headers,raw_body=raw_body,webhook_id=self.settings.paypal_webhook_id)
        except Exception:
            raise ServiceUnavailable('PayPal verification is unavailable.') from None
        if not valid:
            if await self.audit_limit.acquire(ip) is None:
                await self.audit.record('webhook.signature_failed',actor_type='system')
            await invalid('invalid_signature')
        resource = event.get('resource')
        resource = resource if isinstance(resource,dict) else {}
        payload = {'event_type':event['event_type'], 'resource_type':event.get('resource_type'),
                   'resource_id':resource.get('id'), 'custom_id':resource.get('custom_id'),
                   'status':resource.get('status'), 'summary':event.get('summary')}
        row = await self.repo.persist_event(event['id'],event['event_type'],payload)
        if row.get('processed_at'):
            return {'status':'duplicate'}
        if event['event_type'] in EVENTS:
            await self.apply(event['event_type'],resource)
        await self.repo.mark_processed(row['id'],self.clock.utc_now())
        await self.audit.record('webhook.processed',actor_type='system',entity_type='webhook',entity_id=row['id'])
        return {'status':'processed'}

    async def lookup(self, kind: str, r: dict[str, Any]) -> PaymentAttemptRecord | None:
        async def find(field, val):
            return await self.repo.find_attempt(field,val) if isinstance(val,str) and val else None
        async def custom():
            try:
                value = str(UUID(r.get('custom_id','')))
            except (ValueError, TypeError, AttributeError):
                return None
            return await find('order_id',value)
        if kind == 'CHECKOUT.ORDER.COMPLETED':
            return await find('provider_order_id',r.get('id'))
        if kind.endswith(('REFUNDED','REVERSED')):
            for link in r.get('links',[]):
                if isinstance(link,dict) and link.get('rel') == 'up':
                    cap = urlsplit(link.get('href','')).path.rstrip('/').rsplit('/',1)[-1]
                    if re.fullmatch('[A-Z0-9]{1,64}',cap):
                        found = await find('provider_capture_id',cap)
                        if found:
                            return found
            found = await custom()
            if found:
                return found
            return await find('provider_capture_id',r.get('id')) if kind.endswith('REVERSED') else None
        found = await find('provider_capture_id',r.get('id'))
        if found:
            return found
        supplementary = r.get('supplementary_data') or {}
        related = supplementary.get('related_ids') or {}
        return await find('provider_order_id',related.get('order_id')) or await custom()

    async def apply(self, kind: str, resource: dict[str, Any]) -> None:
        attempt = await self.lookup(kind,resource)
        if not attempt:
            return
        if kind.endswith(('REFUNDED','REVERSED')):
            await self.audit.record('payment.refund_recorded', user_id=attempt.user_id,actor_type='system',entity_type='order',entity_id=attempt.order_id)
            return
        if kind == 'CHECKOUT.ORDER.COMPLETED':
            outcome = parse_provider_order(resource)
            if not outcome.capture_id:
                return
        else:
            capture = dict(resource)
            capture['status'] = {'DENIED':'FAILED','DECLINED':'DECLINED'}.get(kind.rsplit('.',1)[-1],kind.rsplit('.',1)[-1])
            outcome = parse_provider_order({'id':attempt.provider_order_id or resource.get('supplementary_data',{}).get('related_ids',{}).get('order_id',''),
                                            'status':'COMPLETED','purchase_units':[{'payments':{'captures':[capture]}}]})
        result = classify(outcome,source='webhook',attempt=attempt)
        if result.failure_code in ('amount_mismatch','currency_mismatch'):
            await self.audit.record('payment.conflicting_result',user_id=attempt.user_id,actor_type='system',entity_type='order',entity_id=attempt.order_id,
                                    details={'reason':result.failure_code})
        await self.payments.apply_payment_result(attempt_id=attempt.id,classification=result.status,provider_order_id=result.provider_order_id or None,
            provider_capture_id=result.provider_capture_id,provider_status=result.provider_status,failure_code=result.failure_code,source='webhook')
