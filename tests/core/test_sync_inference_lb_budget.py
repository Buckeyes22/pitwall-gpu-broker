"""Sync inference LB calls must resolve within a single client budget.

The RunPod load-balancer surface *holds* requests while no worker is ready,
so every attempt against a cold/broken endpoint burns the client's full
read-timeout budget (330s). With the client's default retry_attempts=4 a
single POST /v1/inference would hang ~22 minutes before surfacing an error.
The sync path must make exactly one attempt so callers see the provider's
failure within the documented 330s budget.

Found by the L3 live release drill: a stuck-initializing worker (unreachable
image registry) held four smoke requests for 20+ minutes server-side.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

import pitwall.core.inference as inference
from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider

_NOW = dt.datetime(2026, 5, 28, 12, 0, tzinfo=dt.UTC)


def _lb_provider() -> Provider:
    return Provider(
        id="prov_lb",
        capability_id="cap_embedding_bge_m3",
        name="BGE-M3 LB provider",
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id="eptest00000000",
        config={"cost": {"kind": "per_second", "per_second_active": "0.00155"}},
        priority=1,
        updated_at=_NOW,
    )


class _CapturingLBClient:
    """Stands in for ServerlessLBClient and records constructor kwargs."""

    captured_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).captured_kwargs = kwargs

    async def embed(self, **_: Any) -> dict[str, Any]:
        return {"dense": [[0.0]], "sparse": None, "colbert": None, "raw": {}}

    async def aclose(self) -> None:
        pass


@pytest.mark.anyio
async def test_sync_lb_inference_makes_a_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inference, "ServerlessLBClient", _CapturingLBClient)

    await inference._run_serverless_lb_inference(_lb_provider(), {"texts": ["x"]}, "key")

    assert _CapturingLBClient.captured_kwargs.get("retry_attempts") == 1, (
        "sync inference must not retry LB calls: the LB holds requests while no "
        "worker is ready, so each retry burns the full 330s timeout budget "
        f"(got kwargs: {_CapturingLBClient.captured_kwargs})"
    )
