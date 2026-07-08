"""Shared builders for release program contract tests — thin wrappers over conftest.

Leading underscore => not collected as a test module. Adds no new app-build
mechanism; wraps the existing conftest seams (_env_for_app/_import_app/
make_asyncpg_pool). No network, no DB.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import httpx

from tests.conftest import _env_for_app, _import_app, make_asyncpg_pool


def build_app(
    *,
    secret: str | None = None,
    pool: MagicMock | None = None,
    **state: Any,
):
    """Import a fresh app module with optional admin secret and attached state.

    Caller is responsible for clearing pitwall.api from sys.modules first
    (use the `clear_app_module` fixture). Returns the imported module.
    """
    env: dict[str, str] = _env_for_app()
    if secret is not None:
        env["PITWALL_ADMIN_SECRET"] = secret
    mod = _import_app(env)
    if secret is not None:
        mod.app.state.test_admin_secret = secret
    mod.app.state.pool = pool if pool is not None else make_asyncpg_pool()
    mod.app.state.runpod_api_key = "test-key"
    for key, value in state.items():
        setattr(mod.app.state, key, value)
    return mod


@asynccontextmanager
async def client_for(mod):
    secret = getattr(mod.app.state, "test_admin_secret", None)
    headers = {"X-Pitwall-Secret": secret} if secret else None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mod.app),
        base_url="http://test",
        headers=headers,
    ) as client:
        yield client


def override(mod, dep, value) -> None:
    mod.app.dependency_overrides[dep] = lambda: value
