"""Stage 1 hard-constraint filtering for provider routing.

The functions in this module are intentionally pure: they inspect the routing
request and in-memory provider records only. Stage 2 and later are responsible
for health, capacity, network probes, and scoring.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, cast

from pitwall.core.enums import ProviderType
from pitwall.core.models import Capability, Provider
from pitwall.routing.types import ConstraintResult, EliminationReason, RoutingRequest

DEFAULT_LB_MAX_PAYLOAD_MB = Decimal("30")
_BYTES_PER_MB = Decimal(1024 * 1024)

_ProviderLike = Provider | Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HardConstraintFilterResult:
    """Complete Stage 1 output for a provider set."""

    passed: tuple[_ProviderLike, ...]
    eliminated: tuple[ConstraintResult, ...]
    results: tuple[ConstraintResult, ...]

    @property
    def eligible(self) -> tuple[_ProviderLike, ...]:
        """Providers that survived Stage 1."""
        return self.passed

    @property
    def failed(self) -> tuple[ConstraintResult, ...]:
        """Providers eliminated during Stage 1."""
        return self.eliminated

    def __iter__(
        self,
    ) -> Iterator[tuple[_ProviderLike, ...] | tuple[ConstraintResult, ...]]:
        yield self.passed
        yield self.eliminated


def filter_hard_constraints(
    request: RoutingRequest,
    providers: Iterable[_ProviderLike],
    *,
    capability: Capability | None = None,
    capability_id: str | None = None,
) -> HardConstraintFilterResult:
    """Evaluate Stage 1 hard constraints across *providers*.

    Returns all surviving providers plus every per-provider evaluation result.
    Providers are returned in their input order; no ranking or network probes
    happen here.
    """

    resolved_capability_id = _capability_id(request, capability, capability_id)
    passed: list[_ProviderLike] = []
    eliminated: list[ConstraintResult] = []
    results: list[ConstraintResult] = []

    for provider in providers:
        result = evaluate_hard_constraints(
            request,
            provider,
            capability=capability,
            capability_id=resolved_capability_id,
        )
        results.append(result)
        if result.passed:
            passed.append(provider)
        else:
            eliminated.append(result)

    return HardConstraintFilterResult(
        passed=tuple(passed),
        eliminated=tuple(eliminated),
        results=tuple(results),
    )


def evaluate_hard_constraints(
    request: RoutingRequest,
    provider: _ProviderLike,
    *,
    capability: Capability | None = None,
    capability_id: str | None = None,
) -> ConstraintResult:
    """Evaluate all Stage 1 hard constraints for one provider."""

    reasons = _hard_constraint_reasons(
        request,
        provider,
        capability=capability,
        capability_id=capability_id,
    )
    provider_id = _provider_id(provider)
    return ConstraintResult(
        provider_id=provider_id,
        passed=not reasons,
        reason=reasons[0] if reasons else None,
        reasons=tuple(reasons),
    )


def hard_constraint_reasons(
    request: RoutingRequest,
    provider: _ProviderLike,
    *,
    capability: Capability | None = None,
    capability_id: str | None = None,
) -> tuple[EliminationReason, ...]:
    """Return every Stage 1 elimination reason for *provider*."""

    return tuple(
        _hard_constraint_reasons(
            request,
            provider,
            capability=capability,
            capability_id=capability_id,
        )
    )


def _hard_constraint_reasons(
    request: RoutingRequest,
    provider: _ProviderLike,
    *,
    capability: Capability | None = None,
    capability_id: str | None = None,
) -> list[EliminationReason]:
    reasons: list[EliminationReason] = []

    if _capability_mismatch(request, provider, capability, capability_id):
        reasons.append(EliminationReason.CAPABILITY_MISMATCH)
    if _region_mismatch(request, provider):
        reasons.append(EliminationReason.REGION_MISMATCH)
    if _cuda_mismatch(request, provider):
        reasons.append(EliminationReason.CUDA_MISMATCH)
    if _gpu_class_mismatch(request, provider):
        reasons.append(EliminationReason.GPU_CLASS_MISMATCH)
    if _payload_too_large(request, provider):
        reasons.append(EliminationReason.PAYLOAD_TOO_LARGE)

    return reasons


def _capability_mismatch(
    request: RoutingRequest,
    provider: _ProviderLike,
    capability: Capability | None,
    capability_id: str | None,
) -> bool:
    if capability is not None and request.capability_name != capability.name:
        return True

    expected_capability_id = _capability_id(request, capability, capability_id)
    provider_capability_id = _string_field(provider, "capability_id")
    if expected_capability_id is not None:
        return provider_capability_id != expected_capability_id

    provider_capability_name = _first_config_string(
        provider,
        "capability_name",
        "capability",
    )
    if provider_capability_name is not None:
        return provider_capability_name != request.capability_name

    if provider_capability_id is not None:
        return provider_capability_id != request.capability_name

    return True


def _provider_id(provider: _ProviderLike) -> str:
    provider_id = _string_field(provider, "id")
    if provider_id is None:
        raise ValueError("provider must include a non-empty id")
    return provider_id


def _capability_id(
    request: RoutingRequest,
    capability: Capability | None,
    capability_id: str | None,
) -> str | None:
    if capability_id is not None:
        return capability_id
    if capability is not None:
        return capability.id
    return request.capability_id


def _region_mismatch(request: RoutingRequest, provider: _ProviderLike) -> bool:
    provider_region = _string_field(provider, "region")

    if request.required_region is not None and provider_region != request.required_region:
        return True

    if request.required_volume_id is None:
        return False

    provider_volume_id = _provider_volume_id(provider)
    return provider_volume_id != request.required_volume_id


def _cuda_mismatch(request: RoutingRequest, provider: _ProviderLike) -> bool:
    required = request.required_cuda_min or request.required_cuda_version
    if required is None:
        return False

    candidates = _provider_cuda_versions(provider)
    if not candidates:
        return True

    return not any(_version_at_least(candidate, required) for candidate in candidates)


def _gpu_class_mismatch(request: RoutingRequest, provider: _ProviderLike) -> bool:
    required = request.required_gpu_class
    if required is None:
        return False

    candidates = _provider_gpu_classes(provider)
    if not candidates:
        return True

    required_token = _normalize_gpu_token(required)
    return not any(
        candidate == required
        or _normalize_gpu_token(candidate) == required_token
        or _gpu_tokens_overlap(candidate, required)
        for candidate in candidates
    )


def _payload_too_large(request: RoutingRequest, provider: _ProviderLike) -> bool:
    if request.payload_bytes is None:
        return False

    limit_mb = _provider_payload_limit_mb(provider)
    if limit_mb is None:
        return False

    return Decimal(request.payload_bytes) > limit_mb * _BYTES_PER_MB


def _provider_payload_limit_mb(provider: _ProviderLike) -> Decimal | None:
    explicit = _config_value(provider, "max_payload_mb")
    if explicit is not None:
        parsed = _decimal_or_none(explicit)
        if parsed is not None:
            return parsed

    provider_type = _provider_type(provider)
    if provider_type == ProviderType.SERVERLESS_LB.value:
        return DEFAULT_LB_MAX_PAYLOAD_MB

    return None


def _provider_volume_id(provider: _ProviderLike) -> str | None:
    return _first_config_string(
        provider,
        "volume_id",
        "networkVolumeId",
        "network_volume_id",
        "required_volume",
    )


def _provider_cuda_versions(provider: _ProviderLike) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("allowed_cuda_versions", "allowedCudaVersions"):
        raw = _config_value(provider, key)
        if isinstance(raw, str):
            values.append(raw)
        elif _is_sequence(raw):
            values.extend(str(item) for item in cast(Sequence[Any], raw) if item is not None)

    for key in ("cuda_min", "cuda_version", "cuda"):
        value = _first_config_string(provider, key)
        if value is not None:
            values.append(value)

    return tuple(dict.fromkeys(values))


def _provider_gpu_classes(provider: _ProviderLike) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("gpu_class", "gpu_type", "gpu_type_id"):
        value = _first_config_string(provider, key)
        if value is not None:
            values.append(value)

    for key in ("gpu_classes", "gpu_types", "gpu_type_priority"):
        raw = _config_value(provider, key)
        if isinstance(raw, str):
            values.append(raw)
        elif _is_sequence(raw):
            values.extend(str(item) for item in cast(Sequence[Any], raw) if item is not None)

    return tuple(dict.fromkeys(values))


def _field(provider: _ProviderLike, key: str) -> object:
    if isinstance(provider, Mapping):
        return provider.get(key)
    return getattr(provider, key)


def _string_field(provider: _ProviderLike, key: str) -> str | None:
    value = _field(provider, key)
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, str) and value:
        return value
    return None


def _provider_type(provider: _ProviderLike) -> str | None:
    return _string_field(provider, "provider_type")


def _config(provider: _ProviderLike) -> Mapping[str, Any]:
    raw = _field(provider, "config")
    if isinstance(raw, Mapping):
        return raw
    return {}


def _config_value(provider: _ProviderLike, key: str) -> object:
    config = _config(provider)
    if key in config:
        return config[key]
    constraints = config.get("constraints")
    if isinstance(constraints, Mapping):
        return constraints.get(key)
    return None


def _first_config_string(provider: _ProviderLike, *keys: str) -> str | None:
    for key in keys:
        value = _config_value(provider, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (bytes, str))


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _version_at_least(candidate: str, required: str) -> bool:
    parsed_candidate = _version_tuple(candidate)
    parsed_required = _version_tuple(required)
    if parsed_candidate is None or parsed_required is None:
        return candidate == required
    return parsed_candidate >= parsed_required


def _version_tuple(value: str) -> tuple[int, ...] | None:
    parts = value.strip().replace("_", ".").split(".")
    parsed: list[int] = []
    for part in parts:
        digits = "".join(char for char in part if char.isdigit())
        if not digits:
            continue
        parsed.append(int(digits))
    return tuple(parsed) if parsed else None


def _normalize_gpu_token(value: str) -> str:
    return "".join(_gpu_pieces(value))


def _gpu_tokens_overlap(candidate: str, required: str) -> bool:
    candidate_token = _normalize_gpu_token(candidate)
    required_token = _normalize_gpu_token(required)
    required_pieces = set(_gpu_pieces(required))
    candidate_pieces = set(_gpu_pieces(candidate))
    pieces_match = bool(required_pieces) and required_pieces.issubset(candidate_pieces)
    suffix_match = len(required_token) >= 4 and candidate_token.endswith(required_token)
    return pieces_match or suffix_match


def _gpu_pieces(value: str) -> tuple[str, ...]:
    ignored = {"NVIDIA", "GEFORCE", "GENERATION"}
    return tuple(
        piece
        for piece in value.replace("_", " ").replace("-", " ").upper().split()
        if piece not in ignored
    )


apply_hard_constraints = filter_hard_constraints
check_hard_constraints = evaluate_hard_constraints
evaluate_hard_constraint = evaluate_hard_constraints
hard_constraint_filter = filter_hard_constraints
stage1_hard_constraint_filter = filter_hard_constraints


__all__ = [
    "DEFAULT_LB_MAX_PAYLOAD_MB",
    "HardConstraintFilterResult",
    "apply_hard_constraints",
    "check_hard_constraints",
    "evaluate_hard_constraint",
    "evaluate_hard_constraints",
    "filter_hard_constraints",
    "hard_constraint_reasons",
    "hard_constraint_filter",
    "stage1_hard_constraint_filter",
]
