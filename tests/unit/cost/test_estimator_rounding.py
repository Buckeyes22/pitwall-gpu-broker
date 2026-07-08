from __future__ import annotations

from decimal import Decimal
from typing import Any

from pitwall.core.models import Capability
from pitwall.cost.estimator import get_estimator

_CREATED_AT = "2026-05-26T14:00:00Z"


def _capability(
    *,
    cost_mode: str,
    execution_timeout_ms: int = 60_000,
) -> Capability:
    return Capability(
        id="cap_estimator_rounding",
        name="embedding.rounding",
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode=cost_mode,
        defaults={"execution_timeout_ms": execution_timeout_ms},
        created_at=_CREATED_AT,
        updated_at=_CREATED_AT,
    )


def test_per_request_rounds_half_up_to_six_decimal_places() -> None:
    estimator = get_estimator("per_request")
    capability = _capability(cost_mode="per_request")

    assert estimator.estimate(
        capability,
        {"per_request": Decimal("0.0000005")},
        {},
    ) == Decimal("0.000001")
    assert estimator.estimate(
        capability,
        {"per_request": Decimal("0.0000025")},
        {},
    ) == Decimal("0.000003")
    assert estimator.estimate(
        capability,
        {"per_request": Decimal("0.00000149")},
        {},
    ) == Decimal("0.000001")


def test_per_second_rounds_computed_cost_ties_half_up() -> None:
    estimator = get_estimator("per_second")
    capability = _capability(cost_mode="per_second", execution_timeout_ms=1)

    assert estimator.estimate(
        capability,
        {"per_second_active": Decimal("0.0005")},
        {},
    ) == Decimal("0.000001")
    assert estimator.estimate(
        capability,
        {"per_second_active": Decimal("0.0025")},
        {},
    ) == Decimal("0.000003")


def test_per_token_rounds_computed_cost_ties_half_up() -> None:
    estimator = get_estimator("per_token")
    capability = _capability(cost_mode="per_token")
    payload: dict[str, Any] = {"input_tokens": 1, "output_tokens": 0}

    assert estimator.estimate(
        capability,
        {
            "per_million_input_tokens": Decimal("0.50"),
            "per_million_output_tokens": Decimal("0"),
        },
        payload,
    ) == Decimal("0.000001")
    assert estimator.estimate(
        capability,
        {
            "per_million_input_tokens": Decimal("2.50"),
            "per_million_output_tokens": Decimal("0"),
        },
        payload,
    ) == Decimal("0.000003")
