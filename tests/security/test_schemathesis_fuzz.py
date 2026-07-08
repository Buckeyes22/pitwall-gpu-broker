"""S7: schemathesis fuzz of the public API — no server errors on any input.

Generates schema-derived and deliberately malformed requests from the live
``app.openapi()`` and drives the in-process ASGI app, asserting that no public
route ever returns a 5xx — i.e. fuzzed/garbage input is always handled (4xx),
never crashes a handler. A fake asyncpg pool is attached so data-reading routes
return clean 404s instead of false-500ing on a missing pool, which means any
5xx that surfaces here is a genuine input-handling bug.

Scope notes:
  * Only the ``not_a_server_error`` check is asserted. The default
    ``response_schema_conformance`` check is intentionally separate from this
    security property. Contract conformance is covered by API schema tests; this
    lane proves every generated request terminates without a server error.

schemathesis 4.x API: ``schemathesis.openapi.from_asgi`` + a *synchronous*
``@schema.parametrize()`` test calling ``case.call_and_validate()``.
"""

from __future__ import annotations

import importlib.util
import warnings
from typing import Any

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.python import asgi as schemathesis_asgi
from starlette_testclient import TestClient

import pitwall.api.app as _pitwall_api_app
from tests.conftest import make_asyncpg_pool


class _ClosingTestClient(TestClient):
    """Close the second lifespan memory channel omitted by starlette-testclient 0.4.1."""

    def __exit__(self, *args: Any) -> None:
        stream_receive = self.stream_receive
        try:
            super().__exit__(*args)
        finally:
            # The upstream client closes stream_send in wait_shutdown but leaves
            # this independent channel open until cyclic GC. Close both halves
            # synchronously after the lifespan task has stopped.
            stream_receive.send_stream.close()
            stream_receive.receive_stream.close()


schemathesis_asgi.get_client = _ClosingTestClient

pytestmark = [
    pytest.mark.security,
    pytest.mark.fuzz,
    # Schemathesis 4.10/httpx leaves its per-example AnyIO memory transport
    # streams for cyclic GC on Python 3.13.  Scope the upstream warning waiver
    # to this harness; project ResourceWarnings remain errors everywhere else.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObject.*Stream.*:ResourceWarning"),
]


def _build_isolated_fuzz_app() -> Any:
    # Load a PRIVATE copy of app.py as a throwaway module, yielding a fresh,
    # fully isolated FastAPI instance. This shares nothing mutable with the
    # global app or sys.modules['pitwall.api.*'], so the fuzz can attach a fake
    # pool without leaking into other tests AND without a module-level purge that
    # would rebind sys.modules out from under other test files' top-level imports
    # (which previously made the kill-switch tests issue real calls). The route
    # functions still come from the shared, already-imported pitwall.api.routes.*
    # modules; only the app object + its state are private.
    spec = importlib.util.spec_from_file_location(
        "_pitwall_fuzz_app_private", _pitwall_api_app.__file__
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.app.state.pool = make_asyncpg_pool()
    mod.app.state.runpod_api_key = "test-key"
    mod.app.state.redis = _FakeRedis()
    return mod.app


class _FakeRedis:
    async def ping(self) -> bool:
        return True


with warnings.catch_warnings():
    # FastAPI derives op-ids from function names → one benign duplicate warning.
    warnings.simplefilter("ignore")
    schema = schemathesis.openapi.from_asgi("/openapi.json", _build_isolated_fuzz_app())


# Several public params carry a NUL-byte reject pattern (^[^\x00]+$, the
# null-byte hardening), so Hypothesis filters out a fair fraction of generated
# strings for some operations (notably POST /v1/inference). That legitimately
# trips the `filter_too_much` health check on unlucky seeds — flaky, and it fired
# in coverage-combined though the dedicated security-fuzz job passed. The filtering
# is expected and harmless (the property is just "no 5xx"), so suppress that one
# health check to make the gate deterministic.
@schema.parametrize()
@settings(suppress_health_check=[HealthCheck.filter_too_much])
def test_public_api_no_server_errors(case: Any) -> None:
    case.call_and_validate(checks=[not_a_server_error])
