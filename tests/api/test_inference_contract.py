"""release program contract tests for POST /v1/inference business errors."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from pitwall.core.enums import CapabilityClass, CapabilitySource, ProviderType
from pitwall.core.inference import GatedSyncInference
from pitwall.core.models import Capability, Provider
from pitwall.cost.budget_gate import BudgetRejected, BudgetSnapshot
from pitwall.resolver import CapabilityDisabledError, NoHealthyProviderError
from pitwall.resolver.service import Stage12Resolution
from tests.api._contract_helpers import build_app, client_for, override

pytestmark = pytest.mark.anyio

_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)
_CAPABILITY_ID = "cap_bge_m3"
_CAPABILITY_NAME = "embedding.bge-m3"


def _capability() -> Capability:
    return Capability(
        id=_CAPABILITY_ID,
        name=_CAPABILITY_NAME,
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        cost_mode="per_second",
        source=CapabilitySource.API,
        enabled=True,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _provider() -> Provider:
    return Provider(
        id="prov_bge_m3",
        capability_id=_CAPABILITY_ID,
        name="prov_bge_m3",
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id="eptest00000000",
        config={
            "lb_base_url": "https://eptest00000000.api.runpod.ai",
            "cost": {"mode": "per_second", "per_second_active": "0.000123"},
        },
        priority=1,
        enabled=True,
        health_status="healthy",
        updated_at=_NOW,
    )


def _resolution() -> Stage12Resolution:
    provider = _provider()
    return Stage12Resolution(
        capability=_capability(),
        provider=provider,
        eligible_providers=(provider,),
    )


def _snapshot() -> BudgetSnapshot:
    return BudgetSnapshot(
        monthly_budget_usd=Decimal("50.0"),
        per_request_max_usd=Decimal("10.0"),
        mtd_spend_usd=Decimal("49.5"),
        estimate_usd=Decimal("1.0"),
        budget_remaining_usd=Decimal("0.5"),
    )


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "capability_id": _CAPABILITY_ID,
        "text": "hello",
    }
    body.update(overrides)
    return body


def _idempotency_pool() -> MagicMock:
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": "wkl_old",
        "state": "completed",
        "input": {"text": "ORIGINAL"},
        "result": {"ok": True},
    }
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm
    return pool


def _empty_idempotency_pool() -> MagicMock:
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm
    return pool


def _setup(clear_app_module, *, pool: MagicMock | None = None):
    pool = pool or MagicMock()
    mod = build_app(pool=pool)
    import pitwall.api.routes.inference as inference_mod

    override(mod, inference_mod._pool, pool)
    override(mod, inference_mod._capability_repo, AsyncMock())
    override(mod, inference_mod._provider_repo, AsyncMock())
    return mod, inference_mod


async def test_budget_rejected_402(clear_app_module) -> None:
    mod, inference_mod = _setup(clear_app_module)
    inference_mod.resolve_inference_target = AsyncMock(return_value=_resolution())
    inference_mod.run_sync_inference = AsyncMock(
        side_effect=BudgetRejected("monthly_budget", _snapshot())
    )

    async with client_for(mod) as client:
        resp = await client.post("/v1/inference", json=_body())

    assert resp.status_code == 402
    assert resp.json()["error"] == "budget_rejected"


async def test_idempotency_mismatch_422(clear_app_module) -> None:
    mod, _ = _setup(clear_app_module, pool=_idempotency_pool())

    async with client_for(mod) as client:
        resp = await client.post(
            "/v1/inference",
            headers={"Idempotency-Key": "idem-123"},
            json=_body(text="CHANGED"),
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "idempotency_mismatch"
    assert body["original_workload_id"] == "wkl_old"


async def test_capability_disabled_409(clear_app_module) -> None:
    mod, inference_mod = _setup(clear_app_module)
    inference_mod.resolve_inference_target = AsyncMock(
        side_effect=CapabilityDisabledError(_CAPABILITY_NAME)
    )

    async with client_for(mod) as client:
        resp = await client.post("/v1/inference", json=_body())

    assert resp.status_code == 409
    assert resp.json()["error"] == "capability_disabled"


async def test_no_providers_available_503(clear_app_module) -> None:
    mod, inference_mod = _setup(clear_app_module)
    inference_mod.resolve_inference_target = AsyncMock(
        side_effect=NoHealthyProviderError(_CAPABILITY_NAME)
    )

    async with client_for(mod) as client:
        resp = await client.post("/v1/inference", json=_body())

    assert resp.status_code == 503
    assert resp.json()["error"] == "no_providers_available"


async def test_pre_spend_payload_secret_rejected_before_resolver(clear_app_module) -> None:
    mod, inference_mod = _setup(clear_app_module)
    inference_mod.resolve_inference_target = AsyncMock(return_value=_resolution())
    inference_mod.run_sync_inference = AsyncMock()

    async with client_for(mod) as client:
        resp = await client.post(
            "/v1/inference",
            json=_body(
                text="use sk-test_1234567890abcdef1234567890abcdef",
            ),
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "pre_spend_payload_rejected"
    assert body["decision"] == "block"
    assert body["findings"][0]["kind"] == "secret"
    assert body["findings"][0]["path"] == "$.text"
    assert "sk-test" not in str(body)
    inference_mod.resolve_inference_target.assert_not_awaited()
    inference_mod.run_sync_inference.assert_not_awaited()


@pytest.mark.parametrize(
    ("extra_field", "extra_value", "expected_path"),
    [
        (
            "prompt",
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            "$.prompt",
        ),
        (
            "texts",
            ["R2_SECRET_ACCESS_KEY=r2-secret-access-key-material-1234567890"],
            "$.texts[0]",
        ),
    ],
)
async def test_labeled_cloud_secret_rejected_before_resolver(
    clear_app_module,
    extra_field: str,
    extra_value: object,
    expected_path: str,
) -> None:
    mod, inference_mod = _setup(clear_app_module)
    inference_mod.resolve_inference_target = AsyncMock(return_value=_resolution())
    inference_mod.run_sync_inference = AsyncMock()

    async with client_for(mod) as client:
        resp = await client.post(
            "/v1/inference",
            json=_body(**{extra_field: extra_value}),
        )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "pre_spend_payload_rejected"
    assert body["decision"] == "block"
    assert body["findings"][0]["kind"] == "secret"
    assert body["findings"][0]["path"] == expected_path
    assert "SECRET_ACCESS_KEY" not in str(body)
    inference_mod.resolve_inference_target.assert_not_awaited()
    inference_mod.run_sync_inference.assert_not_awaited()


async def test_pre_spend_payload_pii_redacted_before_runpod_call(clear_app_module) -> None:
    mod, inference_mod = _setup(clear_app_module)
    inference_mod.resolve_inference_target = AsyncMock(return_value=_resolution())
    inference_mod.run_sync_inference = AsyncMock(
        return_value=type(
            "Gated",
            (),
            {
                "workload_id": "wkl_redacted",
                "runpod_result": {"ok": True},
                "execution_ms": 1,
            },
        )()
    )
    inference_mod.record_inference_trace = AsyncMock(return_value=None)

    async with client_for(mod) as client:
        resp = await client.post(
            "/v1/inference",
            json=_body(text="contact ada.lovelace@example.com"),
        )

    assert resp.status_code == 200
    assert resp.json()["workload_id"] == "wkl_redacted"
    called_kwargs = inference_mod.run_sync_inference.await_args.kwargs
    assert called_kwargs["capability_params"] == {"text": "contact [REDACTED:email]"}
    assert "ada.lovelace@example.com" not in str(called_kwargs)


async def test_inference_rejects_null_byte_capability_id_before_db(clear_app_module) -> None:
    """A NUL byte in the capability id is rejected at the schema boundary (422),
    never reaching the DB lookup where asyncpg raises CharacterNotInRepertoireError
    (500). Mirrors the POST /v1/leases hardening; found via schemathesis."""
    cap_repo = AsyncMock()
    mod = build_app(pool=MagicMock())
    import pitwall.api.routes.inference as inference_mod

    override(mod, inference_mod._capability_repo, cap_repo)

    async with client_for(mod) as client:
        resp = await client.post("/v1/inference", json={"capability_id": "\x00"})

    assert resp.status_code == 422
    cap_repo.get_by_name.assert_not_awaited()


async def test_concurrent_idempotent_inference_requests_coalesce_one_sync_execution(
    clear_app_module,
) -> None:
    request_count = 6
    mod, inference_mod = _setup(clear_app_module, pool=_empty_idempotency_pool())
    all_resolved = asyncio.Event()
    resolved_count = 0

    async def resolve_once_all_requests_arrive(**_: object) -> Stage12Resolution:
        nonlocal resolved_count
        resolved_count += 1
        if resolved_count == request_count:
            all_resolved.set()
        return _resolution()

    async def run_once(*_: object, **__: object) -> GatedSyncInference:
        await all_resolved.wait()
        return GatedSyncInference(
            workload_id="wkl_coalesced",
            runpod_result={"ok": True},
            execution_ms=7,
        )

    inference_mod.resolve_inference_target = AsyncMock(side_effect=resolve_once_all_requests_arrive)
    inference_mod.run_sync_inference = AsyncMock(side_effect=run_once)
    inference_mod.record_inference_trace = AsyncMock(return_value="trace-coalesced")

    async with client_for(mod) as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/v1/inference",
                    headers={"Idempotency-Key": "idem-coalesced"},
                    json=_body(text="same prompt"),
                )
                for _ in range(request_count)
            ]
        )

    assert [response.status_code for response in responses] == [200] * request_count
    assert [response.json() for response in responses] == [
        {"workload_id": "wkl_coalesced", "result": {"ok": True}}
    ] * request_count
    assert {response.headers["X-Pitwall-Trace"] for response in responses} == {"trace-coalesced"}
    assert inference_mod.run_sync_inference.await_count == 1
    assert inference_mod.record_inference_trace.await_count == 1
