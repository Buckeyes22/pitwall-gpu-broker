"""Task 6: error envelope consistency.

Every typed API error subclasses PitwallApiError and renders a stable body
``{"error": <error_code>, ...}`` (via to_response_body()) with its declared
status_code. Verified vs src/pitwall/api/exceptions.py 2026-05-30: the method is
``to_response_body``; CapabilityNotFound/ProviderNotFound take a single
positional arg and add a context key (name / id).
"""

from __future__ import annotations

import inspect

import pytest

from pitwall.api import exceptions as exc
from tests.api._contract_helpers import build_app, client_for

pytestmark = pytest.mark.anyio


def _error_subclasses():
    for _name, obj in inspect.getmembers(exc, inspect.isclass):
        if issubclass(obj, exc.PitwallApiError) and obj is not exc.PitwallApiError:
            yield obj


def test_every_typed_error_has_code_and_status() -> None:
    subclasses = list(_error_subclasses())
    assert subclasses  # sanity: we found the typed errors
    for cls in subclasses:
        assert isinstance(cls.status_code, int)
        assert 400 <= cls.status_code < 600
        assert isinstance(cls.error_code, str) and cls.error_code
        # Render the body when the error is default-constructible. Some errors
        # require context args; their class-level code/status are pinned above.
        try:
            instance = cls()
        except TypeError:
            continue
        assert instance.to_response_body()["error"] == cls.error_code


def test_capability_not_found_envelope_shape() -> None:
    err = exc.CapabilityNotFound("embedding.missing")
    body = err.to_response_body()
    assert body["error"] == "capability_not_found"
    assert body["name"] == "embedding.missing"


def test_provider_not_found_envelope_shape() -> None:
    err = exc.ProviderNotFound("prov_x")
    body = err.to_response_body()
    assert body["error"] == "provider_not_found"


@pytest.mark.parametrize(
    "path,expected_code",
    [
        ("/v1/capabilities/missing.cap", "capability_not_found"),
        ("/v1/providers/prov_missing", "provider_not_found"),
    ],
    ids=["capability_not_found", "provider_not_found"],
)
async def test_live_routes_return_error_envelope(
    clear_app_module, path: str, expected_code: str
) -> None:
    from unittest.mock import AsyncMock

    mod = build_app()
    if "capabilities" in path:
        from pitwall.api.capability_routes import _repo as dep
    else:
        from pitwall.api.provider_routes import _repo as dep
    repo = AsyncMock()
    repo.get.return_value = None
    repo.get_by_name.return_value = None
    mod.app.dependency_overrides[dep] = lambda: repo

    async with client_for(mod) as client:
        resp = await client.get(path)
    assert resp.status_code == 404
    assert resp.json()["error"] == expected_code
