"""Logging setup with secret redaction.

Every record is scrubbed when it is created (via a LogRecord factory), and
again by a filter on the root and uvicorn handlers, so secrets never reach the
log sink via message args or exception tracebacks, whichever handler or logger
ends up emitting the record.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any, TYPE_CHECKING

from app.core.observability import ContextFilter, JsonFormatter


if TYPE_CHECKING:
    from app.core.config import Settings

REDACTED = "[REDACTED]"
_MIN_SECRET_LENGTH = 8
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

_SENSITIVE_KEY = r"[\w-]*(?:secret|token|password|passwd|api[_-]?key|service[_-]?role|authorization)[\w-]*"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ClawPay device and LLM tokens, including the /mcp/<token> path form
    (re.compile(r"cp[dl]_[A-Za-z0-9_-]{20,}"), REDACTED),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), f"Bearer {REDACTED}"),
    # JWT-shaped values (Supabase keys are JWTs)
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), REDACTED),
    # "key": "value" (JSON-ish)
    (
        re.compile(rf'(?i)(["\']{_SENSITIVE_KEY}["\']\s*:\s*)(["\'])(?:(?!\2).)*\2'),
        rf"\1\2{REDACTED}\2",
    ),
    # key=value or key: value
    (
        re.compile(rf"(?i)\b({_SENSITIVE_KEY})(\s*[=:]\s*)(?![\"'\s])[^\s,;&\"']+"),
        rf"\1\2{REDACTED}",
    ),
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace known secret literals and secret-looking patterns in ``text``."""
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= _MIN_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets: set[str] = {s for s in secrets if len(s) >= _MIN_SECRET_LENGTH}

    def _redact_arg(self, value: object) -> object:
        if isinstance(value, str):
            return redact(value, self.secrets)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        text = str(value)
        cleaned = redact(text, self.secrets)
        return value if cleaned == text else cleaned

    def filter(self, record: logging.LogRecord) -> bool:
        has_extra = bool(record.exc_info or record.exc_text or record.stack_info)
        if not has_extra:
            # Redact msg and args in place so formatters that read record.args
            # (e.g. uvicorn's access formatter) keep working.
            original = record.getMessage()
            msg, args = record.msg, record.args
            if isinstance(msg, str):
                record.msg = redact(msg, self.secrets)
            if isinstance(record.args, tuple):
                record.args = tuple(self._redact_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: self._redact_arg(v) for k, v in record.args.items()}
            try:
                rendered = record.getMessage()
            except Exception:
                rendered = None
            fully_redacted = redact(original, self.secrets)
            if rendered == fully_redacted:
                return True
            # Per-part redaction was not enough (e.g. "client_secret=%s"):
            # restore and fall through to collapsing the rendered message.
            record.msg, record.args = msg, args

        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{logging.Formatter().formatException(record.exc_info)}"
            record.exc_info = None
        elif record.exc_text:
            message = f"{message}\n{record.exc_text}"
        if record.stack_info:
            message = f"{message}\n{record.stack_info}"
            record.stack_info = None
        record.msg = redact(message, self.secrets)
        record.args = None
        record.exc_text = None
        return True


_active_filter: RedactingFilter | None = None


def _install_record_factory(new_filter: RedactingFilter) -> None:
    """Redact every record at creation time.

    Handler filters only cover handlers that exist when configure_logging runs;
    the record factory also covers handlers added later and non-propagating
    library loggers with their own handlers.
    """
    global _active_filter
    _active_filter = new_filter
    current = logging.getLogRecordFactory()
    if getattr(current, "_clawpay_redacting", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(*args, **kwargs)
        active = _active_filter
        if active is not None:
            try:
                active.filter(record)
            except Exception:
                # Malformed call (e.g. too few args): never raise at the call
                # site; keep the redacted format string instead.
                record.msg = redact(str(record.msg), active.secrets)
                record.args = None
                if record.exc_info:
                    try:
                        text = logging.Formatter().formatException(record.exc_info)
                        record.msg = f"{record.msg}\n{redact(text, active.secrets)}"
                    except Exception:
                        pass
                    record.exc_info = None
                    record.exc_text = None
        return record

    factory._clawpay_redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def _replace_filter(target: logging.Filterer, new_filter: RedactingFilter) -> None:
    for existing in list(target.filters):
        if isinstance(existing, RedactingFilter):
            target.removeFilter(existing)
    target.addFilter(new_filter)


def configure_logging(settings: Settings) -> None:
    """Idempotently configure root logging with secret redaction."""
    root = logging.getLogger()
    root.setLevel(settings.log_level)

    handler = next(
        (h for h in root.handlers if getattr(h, "_clawpay_handler", False)), None
    )
    if handler is None:
        handler = logging.StreamHandler()
        handler._clawpay_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    # Text for local runs, JSON lines for log shipping (LOG_FORMAT=json).
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else logging.Formatter(_LOG_FORMAT))
    if not any(isinstance(f, ContextFilter) for f in handler.filters):
        handler.addFilter(ContextFilter())

    redacting = RedactingFilter(settings.secret_values())
    _install_record_factory(redacting)
    for h in root.handlers:
        _replace_filter(h, redacting)
    for name in _UVICORN_LOGGERS:
        for h in logging.getLogger(name).handlers:
            _replace_filter(h, redacting)
