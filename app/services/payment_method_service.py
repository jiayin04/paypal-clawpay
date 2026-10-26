"""Payment method linking service per design §3.10 (PayPal Vault linking flow)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import timedelta
from typing import Any

from app.core.clock import Clock
from app.core.config import Settings
from app.core.exceptions import ApiError, Conflict, ServiceUnavailable
from app.integrations.paypal import PayPalGateway
from app.repositories.errors import RepositoryError, call_rpc_with_retry
from app.repositories.protocols import PaymentMethodRepository
from app.services.context import DeviceContext

logger = logging.getLogger(__name__)

_STATE_TTL_SECONDS = 600  # 10 minutes
_EMAIL_RE = None  # lazy import


def _mask_email(email: str) -> str:
    """Mask 'john@example.com' -> 'j***@example.com'."""
    if "@" not in email:
        return "***@unknown"
    local, domain = email.split("@", 1)
    masked_local = local[0] + "***" if local else "***"
    return f"{masked_local}@{domain}"


def _generate_state_token() -> str:
    """Generate a 32-byte URL-safe state token."""
    return hashlib.sha256(os.urandom(32)).hexdigest()[:40]


class PaymentMethodService:
    """Implements the PayPal Vault linking flow (design §3.10).

    Flow:
    1. POST /setup   -> create pending row, create PayPal setup token, return approve_url
    2. GET  /return  -> (unauthenticated) state lookup, get setup token, create payment token,
                        activate_payment_method, render HTML redirect page
    3. GET           -> list active payment methods for the authenticated user
    4. DELETE /{id}  -> revoke payment method (soft-delete), best-effort PayPal vault delete
    """

    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        payment_methods: PaymentMethodRepository,
        paypal: PayPalGateway,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._payment_methods = payment_methods
        self._paypal = paypal

    def _return_url(self) -> str:
        base = (self._settings.public_base_url or "").rstrip("/")
        if not base:
            raise ServiceUnavailable("paypal_not_configured", "PUBLIC_BASE_URL is not configured.")
        return f"{base}/api/v1/payment-methods/return"

    def _cancel_url(self, return_url: str) -> str:
        return f"{return_url}?cancelled=1"

    async def setup(self, device: DeviceContext, *, request_id: str) -> dict[str, Any]:
        """Create a pending payment method and a PayPal setup token.

        Returns {payment_method_id, approve_url, expires_at}.
        Raises 503 if PayPal not configured or public URL not set.
        """
        if not self._settings.paypal_client_id:
            raise ServiceUnavailable("paypal_not_configured", "PayPal is not configured.")
        return_url = self._return_url()  # raises 503 if missing
        cancel_url = self._cancel_url(return_url)

        state_token = _generate_state_token()
        now = self._clock.utc_now()
        state_expires_at = now + timedelta(seconds=_STATE_TTL_SECONDS)

        # Create pending row (before calling PayPal so we have an id)
        payment_method_id = await self._payment_methods.create_pending(
            user_id=device.user_id,
            state_token=state_token,
            state_expires_at=state_expires_at,
        )

        # Create PayPal setup token
        setup_token = await self._paypal.create_setup_token(
            return_url=f"{return_url}?state={state_token}",
            cancel_url=f"{cancel_url}&state={state_token}",
            request_id=request_id,
        )

        expires_at_str = state_expires_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return {
            "payment_method_id": payment_method_id,
            "approve_url": setup_token.approve_url,
            "expires_at": expires_at_str,
        }

    async def handle_return(self, state_token: str, cancelled: bool = False) -> dict[str, Any]:
        """Handle the return from PayPal after the buyer authorises (or cancels).

        Returns {status: "activated"|"cancelled"|"failed", payer_email_masked}.
        """
        pm = await self._payment_methods.find_by_state(state_token)
        if pm is None:
            raise ApiError(404, "state_not_found", "State token not found or expired.")

        payment_method_id = str(pm["id"])
        user_id = str(pm["user_id"])

        if cancelled:
            return {"status": "cancelled"}

        # Get the setup token ID from PayPal (state token is our own; we need the PayPal setup token id)
        # The setup token id was embedded in the return URL's query param by PayPal.
        # We can't easily store it unless we match on setup_token_id.
        # For the return flow we use the setup_token_id passed in the return URL.
        raise ApiError(
            400,
            "missing_setup_token",
            "setup_token_id must be provided in the return URL parameters.",
        )

    async def handle_return_with_token(
        self, state_token: str, setup_token_id: str, cancelled: bool = False
    ) -> dict[str, Any]:
        """Handle return with the setup_token_id from PayPal's redirect."""
        pm = await self._payment_methods.find_by_state(state_token)
        if pm is None:
            raise ApiError(404, "state_not_found", "State token not found or expired.")

        payment_method_id = str(pm["id"])
        user_id = str(pm["user_id"])

        if cancelled:
            return {"status": "cancelled", "payment_method_id": payment_method_id}

        # Get setup token to verify APPROVED status
        setup_token = await self._paypal.get_setup_token(setup_token_id)
        if setup_token.status != "APPROVED":
            logger.warning("Setup token %s status is %s, not APPROVED", setup_token_id, setup_token.status)
            return {"status": "failed", "payment_method_id": payment_method_id, "reason": setup_token.status}

        # Create payment token (vault token)
        payment_token = await self._paypal.create_payment_token(
            setup_token_id=setup_token_id,
            request_id=f"pt-{payment_method_id}",
        )
        vault_id = payment_token.id

        # Mask email from payment token payer info if available
        raw_email: str | None = getattr(payment_token, "payer_email", None)
        payer_email_masked = _mask_email(raw_email) if raw_email else None

        # Activate (with one 23505 unique-violation retry per design §3.10)
        result = await self._payment_methods.activate_payment_method(
            payment_method_id=payment_method_id,
            user_id=user_id,
            vault_token_reference=vault_id,
            payer_email_masked=payer_email_masked,
        )
        if not result.ok:
            if result.reason == "23505":
                # Retry once
                result = await self._payment_methods.activate_payment_method(
                    payment_method_id=payment_method_id,
                    user_id=user_id,
                    vault_token_reference=vault_id,
                    payer_email_masked=payer_email_masked,
                )
            if not result.ok:
                raise Conflict("conflict", "Payment method conflict – try again.")

        return {
            "status": "activated",
            "payment_method_id": payment_method_id,
            "payer_email_masked": payer_email_masked,
        }

    async def list_methods(self, user_id: str) -> list[dict[str, Any]]:
        """Return active payment methods for the user (no vault_token_reference)."""
        rows = await self._payment_methods.list_active(user_id)
        return [
            {
                "id": r["id"],
                "status": r.get("status", "active"),
                "payer_email_masked": r.get("payer_email_masked"),
                "created_at": str(r["created_at"]) if r.get("created_at") else None,
            }
            for r in rows
        ]

    async def revoke(self, device: DeviceContext, payment_method_id: str) -> dict[str, Any]:
        """Soft-revoke a payment method, then best-effort PayPal vault delete."""
        result = await self._payment_methods.revoke_payment_method(
            payment_method_id=payment_method_id,
            user_id=device.user_id,
        )
        if not result.ok:
            if result.reason == "payment_in_progress":
                raise Conflict(
                    "payment_in_progress",
                    "A payment using this method is in progress. Cancel it first.",
                )
            raise ApiError(404, "not_found", "Payment method not found.")

        vault_ref = result.data.get("vault_token_reference")
        if vault_ref:
            # Best-effort: PayPal delete; failure is logged but not propagated
            try:
                await self._paypal.delete_payment_token(vault_ref)
            except Exception as exc:
                logger.warning("Failed to delete PayPal vault token %s: %s", vault_ref, type(exc).__name__)

        return {"payment_method_id": payment_method_id, "status": "revoked"}
