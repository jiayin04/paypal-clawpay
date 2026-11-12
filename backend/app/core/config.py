"""Typed application settings loaded from environment variables / .env.

Money values are integers in MINOR units of the currency (e.g. sen for MYR):
RM100 = 10000, RM500 = 50000.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.domain.money import EXPONENTS


_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
_MIN_PEPPER_LENGTH = 32
_MIN_INTERNAL_TOKEN_LENGTH = 32

# Outbound endpoints: https, or http only to a loopback host (local mock agents).
_UPSTREAM_URL_FIELDS = {
    "grocer_agent_endpoint": "GROCER_AGENT_ENDPOINT",
    "electronics_agent_endpoint": "ELECTRONICS_AGENT_ENDPOINT",
    "llm_proxy_upstream_base_url": "LLM_PROXY_UPSTREAM_BASE_URL",
    "paypal_agent_endpoint": "PAYPAL_AGENT_ENDPOINT",
    "channel3_agent_endpoint": "CHANNEL3_AGENT_ENDPOINT",
    "channel3_base_url": "CHANNEL3_BASE_URL",
}

# Optional fields where an empty env value (e.g. "SUPABASE_URL=") means "unset".
_OPTIONAL_FIELDS = (
    "supabase_url",
    "supabase_publishable_key",
    "supabase_jwt_secret",
    "supabase_service_role_key",
    "paypal_client_id",
    "paypal_client_secret",
    "paypal_webhook_id",
    "gadget_approval_public_key_registry",
    "agentcore_region",
    "paypal_agent_endpoint",
    "grocer_agent_endpoint",
    "electronics_agent_endpoint",
    "public_base_url",
    "device_token_pepper",
    "llm_proxy_upstream_base_url",
    "llm_proxy_upstream_api_key",
    "deepseek_api_key",
    "llm_proxy_model",
    "internal_service_token",
    "metrics_token",
    "paypal_agent_token",
    "channel3_api_key",
    "agent_passport_signing_key",
    "channel3_agent_endpoint",
    "fish_audio_api_key",
    "fish_audio_voice_id",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        # Validation errors name the variable, never its value.
        hide_input_in_errors=True,
    )

    # App
    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = Field(8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["text", "json"] = "text"  # json = one object per line for log shipping
    metrics_token: SecretStr | None = None  # bearer token for GET /metrics; unset = endpoint disabled

    # Supabase (service-role key is server-side only)
    supabase_url: str | None = None
    # SUPABASE_PUBLISHABLE_KEY wins over the legacy SUPABASE_ANON_KEY name.
    supabase_publishable_key: SecretStr | None = Field(
        None,
        validation_alias=AliasChoices(
            "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_ANON_KEY", "supabase_publishable_key"
        ),
    )
    supabase_service_role_key: SecretStr | None = None
    supabase_jwt_secret: SecretStr | None = None  # legacy HS256 projects only
    supabase_timeout_seconds: float = Field(5.0, gt=0)

    # PayPal
    paypal_env: Literal["sandbox", "live"] = "sandbox"
    paypal_client_id: str | None = None
    paypal_client_secret: SecretStr | None = None
    paypal_webhook_id: str | None = None
    paypal_http_timeout_seconds: float = Field(8.0, gt=0)
    paypal_brand_name: str = Field("ClawPay", min_length=1, max_length=127)
    paypal_inline_budget_seconds: float = Field(10.0, ge=1, le=15)
    reconcile_interval_seconds: int = Field(30, ge=5)
    reconcile_replay_window_seconds: int = Field(900, ge=60, le=3600)

    # Devices / agents
    gadget_approval_public_key_registry: str | None = None
    agentcore_region: str | None = None
    paypal_agent_endpoint: str | None = None  # A2A PayPal agent; unset = call PayPal directly
    paypal_agent_token: SecretStr | None = None  # shared bearer between ClawPay and the PayPal agent
    grocer_agent_endpoint: str | None = None
    electronics_agent_endpoint: str | None = None
    # Channel3 merchant (real catalog). Enabled when the API key (in-process agent)
    # or a standalone agent endpoint is set.
    channel3_api_key: SecretStr | None = None
    channel3_agent_endpoint: str | None = None
    channel3_base_url: str = "https://api.trychannel3.com"
    channel3_country: str = Field("US", pattern=r"^[A-Z]{2}$")
    channel3_timeout_seconds: float = Field(4.5, ge=0.5, le=15)
    internal_service_token: SecretStr | None = None

    # ESP-Claw device integration
    public_base_url: str | None = None  # public HTTPS origin the device calls
    mcp_enabled: bool = False  # mounts POST /mcp
    mcp_path_token_enabled: bool = False  # enables the /mcp/{device_token} fallback
    mcp_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    mcp_tool_calls_per_minute: int = Field(30, ge=1)
    mcp_requests_per_minute: int = Field(120, ge=1)
    device_requests_per_minute: int = Field(30, ge=1)
    auth_failures_per_minute: int = Field(20, ge=1)
    client_ip_header: Literal["none", "cf-connecting-ip", "x-forwarded-for-last"] = "none"
    device_owner_registration_enabled: bool = False
    device_token_pepper: SecretStr | None = None  # HMAC pepper for hashing device tokens

    # Agent passports: Ed25519 issuer seed (32 bytes, base64url) signs grants and receipts.
    agent_passport_signing_key: SecretStr | None = None
    # true = a device without any grant cannot order (default: only devices that were given a grant are governed)
    agent_passport_required: bool = False

    # Merchant agents and orders
    merchant_agent_timeout_seconds: float = Field(8.0, gt=0, le=15)
    quote_ttl_seconds: int = Field(300, ge=60, le=1800)
    challenge_ttl_seconds: int = Field(120, ge=30, le=300)
    order_require_payment_method: bool = True

    # OpenAI-compatible LLM proxy for the device (Phase 4); upstream key stays server-side
    llm_proxy_upstream_base_url: str | None = None
    llm_proxy_upstream_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None  # fallback for the direct DeepSeek upstream only
    llm_proxy_model: str | None = None
    llm_proxy_max_tokens: int = Field(1024, ge=1, le=8192)
    llm_proxy_requests_per_minute: int = Field(20, ge=1)
    llm_proxy_daily_token_limit: int = Field(200000, ge=0)
    llm_proxy_max_body_bytes: int = Field(262144, ge=1024)
    llm_proxy_timeout_seconds: float = Field(110.0, gt=0)

    # Fish Audio text-to-speech for watch replies (POST /api/v1/assistant/speech); key stays server-side
    fish_audio_api_key: SecretStr | None = None
    fish_audio_voice_id: str | None = None  # reference_id of a Fish Audio voice; unset = Fish default voice
    fish_audio_model: Literal["s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3"] = "s2.1-pro"
    fish_audio_requests_per_minute: int = Field(20, ge=1)
    fish_audio_timeout_seconds: float = Field(20.0, gt=0, le=60)

    # Spending policy (minor units)
    phone_approval_threshold_minor: int = Field(10000, ge=0)
    daily_spending_limit_minor: int = Field(50000, ge=0)
    default_currency: str = "MYR"
    spending_day_timezone: str = "Asia/Kuala_Lumpur"

    # Spec section 6 names (major units). Read only so we can reject them: an
    # alias would need a currency-dependent conversion, and silently ignoring
    # them would leave the operator on the defaults.
    phone_approval_threshold_myr: str | None = Field(None, exclude=True, repr=False)
    daily_spending_limit_myr: str | None = Field(None, exclude=True, repr=False)

    @field_validator(*_OPTIONAL_FIELDS, mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("supabase_url")
    @classmethod
    def _strip_supabase_url(cls, value: str | None) -> str | None:
        return value.strip().rstrip("/") if value is not None else None

    @field_validator("mcp_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.split(",")
        if isinstance(value, (list, tuple)):
            return [str(v).strip().rstrip("/") for v in value if str(v).strip()]
        return value

    @field_validator(*_UPSTREAM_URL_FIELDS, mode="after")
    @classmethod
    def _validate_upstream_url(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        env_name = _UPSTREAM_URL_FIELDS[info.field_name]
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        ok = (
            not parts.username
            and not parts.password
            and bool(parts.hostname)
            and (
                parts.scheme == "https"
                or (parts.scheme == "http" and parts.hostname in _LOOPBACK_HOSTS)
            )
        )
        if not ok:
            raise ValueError(
                f"{env_name} must be an https:// URL, or http:// to 127.0.0.1/localhost"
            )
        return value

    @field_validator("log_format", mode="before")
    @classmethod
    def _lower_log_format(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("default_currency", mode="before")
    @classmethod
    def _normalise_currency(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip().upper()
            if not _CURRENCY_RE.match(value):
                raise ValueError("default_currency must be a 3-letter ISO 4217 code")
        return value

    @field_validator("spending_day_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _reject_legacy_policy_vars(self) -> Settings:
        legacy = {
            "PHONE_APPROVAL_THRESHOLD_MYR": "PHONE_APPROVAL_THRESHOLD_MINOR",
            "DAILY_SPENDING_LIMIT_MYR": "DAILY_SPENDING_LIMIT_MINOR",
        }
        found = [old for old in legacy if getattr(self, old.lower()) not in (None, "")]
        if found:
            hints = ", ".join(f"{old} -> {legacy[old]}" for old in found)
            raise ValueError(
                f"unsupported env vars ({hints}); set the *_MINOR variables in minor units "
                "(e.g. RM100 = 10000)"
            )
        return self

    @model_validator(mode="after")
    def _threshold_within_limit(self) -> Settings:
        if self.phone_approval_threshold_minor > self.daily_spending_limit_minor:
            raise ValueError(
                "phone_approval_threshold_minor must not exceed daily_spending_limit_minor"
            )
        return self

    @model_validator(mode="after")
    def _startup_checks(self) -> Settings:
        if (self.llm_proxy_upstream_api_key is None
                and urlsplit(self.llm_proxy_upstream_base_url or "").hostname == "api.deepseek.com"):
            self.llm_proxy_upstream_api_key = self.deepseek_api_key
        if self.paypal_env == "live":
            raise ValueError("PAYPAL_ENV: live PayPal is not supported in this build")
        if self.default_currency not in EXPONENTS:
            raise ValueError("DEFAULT_CURRENCY must be MYR or USD")
        key = self.supabase_service_role_key
        if key is not None and key.get_secret_value().startswith("sb_publishable_"):
            raise ValueError(
                "SUPABASE_SERVICE_ROLE_KEY: service role key looks like a publishable key"
            )
        pepper = self.device_token_pepper
        if self.mcp_enabled and (
            pepper is None or len(pepper.get_secret_value()) < _MIN_PEPPER_LENGTH
        ):
            raise ValueError(
                "DEVICE_TOKEN_PEPPER must be set (at least 32 characters) when MCP_ENABLED=true"
            )
        token = self.internal_service_token
        if token is not None and len(token.get_secret_value()) < _MIN_INTERNAL_TOKEN_LENGTH:
            raise ValueError("INTERNAL_SERVICE_TOKEN must be at least 32 characters")
        agent_token = self.paypal_agent_token
        if agent_token is not None and len(agent_token.get_secret_value()) < _MIN_INTERNAL_TOKEN_LENGTH:
            raise ValueError("PAYPAL_AGENT_TOKEN must be at least 32 characters")
        if self.paypal_agent_endpoint and agent_token is None:
            raise ValueError("PAYPAL_AGENT_TOKEN must be set when PAYPAL_AGENT_ENDPOINT is set")
        seed = self.agent_passport_signing_key
        if seed is not None:
            import base64
            import binascii

            raw = seed.get_secret_value()
            try:
                decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            except (binascii.Error, ValueError):
                decoded = b""
            if len(decoded) != 32:
                raise ValueError("AGENT_PASSPORT_SIGNING_KEY must be 32 random bytes, base64url-encoded")
        metrics = self.metrics_token
        if metrics is not None and len(metrics.get_secret_value()) < _MIN_INTERNAL_TOKEN_LENGTH:
            raise ValueError("METRICS_TOKEN must be at least 32 characters")
        if self.public_base_url is not None:
            parts = urlsplit(self.public_base_url.strip())
            local_ok = (
                self.app_env in ("development", "test")
                and parts.scheme == "http"
                and parts.hostname in _LOOPBACK_HOSTS
            )
            if not parts.hostname or not (parts.scheme == "https" or local_ok):
                raise ValueError(
                    "PUBLIC_BASE_URL must be an https:// URL "
                    "(http://127.0.0.1 or localhost only in development/test)"
                )
            self.public_base_url = self.public_base_url.strip().rstrip("/")
        return self

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url) and self.supabase_service_role_key is not None

    @property
    def spending_tz(self) -> ZoneInfo:
        return ZoneInfo(self.spending_day_timezone)

    @property
    def supabase_jwks_url(self) -> str | None:
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json" if self.supabase_url else None

    @property
    def supabase_jwt_issuer(self) -> str | None:
        return f"{self.supabase_url}/auth/v1" if self.supabase_url else None

    @property
    def channel3_enabled(self) -> bool:
        return self.channel3_api_key is not None or bool(self.channel3_agent_endpoint)

    @property
    def merchant_codes(self) -> tuple[str, ...]:
        """Merchant agents ClawPay may call, in tool-schema order."""
        return ("grocer", "electronics", "channel3") if self.channel3_enabled else ("grocer", "electronics")

    @property
    def paypal_agent_enabled(self) -> bool:
        return bool(self.paypal_agent_endpoint) and self.paypal_agent_token is not None

    @property
    def paypal_configured(self) -> bool:
        """PayPal is reachable: through the PayPal agent, or directly with credentials."""
        if self.paypal_agent_enabled:
            return True
        return bool(self.paypal_client_id) and self.paypal_client_secret is not None

    def secret_values(self) -> list[str]:
        """Plain values of every configured secret, for log redaction."""
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                plain = value.get_secret_value()
                if plain:
                    values.append(plain)
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()
