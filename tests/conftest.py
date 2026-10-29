import os

import pytest

_ENV_PREFIXES = (
    "SUPABASE_",
    "PAYPAL_",
    "AGENTCORE_",
    "GADGET_",
    "MCP_",
    "DEVICE_",
    "LLM_PROXY_",
    "MERCHANT_",
    "RECONCILE_",
    "ORDER_",
    "QUOTE_",
    "CHALLENGE_",
    "AUTH_",
    "CLIENT_IP_",
    "DEVICE_OWNER_",
    "CLAWPAY_",
    "METRICS_",
)
_ENV_NAMES = {
    "APP_ENV",
    "PUBLIC_BASE_URL",
    "APP_HOST",
    "APP_PORT",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "INTERNAL_SERVICE_TOKEN",
    "GROCER_AGENT_ENDPOINT",
    "ELECTRONICS_AGENT_ENDPOINT",
    "PHONE_APPROVAL_THRESHOLD_MINOR",
    "DAILY_SPENDING_LIMIT_MINOR",
    "PHONE_APPROVAL_THRESHOLD_MYR",
    "DAILY_SPENDING_LIMIT_MYR",
    "DEFAULT_CURRENCY",
    "SPENDING_DAY_TIMEZONE",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's shell environment out of test results."""
    for name in list(os.environ):
        upper = name.upper()
        if upper in _ENV_NAMES or upper.startswith(_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
