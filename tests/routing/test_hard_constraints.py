"""Tests for Stage 1 hard-constraint filtering — ."""

from __future__ import annotations

from datetime import UTC, datetime

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.routing import (
    EliminationReason,
    RoutingRequest,
    evaluate_hard_constraints,
    filter_hard_constraints,
)

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC)


def _provider(
    provider_id: str,
    *,
    capability_id: str = "cap_embed",
    provider_type: ProviderType = ProviderType.SERVERLESS_QUEUE,
    region: str | None = "US-KS-2",
    config: dict[str, object] | None = None,
) -> Provider:
    return Provider(
        id=provider_id,
        capability_id=capability_id,
        name=f"{provider_id}-name",
        provider_type=provider_type,
        region=region,
        config=config or {},
        priority=0,
        updated_at=_NOW,
    )


def _capability() -> Capability:
    return Capability(
        id="cap_embed",
        name="embedding.bge-m3",
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode="per_second",
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_filter_preserves_order_and_captures_eliminations() -> None:
    request = RoutingRequest(
        capability_name="embedding.bge-m3",
        required_gpu_class="NVIDIA L4",
        required_cuda_min="12.4",
    )
    providers = [
        _provider(
            "prov_ok",
            config={
                "gpu_type_priority": ["NVIDIA L4", "NVIDIA RTX A4000"],
                "constraints": {"cuda_min": "12.8"},
            },
        ),
        _provider(
            "prov_wrong_gpu",
            config={
                "gpu_type_priority": ["NVIDIA RTX A4000"],
                "constraints": {"cuda_min": "12.8"},
            },
        ),
    ]

    result = filter_hard_constraints(request, providers, capability=_capability())

    assert [provider.id for provider in result.passed] == ["prov_ok"]
    assert [item.provider_id for item in result.eliminated] == ["prov_wrong_gpu"]
    assert result.eliminated[0].reason is EliminationReason.GPU_CLASS_MISMATCH
    assert result.eliminated[0].reasons == (EliminationReason.GPU_CLASS_MISMATCH,)


def test_evaluate_captures_all_stage_1_reasons_without_short_circuiting() -> None:
    request = RoutingRequest(
        capability_name="embedding.bge-m3",
        payload_bytes=(31 * 1024 * 1024),
        required_gpu_class="NVIDIA L4",
        required_region="US-KS-2",
        required_volume_id="vol_required",
        required_cuda_min="12.4",
    )
    provider = _provider(
        "prov_bad",
        capability_id="cap_other",
        provider_type=ProviderType.SERVERLESS_LB,
        region="US-CA-2",
        config={
            "gpu_type_priority": ["NVIDIA RTX A4000"],
            "volume_id": "vol_other",
            "constraints": {"cuda_min": "12.1"},
        },
    )

    result = evaluate_hard_constraints(
        request,
        provider,
        capability=_capability(),
    )

    assert result.passed is False
    assert result.reason is EliminationReason.CAPABILITY_MISMATCH
    assert result.reasons == (
        EliminationReason.CAPABILITY_MISMATCH,
        EliminationReason.REGION_MISMATCH,
        EliminationReason.CUDA_MISMATCH,
        EliminationReason.GPU_CLASS_MISMATCH,
        EliminationReason.PAYLOAD_TOO_LARGE,
    )
    assert result.to_dict()["reasons"] == [
        "capability_mismatch",
        "region_mismatch",
        "cuda_mismatch",
        "gpu_class_mismatch",
        "payload_too_large",
    ]


def test_payload_boundary_allows_exactly_30_mb_for_lb() -> None:
    request = RoutingRequest(
        capability_name="embedding.bge-m3",
        payload_bytes=30 * 1024 * 1024,
    )
    provider = _provider("prov_lb", provider_type=ProviderType.SERVERLESS_LB)

    result = evaluate_hard_constraints(
        request,
        provider,
        capability=_capability(),
    )

    assert result.passed is True


def test_gpu_matching_does_not_confuse_l4_with_l40() -> None:
    request = RoutingRequest(
        capability_name="embedding.bge-m3",
        required_gpu_class="NVIDIA L4",
    )
    provider = _provider(
        "prov_l40",
        config={"gpu_type_priority": ["NVIDIA L40"]},
    )

    result = evaluate_hard_constraints(
        request,
        provider,
        capability=_capability(),
    )

    assert result.reasons == (EliminationReason.GPU_CLASS_MISMATCH,)


def test_volume_binding_requires_matching_region_and_volume() -> None:
    request = RoutingRequest(
        capability_name="embedding.bge-m3",
        required_region="US-CA-2",
        required_volume_id="vol_motorsport_corpus_us_ca",
    )
    provider = _provider(
        "prov_wrong_volume",
        region="US-CA-2",
        config={"volume_id": "vol_motorsport_corpus_us_ks"},
    )

    result = evaluate_hard_constraints(
        request,
        provider,
        capability=_capability(),
    )

    assert result.reasons == (EliminationReason.REGION_MISMATCH,)
