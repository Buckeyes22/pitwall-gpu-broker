"""Central allowlist-oriented redaction for logs and diagnostics."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

REDACTED = "[REDACTED]"
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s]+@)")
_AUTHORIZATION = re.compile(
    r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
# Generic bearer values are normally long opaque credentials.  Requiring at
# least 16 characters avoids corrupting benign prose such as "Bearer
# authorization" while configured short test/operator tokens are still caught
# by the exact-secret pass above.
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")
_LABELED_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|hmac)(\s*[:=]\s*)([^\s,;]+)"
)
_SECRET_ENV_NAMES = (
    "RUNPOD_API_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "PITWALL_ADMIN_SECRET",
    "PITWALL_API_TOKEN",
    "PITWALL_WEBHOOK_SECRET",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
)
_LOG_SECRETS: set[str] = set()
_LOG_FACTORY_INSTALLED = False


def redact_text(value: object, *, secrets: Iterable[str] = ()) -> str:
    """Return text with credentials, auth headers, and explicit canaries removed."""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    text = _AUTHORIZATION.sub(rf"\1\2{REDACTED}", text)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    return _LABELED_SECRET.sub(rf"\1\2{REDACTED}", text)


def safe_url_label(value: str) -> str:
    """Return only a URL's scheme, host, optional port, and database/path name."""
    parsed = urlsplit(value)
    if not parsed.scheme or parsed.hostname is None:
        return "invalid-url"
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    authority = f"{host}:{port}" if port is not None else host
    path = parsed.path.rsplit("/", 1)[-1]
    suffix = f"/{path}" if path else ""
    return f"{parsed.scheme}://{authority}{suffix}"


def configure_logging_redaction(*, secrets: Iterable[str] = ()) -> None:
    """Install process-wide redaction for log message templates and arguments."""

    global _LOG_FACTORY_INSTALLED
    _LOG_SECRETS.update(secret for secret in secrets if secret)
    _LOG_SECRETS.update(value for name in _SECRET_ENV_NAMES if (value := os.environ.get(name, "")))
    if _LOG_FACTORY_INSTALLED:
        return
    previous_factory = logging.getLogRecordFactory()

    def redact_argument(value: Any) -> Any:
        if isinstance(value, (str, BaseException)):
            return redact_text(value, secrets=_LOG_SECRETS)
        return value

    def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        record.msg = redact_text(record.msg, secrets=_LOG_SECRETS)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_argument(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact_argument(value) for key, value in record.args.items()}
        return record

    logging.setLogRecordFactory(redacting_factory)
    _LOG_FACTORY_INSTALLED = True


__all__ = ["REDACTED", "configure_logging_redaction", "redact_text", "safe_url_label"]
