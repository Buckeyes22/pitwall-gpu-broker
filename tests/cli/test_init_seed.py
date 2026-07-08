from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pitwall import cli
from pitwall.core.enums import CapabilityClass, CapabilitySource, CostMode, ProviderType
from pitwall.core.models import Capability, Provider


def _capability(
    *,
    id: str = "cap_embedding_demo",
    name: str = "embedding.demo",
    source: CapabilitySource = CapabilitySource.API,
) -> Capability:
    now = dt.datetime(2026, 5, 31, 12, 0, 0, tzinfo=dt.UTC)
    return Capability(
        id=id,
        name=name,
        version="1.0.0",
        class_=CapabilityClass.EMBEDDING,
        description="Demo embedding capability",
        cost_mode=CostMode.PER_SECOND,
        source=source,
        created_at=now,
        updated_at=now,
    )


def _provider(
    *,
    id: str = "prov_demo_runpod_lb",
    capability_id: str = "cap_embedding_demo",
    health_status: str = "unknown",
    source: CapabilitySource = CapabilitySource.YAML,
) -> Provider:
    now = dt.datetime(2026, 5, 31, 12, 0, 0, tzinfo=dt.UTC)
    return Provider(
        id=id,
        capability_id=capability_id,
        name="demo-runpod-lb",
        provider_type=ProviderType.SERVERLESS_LB,
        runpod_endpoint_id="eptest00000000",
        runpod_template_id=None,
        region="US-EXAMPLE-1",
        cloud_type=None,
        config={
            "gpu_class": "NVIDIA L4",
            "lb_base_url": "https://eptest00000000.api.runpod.ai",
            "cost": {"mode": "per_second", "per_second_active": "0.001"},
            "workers": {"workers_min": 0},
            "idle_timeout_minutes": 0,
            "flash_boot_verified": False,
            "max_payload_mb": 30,
            "request_timeout_s": 330,
        },
        priority=1,
        enabled=True,
        health_status=health_status,
        consecutive_failures=0,
        cooldown_trips=0,
        cold_start_p50_ms=None,
        cold_start_p95_ms=None,
        recent_error_rate=0.0,
        cooldown_until=None,
        source=source,
        last_applied_yaml_hash=None,
        updated_at=now,
    )


def _write_seed_files(seed_dir: Path) -> tuple[Path, Path]:
    capabilities = seed_dir / "capabilities.yaml"
    providers = seed_dir / "providers.yaml"
    capabilities.write_text(
        """
capabilities:
  - name: embedding.demo
    version: 1.0.0
    class: embedding
    description: Demo embedding capability
    cost_mode: per_second
""".lstrip(),
        encoding="utf-8",
    )
    providers.write_text(
        """
providers:
  - name: demo-runpod-lb
    capability: embedding.demo
    endpoint_id: eptest00000000
    provider_type: serverless_lb
    region: US-EXAMPLE-1
    gpu_class: NVIDIA L4
    priority: 1
    cost:
      mode: per_second
      per_second_active: "0.001"
""".lstrip(),
        encoding="utf-8",
    )
    return capabilities, providers


@pytest.mark.anyio
async def test_create_capability_happy_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    created = _capability()
    upsert = AsyncMock(return_value=created)

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=MagicMock())),
        patch("pitwall.db.repository.CapabilityRepository.upsert", new=upsert),
    ):
        ns = cli._parse_create_capability_args(
            [
                "--name",
                "embedding.demo",
                "--class",
                "embedding",
                "--cost-mode",
                "per_second",
                "--description",
                "Demo embedding capability",
            ]
        )
        rc = await cli._create_capability_async(ns)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Capability created: cap_embedding_demo" in out
    upsert.assert_awaited_once()
    assert upsert.await_args.kwargs["name"] == "embedding.demo"
    assert upsert.await_args.kwargs["class_"] == "embedding"
    assert upsert.await_args.kwargs["cost_mode"] == "per_second"


@pytest.mark.anyio
async def test_create_capability_rejects_blank_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch("pitwall.db.get_pool", new=AsyncMock(side_effect=AssertionError("no DB"))):
        ns = cli._parse_create_capability_args(
            ["--name", "  ", "--class", "embedding", "--cost-mode", "per_second"]
        )
        rc = await cli._create_capability_async(ns)

    err = capsys.readouterr().err
    assert rc == 1
    assert "capability name cannot be empty" in err


