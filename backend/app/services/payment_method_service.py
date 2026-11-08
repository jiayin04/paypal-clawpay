"""PayPal linking with hashed, expiring state and server-owned setup references."""
import logging
import secrets
from datetime import timedelta
from typing import Any

from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ApiError, Conflict, ServiceUnavailable
from app.core.security import SlidingWindowLimiter
from app.integrations.paypal.gateway import PayPalGateway
from app.repositories.errors import RepositoryError, with_rpc_retry
from app.repositories.ports.payment_methods import PaymentMethodRepository
from app.services.audit_service import AuditService
from app.services.context import UserContext


logger = logging.getLogger(__name__)

def _mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    return (local[:1] + "***@" + domain) if local else None

class PaymentMethodService:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        payment_methods: PaymentMethodRepository,
        paypal: PayPalGateway,
        audit: AuditService | None = None,
    ) -> None:
        self._settings, self._clock, self._payment_methods, self._paypal = settings, clock, with_rpc_retry(payment_methods), paypal
        self._audit = audit
        self.setup_limiter = SlidingWindowLimiter(5, monotonic=clock.monotonic)
        self.return_limiter = SlidingWindowLimiter(10, monotonic=clock.monotonic)

    def configured(self) -> None:
        if not self._settings.paypal_configured:
            raise ServiceUnavailable("PayPal is not configured.", "paypal_not_configured")
        if not self._settings.public_base_url:
            raise ServiceUnavailable("PUBLIC_BASE_URL is not configured.", "public_url_not_configured")

    async def setup(self, user: UserContext, *, request_id: str) -> dict[str, Any]:
        self.configured()
        from app.core.exceptions import RateLimited
        retry = await self.setup_limiter.acquire(user.user_id)
        if retry is not None:
            raise RateLimited(retry)
        state = secrets.token_urlsafe(32)
        url = f"{self._settings.public_base_url}/api/v1/payment-methods/paypal/return?state={state}"
        expires = self._clock.utc_now() + timedelta(minutes=30)
        try:
            token = await self._paypal.create_setup_token(return_url=url, cancel_url=url+"&cancelled=1", request_id=request_id)
        except Exception:
            raise ApiError(502, "paypal_unavailable", "PayPal linking is unavailable.") from None
        pm_id = await self._payment_methods.create_pending(
            user_id=user.user_id, state_token=state, state_expires_at=expires, setup_token_reference=token.id)
        if self._audit:
            await self._audit.record("payment_method.setup_started",actor_type="user",actor_id=user.user_id,user_id=user.user_id,entity_type="payment_method",entity_id=pm_id)
        return {"payment_method_id": pm_id, "approve_url": token.approve_url, "expires_at": expires}

    async def handle_return(self, state_token: str, cancelled: bool = False) -> dict[str, Any]:
        self.configured()
        pm = await self._payment_methods.find_by_state(state_token)
        if pm is None:
            raise ApiError(400, "invalid_state", "Invalid or expired linking state.")
        if cancelled:
            await self._payment_methods.fail_pending(pm['id'])
            return {"status": "cancelled"}
        token = await self._paypal.get_setup_token(pm['setup_token_reference'])
        if token.status != "APPROVED":
            return {"status": "failed"}
        payment = await self._paypal.create_payment_token(setup_token_id=pm['setup_token_reference'], request_id=f"pt-{pm['id']}")
        masked = _mask_email(payment.payer_email)
        for attempt in range(2):
            try:
                result = await self._payment_methods.activate_payment_method(
                    payment_method_id=pm['id'], user_id=pm['user_id'], vault_token_reference=payment.id,
                    provider_customer_reference=payment.customer_id, payer_email_masked=masked)
                if not result.ok:
                    raise Conflict("conflict", "Linking failed. Start again.")
                break
            except RepositoryError as exc:
                if exc.code != "23505" or attempt:
                    raise Conflict("conflict", "Linking failed. Start again.") from None
        return {"status": "activated", "payer_email_masked": masked}

    async def revoke(self, user: UserContext, payment_method_id: str) -> dict[str, str]:
        self.configured()
        result = await self._payment_methods.revoke_payment_method(payment_method_id=payment_method_id, user_id=user.user_id)
        if not result.ok:
            if result.reason == "payment_in_progress":
                raise Conflict("payment_in_progress", "A payment using this PayPal account is still being processed.")
            raise ApiError(404, "not_found", "Payment method not found.")
        if result.data.get("vault_token_reference"):
            try:
                await self._paypal.delete_payment_token(result.data['vault_token_reference'])
            except Exception as exc:
                logger.warning("PayPal vault delete failed: %s", type(exc).__name__)
        return {"id": payment_method_id, "status": "revoked"}
