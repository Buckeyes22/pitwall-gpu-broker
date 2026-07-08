"""Pitwall webhook dispatcher.

Dispatches signed webhook payloads to registered consumer endpoints.
"""

from __future__ import annotations

from pitwall.webhook_dispatcher.dispatcher import (
    DEFAULT_RETRY_DELAYS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    DeliveryOutcome,
    dispatch_completion,
)
from pitwall.webhook_dispatcher.signer import sign, verify

__all__ = [
    "DeliveryOutcome",
    "MAX_ATTEMPTS",
    "DEFAULT_RETRY_DELAYS",
    "DEFAULT_TIMEOUT_SECONDS",
    "dispatch_completion",
    "sign",
    "verify",
]
