import pytest
from pydantic import ValidationError

from app.core.config import Settings


def make(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_defaults_use_minor_units_and_myr() -> None:
    s = make()
    assert s.phone_approval_threshold_minor == 10000
    assert s.daily_spending_limit_minor == 50000
    assert s.default_currency == "MYR"
    assert s.spending_day_timezone == "Asia/Kuala_Lumpur"
    assert s.spending_tz.key == "Asia/Kuala_Lumpur"
    assert s.paypal_env == "sandbox"


def test_env_overrides_parse_as_ints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHONE_APPROVAL_THRESHOLD_MINOR", "20000")
    monkeypatch.setenv("DAILY_SPENDING_LIMIT_MINOR", "80000")
    monkeypatch.setenv("DEFAULT_CURRENCY", "usd")
    s = make()
    assert s.phone_approval_threshold_minor == 20000
    assert s.daily_spending_limit_minor == 80000
    assert s.default_currency == "USD"


def test_lowercase_currency_normalised() -> None:
    assert make(default_currency="myr").default_currency == "MYR"


@pytest.mark.parametrize("bad", ["RM", "MYRR", "12A", ""])
def test_invalid_currency_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        make(default_currency=bad)


def test_negative_minor_amount_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_SPENDING_LIMIT_MINOR", "-1")
    with pytest.raises(ValidationError):
        make()


def test_non_integer_minor_amount_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHONE_APPROVAL_THRESHOLD_MINOR", "100.50")
    with pytest.raises(ValidationError):
        make()


def test_threshold_above_limit_rejected() -> None:
    with pytest.raises(ValidationError):
        make(phone_approval_threshold_minor=60000, daily_spending_limit_minor=50000)


def test_unknown_timezone_rejected() -> None:
    with pytest.raises(ValidationError):
        make(spending_day_timezone="Mars/Olympus_Mons")


def test_missing_supabase_vars_not_configured() -> None:
    s = make()
    assert s.supabase_url is None
    assert s.supabase_service_role_key is None
    assert s.supabase_configured is False


def test_blank_supabase_url_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "")
    s = make()
    assert s.supabase_url is None
    assert s.supabase_service_role_key is None
    assert s.supabase_configured is False


def test_supabase_configured_requires_url_and_service_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    assert make().supabase_configured is False
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-secret-value")
    assert make().supabase_configured is True


@pytest.mark.parametrize(
    ("legacy", "replacement"),
    [
        ("PHONE_APPROVAL_THRESHOLD_MYR", "PHONE_APPROVAL_THRESHOLD_MINOR"),
        ("DAILY_SPENDING_LIMIT_MYR", "DAILY_SPENDING_LIMIT_MINOR"),
    ],
)
def test_spec_major_unit_names_fail_loudly(
    monkeypatch: pytest.MonkeyPatch, legacy: str, replacement: str
) -> None:
    monkeypatch.setenv(legacy, "100")
    with pytest.raises(ValidationError) as exc_info:
        make()
    assert replacement in str(exc_info.value)


def test_spec_major_unit_names_in_env_file_fail_loudly(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DAILY_SPENDING_LIMIT_MYR=500\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        Settings(_env_file=env_file)


def test_blank_spec_major_unit_names_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHONE_APPROVAL_THRESHOLD_MYR", "")
    assert make().phone_approval_threshold_minor == 10000


@pytest.mark.parametrize(("url", "primary", "expected"), [
    ("https://api.deepseek.com", "", "deepseek-test-secret"),
    ("https://api.deepseek.com/v1", "explicit-test-secret", "explicit-test-secret"),
    ("https://api.openai.com/v1", "", None),
])
def test_deepseek_key_fallback_is_scoped_to_provider(tmp_path, url, primary, expected):
    env_file = tmp_path / ".env"
    env_file.write_text(f"LLM_PROXY_UPSTREAM_BASE_URL={url}\n"
                        f"LLM_PROXY_UPSTREAM_API_KEY={primary}\n"
                        "DEEPSEEK_API_KEY=deepseek-test-secret\n")
    settings = Settings(_env_file=env_file)
    key = settings.llm_proxy_upstream_api_key
    assert (key.get_secret_value() if key else None) == expected
    assert "deepseek-test-secret" not in repr(settings)
    assert "deepseek-test-secret" in settings.secret_values()


def test_secrets_not_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "super-secret-service-role-key"
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", secret)
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "paypal-secret-value-123")
    s = make()
    assert secret not in repr(s)
    assert secret not in s.model_dump_json()
    assert "paypal-secret-value-123" not in str(s)
    assert set(s.secret_values()) == {secret, "paypal-secret-value-123"}


def test_device_integration_placeholders_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    monkeypatch.setenv("LLM_PROXY_UPSTREAM_API_KEY", "llm-upstream-key-456")
    monkeypatch.setenv("DEVICE_TOKEN_PEPPER", "device-pepper-value-789")
    s = make()
    assert s.mcp_enabled is False
    assert s.public_base_url is None
    assert s.llm_proxy_upstream_base_url is None
    assert "llm-upstream-key-456" not in repr(s)
    assert set(s.secret_values()) == {"llm-upstream-key-456", "device-pepper-value-789"}