@pytest.mark.anyio
async def test_seed_loader_applies_capability_and_provider(tmp_path: Path) -> None:
    from pitwall.seed import apply_seed_files

    cap_file, provider_file = _write_seed_files(tmp_path)
    created_capabilities: list[Capability] = []
    created_providers: list[Provider] = []

    async def create_capability(_self: object, capability: Capability) -> Capability:
        created_capabilities.append(capability)
        return capability

    async def get_capability_by_name(_self: object, name: str) -> Capability | None:
        return next((cap for cap in created_capabilities if cap.name == name), None)

    async def get_capability(_self: object, capability_id: str) -> Capability | None:
        return next((cap for cap in created_capabilities if cap.id == capability_id), None)

    async def get_provider_by_name(_self: object, _name: str) -> Provider | None:
        return None

    async def create_provider(_self: object, provider: Provider) -> Provider:
        created_providers.append(provider)
        return provider

    with (
        patch("pitwall.seed.CapabilityRepository.create", new=create_capability),
        patch("pitwall.seed.CapabilityRepository.get_by_name", new=get_capability_by_name),
        patch("pitwall.seed.CapabilityRepository.get", new=get_capability),
        patch("pitwall.seed.ProviderRepository.get_by_name", new=get_provider_by_name),
        patch("pitwall.seed.ProviderRepository.create", new=create_provider),
    ):
        result = await apply_seed_files([cap_file, provider_file], pool=MagicMock())

    assert [cap.name for cap in result.capabilities] == ["embedding.demo"]
    assert [provider.name for provider in result.providers] == ["demo-runpod-lb"]
    assert created_capabilities[0].source == CapabilitySource.YAML
    assert created_providers[0].capability_id == "cap_embedding_demo"
    assert created_providers[0].health_status == "unknown"
    assert created_providers[0].config["cost"]["per_second_active"] == "0.001"


@pytest.mark.anyio
async def test_init_from_seed_marks_created_provider_healthy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_seed_files(tmp_path)
    created_capabilities: list[Capability] = []
    created_providers: list[Provider] = []
    patched_provider_ids: list[str] = []

    async def create_capability(_self: object, capability: Capability) -> Capability:
        created_capabilities.append(capability)
        return capability

    async def get_capability_by_name(_self: object, name: str) -> Capability | None:
        return next((cap for cap in created_capabilities if cap.name == name), None)

    async def get_capability(_self: object, capability_id: str) -> Capability | None:
        return next((cap for cap in created_capabilities if cap.id == capability_id), None)

    async def get_provider_by_name(_self: object, _name: str) -> Provider | None:
        return None

    async def create_provider(_self: object, provider: Provider) -> Provider:
        created_providers.append(provider)
        return provider

    async def get_provider(_self: object, provider_id: str) -> Provider | None:
        return next(
            (provider for provider in created_providers if provider.id == provider_id), None
        )

    async def patch_provider(_self: object, provider_id: str, **kwargs: object) -> Provider:
        patched_provider_ids.append(provider_id)
        assert kwargs["health_status"] == "healthy"
        assert kwargs["consecutive_failures"] == 0
        assert kwargs["cooldown_trips"] == 0
        assert kwargs["recent_error_rate"] == 0.0
        assert kwargs["cooldown_until"] is None
        provider = await get_provider(_self, provider_id)
        assert provider is not None
        return provider.model_copy(update={"health_status": "healthy"})

    with (
        patch("pitwall.db.get_pool", new=AsyncMock(return_value=MagicMock())),
        patch("pitwall.seed.CapabilityRepository.create", new=create_capability),
        patch("pitwall.seed.CapabilityRepository.get_by_name", new=get_capability_by_name),
        patch("pitwall.seed.CapabilityRepository.get", new=get_capability),
        patch("pitwall.seed.ProviderRepository.get_by_name", new=get_provider_by_name),
        patch("pitwall.seed.ProviderRepository.create", new=create_provider),
        patch("pitwall.db.repository.ProviderRepository.get", new=get_provider),
        patch("pitwall.db.repository.ProviderRepository.patch", new=patch_provider),
    ):
        ns = cli._parse_init_args(["--from-seed", str(tmp_path), "--non-interactive"])
        rc = await cli._init_async(ns)

    out = capsys.readouterr().out
    assert rc == 0
    assert patched_provider_ids == ["prov_demo_runpod_lb"]
    assert "Pitwall init complete" in out
    assert "demo-runpod-lb" in out
    assert "curl -s -X POST" in out
