"""Characterization tests for resolver/service.py Stage 1+2 selection.

Locks in resolver behavior using conftest factories + AsyncMock repos. No DB.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pytest

from pitwall.resolver.exceptions import (
    CapabilityDisabledError,
    CapabilityNotFoundError,
    NoHealthyProviderError,
    ProviderNotFoundError,
)
from pitwall.resolver.service import (
    resolve_capability,
    resolve_capability_record,
    select_stage12_provider,
)
from pitwall.routing import RoutingRequest
from tests.conftest import make_llm_capability, make_provider

TZ_NOW = dt.datetime(2026, 5, 28, 12, 0, 0, tzinfo=dt.UTC)


def _cap_repo(*, by_name=None, by_id=None) -> AsyncMock:
    repo = AsyncMock()
    repo.get_by_name.return_value = by_name
    repo.get.return_value = by_id
    return repo


def _prov_repo(*, listed=None, by_id=None) -> AsyncMock:
    repo = AsyncMock()
    repo.list.return_value = list(listed or [])
    repo.get.return_value = by_id
    return repo


def _request() -> RoutingRequest:
    return RoutingRequest(capability_name="llm.qwen3-32b", capability_id="cap_llm_qwen3_32b")


@pytest.mark.anyio
async def test_resolve_capability_record_by_name() -> None:
    cap = make_llm_capability()
    repo = _cap_repo(by_name=cap)
    out = await resolve_capability_record("llm.qwen3-32b", repo)
    assert out is cap
    repo.get.assert_not_called()


@pytest.mark.anyio
async def test_resolve_capability_record_falls_back_to_id() -> None:
    cap = make_llm_capability()
    repo = _cap_repo(by_name=None, by_id=cap)
    out = await resolve_capability_record("cap_llm_qwen3_32b", repo)
    assert out is cap
    repo.get.assert_awaited_once_with("cap_llm_qwen3_32b")


@pytest.mark.anyio
async def test_resolve_capability_record_not_found_raises() -> None:
    repo = _cap_repo(by_name=None, by_id=None)
    with pytest.raises(CapabilityNotFoundError):
        await resolve_capability_record("missing", repo)


@pytest.mark.anyio
async def test_resolve_capability_disabled_raises() -> None:
    cap = make_llm_capability(enabled=False)
    cap_repo = _cap_repo(by_name=cap)
    prov_repo = _prov_repo()
    with pytest.raises(CapabilityDisabledError):
        await resolve_capability("llm.qwen3-32b", capability_repo=cap_repo, provider_repo=prov_repo)


@pytest.mark.anyio
async def test_resolve_capability_explicit_provider_not_found_raises() -> None:
    cap = make_llm_capability()
    cap_repo = _cap_repo(by_name=cap)
    prov_repo = _prov_repo(by_id=None)
    with pytest.raises(ProviderNotFoundError):
        await resolve_capability(
            "llm.qwen3-32b",
            capability_repo=cap_repo,
            provider_repo=prov_repo,
            provider_id="prov_missing",
        )


@pytest.mark.anyio
async def test_resolve_capability_selects_lowest_priority() -> None:
    cap = make_llm_capability()
    p_lo = make_provider(id="prov_lo", name="a", priority=1)
    p_hi = make_provider(id="prov_hi", name="b", priority=5)
    cap_repo = _cap_repo(by_name=cap)
    prov_repo = _prov_repo(listed=[p_hi, p_lo])
    res = await resolve_capability(
        "llm.qwen3-32b",
        capability_repo=cap_repo,
        provider_repo=prov_repo,
        now=TZ_NOW,
    )
    assert res.selected_provider_id == "prov_lo"
    assert res.provider_id == "prov_lo"
    assert [p.id for p in res.eligible_providers] == ["prov_lo", "prov_hi"]


def test_select_stage12_no_eligible_raises() -> None:
    cap = make_llm_capability()
    unhealthy = make_provider(id="prov_u", health_status="unhealthy")
    with pytest.raises(NoHealthyProviderError):
        select_stage12_provider(_request(), [unhealthy], capability=cap, now=TZ_NOW)


def test_select_stage12_eliminates_disabled_and_unhealthy() -> None:
    cap = make_llm_capability()
    healthy = make_provider(id="prov_ok", name="ok", priority=1, health_status="healthy")
    disabled = make_provider(id="prov_dis", name="dis", priority=2, enabled=False)
    unhealthy = make_provider(id="prov_un", name="un", priority=3, health_status="degraded")
    res = select_stage12_provider(
        _request(), [healthy, disabled, unhealthy], capability=cap, now=TZ_NOW
    )
    assert res.selected_provider_id == "prov_ok"
    eliminated_ids = {e.provider_id for e in res.eliminated}
    assert "prov_dis" in eliminated_ids
    assert "prov_un" in eliminated_ids


def test_select_stage12_to_dict_shape() -> None:
    cap = make_llm_capability()
    healthy = make_provider(id="prov_ok", name="ok", priority=1)
    res = select_stage12_provider(_request(), [healthy], capability=cap, now=TZ_NOW)
    d = res.to_dict()
    assert d["capability_id"] == cap.id
    assert d["capability_name"] == cap.name
    assert d["selected_provider_id"] == "prov_ok"
    assert d["eligible_provider_ids"] == ["prov_ok"]
    assert d["eliminated"] == []


def test_select_stage12_naive_now_raises_value_error() -> None:
    cap = make_llm_capability()
    healthy = make_provider(id="prov_ok", priority=1)
    naive = dt.datetime(2026, 5, 28, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone"):
        select_stage12_provider(_request(), [healthy], capability=cap, now=naive)


def test_select_stage12_none_now_uses_utc_now() -> None:
    cap = make_llm_capability()
    healthy = make_provider(id="prov_ok", priority=1)
    res = select_stage12_provider(_request(), [healthy], capability=cap, now=None)
    assert res.selected_provider_id == "prov_ok"