# --- Phase 2 settings (design §1.2) -------------------------------------------------

PEPPER = "p" * 32


def test_publishable_key_alias_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_ANON_KEY", "legacy-anon-key-value")
    assert make().supabase_publishable_key.get_secret_value() == "legacy-anon-key-value"
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_new_value")
    assert make().supabase_publishable_key.get_secret_value() == "sb_publishable_new_value"


def test_publishable_key_blank_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "")
    assert make().supabase_publishable_key is None


def _error_text(**kwargs) -> str:
    with pytest.raises(ValidationError) as exc_info:
        make(**kwargs)
    return str(exc_info.value)


def test_publishable_key_as_service_key_rejected() -> None:
    secret = "sb_publishable_abcdef123456"
    text = _error_text(supabase_service_role_key=secret)
    assert "SUPABASE_SERVICE_ROLE_KEY" in text
    assert secret not in text


def test_live_paypal_rejected() -> None:
    assert "live PayPal is not supported" in _error_text(paypal_env="live")


def test_mcp_enabled_requires_long_pepper() -> None:
    assert "DEVICE_TOKEN_PEPPER" in _error_text(mcp_enabled=True)
    short = "short-pepper"
    text = _error_text(mcp_enabled=True, device_token_pepper=short)
    assert "DEVICE_TOKEN_PEPPER" in text and short not in text
    assert make(mcp_enabled=True, device_token_pepper=PEPPER).mcp_enabled is True


def test_short_internal_token_rejected() -> None:
    short = "too-short-token"
    text = _error_text(internal_service_token=short)
    assert "INTERNAL_SERVICE_TOKEN" in text and short not in text
    assert make(internal_service_token="t" * 32).internal_service_token is not None


def test_default_currency_must_be_supported() -> None:
    assert "DEFAULT_CURRENCY must be MYR or USD" in _error_text(default_currency="EUR")
    assert make(default_currency="MYR").default_currency == "MYR"
    assert make(default_currency="USD").default_currency == "USD"


def test_supabase_url_trailing_slash_normalised() -> None:
    s = make(supabase_url="https://abc.supabase.co/")
    assert s.supabase_url == "https://abc.supabase.co"
    assert s.supabase_jwks_url == "https://abc.supabase.co/auth/v1/.well-known/jwks.json"
    assert s.supabase_jwt_issuer == "https://abc.supabase.co/auth/v1"
    assert "//auth" not in s.supabase_jwks_url


@pytest.mark.parametrize(
    "field", ["grocer_agent_endpoint", "electronics_agent_endpoint", "llm_proxy_upstream_base_url"]
)
@pytest.mark.parametrize(
    "bad",
    ["http://example.com/a2a", "ftp://127.0.0.1/a2a", "https://user:pw@example.com/a2a", "not a url"],
)
def test_upstream_url_rejected(field: str, bad: str) -> None:
    text = _error_text(**{field: bad})
    assert field.upper() in text
    assert bad not in text


@pytest.mark.parametrize(
    "good", ["http://127.0.0.1:8101/a2a", "http://localhost:8101/a2a", "https://agents.example.com/a2a/"]
)
def test_upstream_url_accepted(good: str) -> None:
    s = make(grocer_agent_endpoint=good)
    assert s.grocer_agent_endpoint == good.rstrip("/")


def test_public_base_url_rules() -> None:
    assert make(public_base_url="https://x.trycloudflare.com/").public_base_url == "https://x.trycloudflare.com"
    assert make(public_base_url="http://127.0.0.1:8000").public_base_url == "http://127.0.0.1:8000"
    assert "PUBLIC_BASE_URL" in _error_text(public_base_url="http://example.com")
    assert "PUBLIC_BASE_URL" in _error_text(app_env="production", public_base_url="http://127.0.0.1:8000")


def test_phase2_defaults() -> None:
    s = make()
    assert s.mcp_path_token_enabled is False
    assert s.device_owner_registration_enabled is False
    assert s.order_require_payment_method is True
    assert s.client_ip_header == "none"
    assert s.mcp_allowed_origins == []
    assert (s.quote_ttl_seconds, s.challenge_ttl_seconds, s.merchant_agent_timeout_seconds) == (300, 120, 8.0)


def test_allowed_origins_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://a.example, https://b.example/")
    assert make().mcp_allowed_origins == ["https://a.example", "https://b.example"]


@pytest.mark.parametrize("bad", [{"quote_ttl_seconds": 30}, {"merchant_agent_timeout_seconds": 20},
                                 {"challenge_ttl_seconds": 301}, {"client_ip_header": "x-forwarded-for"}])
def test_phase2_ranges(bad: dict) -> None:
    with pytest.raises(ValidationError):
        make(**bad)


def test_env_example_is_valid_and_complete() -> None:
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / ".env.example"
    s = Settings(_env_file=path)
    assert s.mcp_enabled is False and s.default_currency == "MYR"
    names = {line.split("=", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")}
    expected = {name.upper() for name in Settings.model_fields} - {
        "SUPABASE_TIMEOUT_SECONDS", "PHONE_APPROVAL_THRESHOLD_MYR", "DAILY_SPENDING_LIMIT_MYR"}
    assert expected <= names, expected - names
