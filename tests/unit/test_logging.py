import logging

import pytest

from app.core.config import Settings
from app.core.logging import REDACTED, RedactingFilter, configure_logging, redact

SECRET = "sk_live_abcdefghijklmnop"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.c2lnbmF0dXJlLXZhbHVl"


def test_registered_secret_literal_replaced() -> None:
    out = redact(f"connecting with {SECRET} now", [SECRET])
    assert SECRET not in out
    assert REDACTED in out


def test_short_secret_literals_ignored() -> None:
    assert redact("the value is abc", ["abc"]) == "the value is abc"


def test_bearer_token_redacted() -> None:
    out = redact("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert REDACTED in out


def test_jwt_redacted() -> None:
    out = redact(f"key loaded {JWT}")
    assert JWT not in out
    assert "eyJ" not in out


def test_key_value_pairs_redacted() -> None:
    out = redact("client_secret=xyz123456&grant_type=client_credentials")
    assert "xyz123456" not in out
    assert "grant_type=client_credentials" in out

    out = redact('{"service_role_key": "abc-very-secret", "name": "ok"}')
    assert "abc-very-secret" not in out
    assert '"name": "ok"' in out

    out = redact("password: hunter2hunter2")
    assert "hunter2hunter2" not in out


def test_normal_text_untouched() -> None:
    text = "Order 123 created for merchant grocer total_minor=10000 currency=MYR"
    assert redact(text) == text


def test_filter_redacts_args(caplog: pytest.LogCaptureFixture) -> None:
    caplog.handler.addFilter(RedactingFilter([SECRET]))
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("key %s", SECRET)
    assert SECRET not in caplog.text
    assert REDACTED in caplog.text


def test_filter_redacts_exception_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.handler.addFilter(RedactingFilter([SECRET]))
    with caplog.at_level(logging.ERROR):
        try:
            raise RuntimeError(f"failed with {SECRET}")
        except RuntimeError:
            logging.getLogger("t").exception("boom")
    assert SECRET not in caplog.text
    assert "RuntimeError" in caplog.text
    assert REDACTED in caplog.text


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1234", "GET", path, "1.1", 200), None,
    )


def test_uvicorn_access_formatter_still_works() -> None:
    """uvicorn's AccessFormatter unpacks record.args; the filter must keep them."""
    from uvicorn.logging import AccessFormatter

    formatter = AccessFormatter('%(client_addr)s "%(request_line)s" %(status_code)s', use_colors=False)
    record = _access_record("/health")
    RedactingFilter([SECRET]).filter(record)
    assert formatter.format(record) == '127.0.0.1:1234 "GET /health HTTP/1.1" 200 OK'

    record = _access_record(f"/cb?token={SECRET}")
    RedactingFilter([SECRET]).filter(record)
    out = formatter.format(record)
    assert SECRET not in out
    assert "/cb?token=[REDACTED]" in out


def test_secret_split_between_format_and_args_redacted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.handler.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("client_secret=%s", "notregistered123")
    assert "notregistered123" not in caplog.text


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def test_handlers_added_after_configure_are_redacted() -> None:
    """Covers handlers created later and non-propagating library loggers."""
    configure_logging(Settings(_env_file=None, supabase_service_role_key=SECRET))

    lib_logger = logging.getLogger("thirdparty.lib")
    lib_logger.propagate = False
    lib_logger.setLevel(logging.INFO)
    late = _ListHandler()
    lib_logger.addHandler(late)
    try:
        lib_logger.info("calling api with %s", SECRET)
        try:
            raise RuntimeError(f"failed with {SECRET}")
        except RuntimeError:
            lib_logger.exception("boom")
    finally:
        lib_logger.removeHandler(late)
        lib_logger.propagate = True

    output = "\n".join(late.lines)
    assert SECRET not in output
    assert output.count(REDACTED) >= 2
    assert "RuntimeError" in output


def test_record_factory_installed_once() -> None:
    settings = Settings(_env_file=None)
    configure_logging(settings)
    factory = logging.getLogRecordFactory()
    configure_logging(settings)
    assert logging.getLogRecordFactory() is factory
    assert getattr(factory, "_clawpay_redacting", False)


def test_configure_logging_idempotent() -> None:
    settings = Settings(_env_file=None, supabase_service_role_key=SECRET)
    root = logging.getLogger()
    configure_logging(settings)
    configure_logging(settings)
    ours = [h for h in root.handlers if getattr(h, "_clawpay_handler", False)]
    assert len(ours) == 1
    filters = [f for f in ours[0].filters if isinstance(f, RedactingFilter)]
    assert len(filters) == 1
    assert SECRET in filters[0].secrets


# --- Phase 2: review finding #1 and device/LLM token patterns ------------------------

DEVICE_TOKEN = "cpd_" + "A" * 20 + "b-_" + "C" * 20
LLM_TOKEN = "cpl_" + "Z" * 43


def test_malformed_format_call_does_not_raise() -> None:
    configure_logging(Settings(_env_file=None, supabase_service_role_key=SECRET))
    lib_logger = logging.getLogger("t.malformed")
    lib_logger.propagate = False
    lib_logger.setLevel(logging.INFO)
    handler = _ListHandler()
    lib_logger.addHandler(handler)
    try:
        lib_logger.info(f"a %s %s {SECRET}", "only-one-arg")  # too few args
    finally:
        lib_logger.removeHandler(handler)
        lib_logger.propagate = True
    output = "\n".join(handler.lines)
    assert "a %s %s" in output
    assert SECRET not in output
    assert REDACTED in output


def test_device_and_llm_tokens_redacted() -> None:
    out = redact(f"token {DEVICE_TOKEN} and {LLM_TOKEN}")
    assert DEVICE_TOKEN not in out and LLM_TOKEN not in out
    assert out.count(REDACTED) == 2


def test_path_token_redacted_in_access_line() -> None:
    from uvicorn.logging import AccessFormatter

    formatter = AccessFormatter('%(client_addr)s "%(request_line)s" %(status_code)s', use_colors=False)
    record = _access_record(f"/mcp/{DEVICE_TOKEN}")
    record.args = ("127.0.0.1:1234", "POST", f"/mcp/{DEVICE_TOKEN}", "1.1", 200)
    RedactingFilter().filter(record)
    out = formatter.format(record)
    assert DEVICE_TOKEN not in out
    assert '"POST /mcp/[REDACTED] HTTP/1.1"' in out
