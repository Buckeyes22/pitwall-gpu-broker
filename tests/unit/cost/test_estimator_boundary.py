from __future__ import annotations

from decimal import Decimal

import pytest

from pitwall.core.models import Capability
from pitwall.cost.estimator import get_estimator

_CREATED_AT = "2026-05-26T14:00:00Z"


def _capability(
    *,
    cost_mode: str,
    execution_timeout_ms: int = 1_000,
) -> Capability:
    return Capability(
        id=f"cap_estimator_boundary_{cost_mode}",
        name=f"embedding.boundary.{cost_mode}",
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode=cost_mode,
        defaults={"execution_timeout_ms": execution_timeout_ms},
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
    )


def _estimate(
    mode: str,
    provider_cost: object,
    payload: dict[str, object],
    *,
    execution_timeout_ms: int = 1_000,
) -> Decimal:
    capability = _capability(cost_mode=mode, execution_timeout_ms=execution_timeout_ms)
    return get_estimator(mode).estimate(capability, provider_cost, payload)


@pytest.mark.parametrize(
    ("mode", "provider_cost", "payload"),
    [
        ("per_request", {"per_request": Decimal("-0.000001")}, {}),
        ("per_second", {"per_second_active": Decimal("-0.000001")}, {}),
        (
            "per_token",
            {
                "per_million_input_tokens": Decimal("-0.01"),
                "per_million_output_tokens": Decimal("0"),
            },
            {"input_tokens": 1, "output_tokens": 0},
        ),
    ],
)
def test_negative_cost_inputs_raise_value_error(
    mode: str,
    provider_cost: object,
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        _estimate(mode, provider_cost, payload)


@pytest.mark.parametrize(
    ("mode", "provider_cost", "payload"),
    [
        ("per_request", {"per_request": Decimal("0")}, {}),
        ("per_second", {"per_second_active": Decimal("0")}, {}),
        (
            "per_token",
            {
                "per_million_input_tokens": Decimal("0"),
                "per_million_output_tokens": Decimal("0"),
            },
            {"input_tokens": 1, "output_tokens": 1},
        ),
    ],
)
def test_zero_cost_inputs_are_valid(
    mode: str,
    provider_cost: object,
    payload: dict[str, object],
) -> None:
    assert _estimate(mode, provider_cost, payload) == Decimal("0.000000")


@pytest.mark.parametrize(
    ("mode", "provider_cost", "payload"),
    [
        ("per_request", {"per_request": Decimal("1e30")}, {}),
        ("per_second", {"per_second_active": Decimal("1e30")}, {}),
        (
            "per_token",
            {
                "per_million_input_tokens": Decimal("1e36"),
                "per_million_output_tokens": Decimal("0"),
            },
            {"input_tokens": 1, "output_tokens": 0},
        ),
    ],
)
def test_huge_cost_inputs_raise_value_error(
    mode: str,
    provider_cost: object,
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="out of representable USD range"):
        _estimate(mode, provider_cost, payload)
