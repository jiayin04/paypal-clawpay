"""Public PayPal integration API; implementation lives in feature modules."""
from app.integrations.paypal.client import PayPalClient
from app.integrations.paypal.errors import (
    PayPalApiError,
    PayPalAuthError,
    PayPalError,
    PayPalUnavailable,
    PaymentClassification,
    classify,
)
from app.integrations.paypal.gateway import PayPalGateway
from app.integrations.paypal.models import (
    PaymentToken,
    ProviderOrder,
    SetupToken,
    from_paypal_value,
    to_paypal_value,
)
from app.integrations.paypal.service import PayPalService

__all__ = ["PayPalGateway", "PayPalService", "PayPalClient", "SetupToken", "PaymentToken", "ProviderOrder", "PaymentClassification", "PayPalError", "PayPalApiError", "PayPalAuthError", "PayPalUnavailable", "classify", "to_paypal_value", "from_paypal_value"]
