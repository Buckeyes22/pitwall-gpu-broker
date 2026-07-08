"""Hypothesis profiles and shared fixtures for release program security tests."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from hypothesis import HealthCheck, settings

settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("dev", max_examples=50, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


# S4 — admin-auth surface. A fixed, non-trivial secret so the constant-time
# behavioural table can probe missing/empty/wrong/correct headers.
ADMIN_SECRET = "s3cr3t-admin-deadbeef-0123456789"


@pytest.fixture
def admin_app(clear_app_module: None) -> tuple[object, str]:
    """Import a fresh ``pitwall.api.app`` with the admin secret configured.

    The ``AdminSecretMiddleware`` reads ``PITWALL_ADMIN_SECRET`` at import time,
    so the app must be re-imported with the env in place. ``clear_app_module``
    purges the cached ``pitwall.api*`` modules first so ``_import_app``
    re-executes the module body. A fake asyncpg pool is attached so post-auth
    handlers don't blow up on a missing pool (auth is enforced by the middleware
    *before* the route runs, so any non-401 status proves the gate let the
    correct secret through).
    """
    from tests.conftest import _env_for_app, _import_app, make_asyncpg_pool

    mod = _import_app(_env_for_app(PITWALL_ADMIN_SECRET=ADMIN_SECRET))
    mod.app.state.pool = make_asyncpg_pool()
    return mod.app, ADMIN_SECRET


# S5 — inbound webhook HMAC. A fixed secret for the signed-delivery table.
WEBHOOK_SECRET = "wh00k-shared-secret-0123456789abcdef"


class _FakeInsertResult:
    def __init__(self, *, is_new: bool = True) -> None:
        self.is_new = is_new


class _FakeWebhookRepo:
    """Stand-in for WebhookDeliveryRepository so the route never touches a pool."""

    def __init__(self, pool: object) -> None:
        self._pool = pool

    async def insert_or_skip(self, **_kwargs: Any) -> _FakeInsertResult:
        return _FakeInsertResult(is_new=True)


def _purge_webhook_receiver() -> None:
    for name in [k for k in sys.modules if k.startswith("pitwall.webhook_receiver")]:
        del sys.modules[name]


@pytest.fixture
def webhook_app_builder(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Any]]:
    """Return a builder that imports a fresh ``pitwall.webhook_receiver`` app.

    ``pitwall.webhook_receiver`` reads ``PITWALL_WEBHOOK_SECRET`` at import time,
    so the module must be re-imported after the env is set. The builder purges
    the cached module, sets/clears the secret, imports fresh, swaps the delivery
    repository for an in-memory fake (no real pool needed), and disables Redis so
    terminal-status enqueue is a no-op.

    Usage: ``module = webhook_app_builder(secret=WEBHOOK_SECRET)`` (or
    ``secret=None`` for the current insecure default).
    """

    def _build(
        secret: str | None = None,
        *,
        previous_secrets: list[str] | None = None,
        max_body_bytes: int | None = None,
        rate_limit: str | None = None,
    ) -> Any:
        _purge_webhook_receiver()
        if secret is None:
            monkeypatch.delenv("PITWALL_WEBHOOK_SECRET", raising=False)
        else:
            monkeypatch.setenv("PITWALL_WEBHOOK_SECRET", secret)
        if previous_secrets is None:
            monkeypatch.delenv("PITWALL_WEBHOOK_PREVIOUS_SECRETS", raising=False)
        else:
            import json

            monkeypatch.setenv("PITWALL_WEBHOOK_PREVIOUS_SECRETS", json.dumps(previous_secrets))
        if max_body_bytes is None:
            monkeypatch.delenv("PITWALL_WEBHOOK_MAX_BODY_BYTES", raising=False)
        else:
            monkeypatch.setenv("PITWALL_WEBHOOK_MAX_BODY_BYTES", str(max_body_bytes))
        if rate_limit is None:
            monkeypatch.delenv("PITWALL_WEBHOOK_RATE_LIMIT", raising=False)
        else:
            monkeypatch.setenv("PITWALL_WEBHOOK_RATE_LIMIT", rate_limit)
        module = importlib.import_module("pitwall.webhook_receiver")
        monkeypatch.setattr(module, "WebhookDeliveryRepository", _FakeWebhookRepo)
        module.app.state.pool = object()
        module.app.state.redis_settings = None
        return module

    yield _build
    _purge_webhook_receiver()
