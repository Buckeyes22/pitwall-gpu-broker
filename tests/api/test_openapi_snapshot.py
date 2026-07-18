"""Task 10: OpenAPI schema generates and contains the expected paths.

Guards against route regressions and produces the artifact release program fuzzes with
schemathesis. Asserts app.openapi() builds without error and that a
representative set of Pitwall paths is present.
"""

from __future__ import annotations

from tests.conftest import _env_for_app, _import_app

_EXPECTED_PATHS = {
    "/v1/capabilities",
    "/v1/capabilities/{name}",
    "/v1/admin/capabilities",
    "/v1/admin/capabilities/{capability_id}",
    "/v1/providers",
    "/v1/providers/{provider_id}",
    "/v1/admin/providers",
    "/v1/inference",
    "/v1/jobs/{workload_id}",
    "/v1/jobs/{workload_id}/result",
    "/v1/leases",
    "/v1/leases/{lease_id}",
    "/v1/admin/kill-switch",
    "/v1/admin/audit-capability/{name}",
    "/v1/webhook-subscriptions",
}


def test_openapi_builds_and_has_paths() -> None:
    mod = _import_app(_env_for_app())
    schema = mod.app.openapi()
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"]
    paths = set(schema["paths"].keys())
    missing = _EXPECTED_PATHS - paths
    assert not missing, f"OpenAPI missing expected paths: {sorted(missing)}"


def test_openapi_is_deterministic() -> None:
    mod = _import_app(_env_for_app())
    a = mod.app.openapi()
    b = mod.app.openapi()
    assert set(a["paths"].keys()) == set(b["paths"].keys())
