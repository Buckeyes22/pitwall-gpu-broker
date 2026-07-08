"""Regression coverage for API route precedence around wildcard proxy routes."""

from __future__ import annotations

import sys

import pytest
from fastapi import FastAPI

from tests.api._route_helpers import iter_effective_routes, route_fully_matches


def _clear_api_modules() -> None:
    for key in [module for module in sys.modules if module.startswith("pitwall.api")]:
        del sys.modules[key]


@pytest.fixture()
def api_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    _clear_api_modules()
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("PITWALL_ADMIN_SECRET", raising=False)

    from pitwall.api.app import app

    yield app

    app.dependency_overrides.clear()
    _clear_api_modules()


def _full_match_names(app: FastAPI, *, method: str, path: str) -> list[str]:
    names: list[str] = []
    for route in iter_effective_routes(app.router.routes):
        if route_fully_matches(route, method=method, path=path):
            names.append(route.name)
    return names


@pytest.mark.parametrize(
    ("method", "path", "expected_names"),
    [
        ("POST", "/v1/inference", ["create_inference"]),
        ("POST", "/v1/leases", ["create_lease"]),
        ("POST", "/v1/jobs", []),
    ],
)
def test_existing_v1_routes_keep_their_route_table_precedence(
    api_app: FastAPI,
    method: str,
    path: str,
    expected_names: list[str],
) -> None:
    matches = _full_match_names(api_app, method=method, path=path)

    assert matches == expected_names
    assert "openai_proxy" not in matches


def test_openai_proxy_wildcard_remains_scoped_to_openai_prefix(api_app: FastAPI) -> None:
    matches = _full_match_names(
        api_app,
        method="POST",
        path="/v1/openai/llm.qwen3-32b/v1/chat/completions",
    )

    assert matches == ["openai_proxy"]
