"""Security: configured secret values are never echoed; only names appear."""

from __future__ import annotations

import importlib
import json
import os
from typing import Any

import httpx
import pytest

from pitwall.config import require_runtime_env

pytestmark = pytest.mark.security

_SECRET_ENV = {
    "RUNPOD_API_KEY": "rpk_DEADBEEF_secret_value",
    "RESEND_API_KEY": "re_DEADBEEF_secret_value",
    "LANGFUSE_SECRET_KEY": "lf_sk_DEADBEEF_secret",
    "LANGFUSE_PUBLIC_KEY": "lf_pk_DEADBEEF_secret",
    "R2_ACCESS_KEY": "r2_ak_DEADBEEF_secret",
    "R2_SECRET_KEY": "r2_sk_DEADBEEF_secret",
    "R2_PARENT_ACCESS_KEY_ID": "r2_parent_DEADBEEF_secret",
    "CLOUDFLARE_API_TOKEN": "cf_DEADBEEF_secret_token",
    "PITWALL_ADMIN_SECRET": "admin_DEADBEEF_secret",
}
_MARKER = "DEADBEEF"


def _set_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _SECRET_ENV.items():
        monkeypatch.setenv(name, value)


def test_require_runtime_env_reports_names_only(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_secret_env(monkeypatch)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    with pytest.raises(SystemExit) as exc:
        require_runtime_env("api")
    assert exc.value.code == os.EX_CONFIG

    err = capsys.readouterr().err
    assert "RUNPOD_API_KEY" in err
    assert "DATABASE_URL" in err
    assert "REDIS_URL" in err
    assert _MARKER not in err, f"secret value leaked into stderr: {err!r}"


class _FakeRedis:
    async def ping(self) -> bool:
        return True


@pytest.mark.anyio
async def test_health_endpoints_contain_no_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    fake_asyncpg_pool: Any,
) -> None:
    _set_secret_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    import pitwall.api.app as app_module

    importlib.reload(app_module)
    app = app_module.app
    app.state.pool = fake_asyncpg_pool
    app.state.redis = _FakeRedis()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/health", "/healthz", "/v1/health"):
            response = await client.get(path)
            assert response.status_code == 200
            assert _MARKER not in response.text, f"secret value leaked in {path}: {response.text!r}"


def test_error_envelope_contains_no_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pitwall.api.exceptions import PitwallApiError, ProviderUnavailable, RateLimited

    _set_secret_env(monkeypatch)
    bodies = [
        PitwallApiError().to_response_body(),
        ProviderUnavailable("llm.qwen3-32b", chain=["prov_a", "prov_b"]).to_response_body(),
        RateLimited(retry_after_s=1.25).to_response_body(),
    ]

    assert _MARKER not in json.dumps(bodies)
