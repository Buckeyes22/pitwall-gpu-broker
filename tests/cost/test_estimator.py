from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from pitwall.core import Capability, CostMode
from pitwall.cost.estimator import (
    CostEstimator,
    GpuHourPricing,
    PerRequestEstimator,
    PerRequestPricing,
    PerSecondEstimator,
    PerSecondPricing,
    PerTokenEstimator,
    PerTokenPricing,
    PerVmSecondPricing,
    get_estimator,
    parse_pricing_model,
    quote_cost,
)


def _capability(
    cost_mode: str = "per_second",
    execution_timeout_ms: int = 60_000,
) -> Capability:
    return Capability(
        id="cap_01HQXR8K9N3JZQP7VW4MEX2YBA",
        name="embedding.bge-m3",
        version="1.0.0",
        **{"class": "embedding"},
        cost_mode=cost_mode,
        defaults={"execution_timeout_ms": execution_timeout_ms},
        created_at="2026-05-26T14:00:00Z",
        updated_at="2026-05-26T14:00:00Z",
    )


# ---------------------------------------------------------------------------
# Per-second table
# ---------------------------------------------------------------------------
PER_SECOND_CASES: list[tuple[str, Any, int, dict[str, Any], Decimal]] = [
    (
        "basic_estimate",
        {"per_second_active": "0.000123"},
        60_000,
        {},
        Decimal("0.000123") * Decimal(60),
    ),
    (
        "custom_timeout",
        {"per_second_active": "0.000205"},
        30_000,
        {},
        Decimal("0.000205") * Decimal(30),
    ),
    (
        "zero_rate",
        {"per_second_active": "0"},
        60_000,
        {},
        Decimal("0"),
    ),
    (
        "pod_lease_nested_cost",
        {"provider_type": "pod_lease", "cost": {"per_second_active": "0.000205"}},
        7_200_000,
        {},
        Decimal("1.476000"),
    ),
    (
        "one_millisecond_timeout",
        {"per_second_active": "0.001"},
        1,
        {},
        Decimal("0.000001"),
    ),
]


@pytest.mark.parametrize(
    "label,provider_cost,timeout_ms,payload,expected",
    PER_SECOND_CASES,
    ids=[c[0] for c in PER_SECOND_CASES],
)
def test_per_second_table(
    label: str,
    provider_cost: Any,
    timeout_ms: int,
    payload: dict[str, Any],
    expected: Decimal,
) -> None:
    cap = _capability(execution_timeout_ms=timeout_ms)
    result = PerSecondEstimator().estimate(cap, provider_cost, payload)
    assert result == expected


def test_per_second_object_cost_attribute() -> None:
    cap = _capability(execution_timeout_ms=330_000)
    provider = SimpleNamespace(
        provider_type="serverless_queue",
        cost={"per_second_active": Decimal("0.000123")},
    )
    result = PerSecondEstimator().estimate(cap, provider, {})
    assert result == Decimal("0.040590")


@pytest.mark.parametrize(
    "label,provider_cost,timeout_ms,expected",
    [
        (
            "current_bge_m3_minute",
            {"per_second_active": "0.000123"},
            60_000,
            Decimal("0.007380"),
        ),
        (
            "current_pod_lease_two_hours",
            {"provider_type": "pod_lease", "cost": {"per_second_active": "0.000205"}},
            7_200_000,
            Decimal("1.476000"),
        ),
        (
            "current_micro_timeout",
            {"per_second_active": "0.001"},
            1,
            Decimal("0.000001"),
        ),
    ],
    ids=lambda case: case if isinstance(case, str) else None,
)
def test_gpu_hour_legacy_numbers_are_characterized_exactly(
    label: str,
    provider_cost: Any,
    timeout_ms: int,
    expected: Decimal,
) -> None:
    cap = _capability(execution_timeout_ms=timeout_ms)

    direct = PerSecondEstimator().estimate(cap, provider_cost, {})
    dispatched = get_estimator(CostMode.PER_SECOND).estimate(cap, provider_cost, {})
    quoted = quote_cost(capability=cap, provider_cost=provider_cost, payload={})

    assert direct == expected
    assert dispatched == expected
    assert quoted.estimate() == expected
    assert quoted.upper_bound() == expected


# ---------------------------------------------------------------------------
# Per-second missing / invalid key table
# ---------------------------------------------------------------------------
PER_SECOND_FAILURE_CASES: list[tuple[str, Any, int, str]] = [
    (
        "missing_per_second_active",
        {"per_request": "0.01"},
        60_000,
        "missing required key 'per_second_active'",
    ),
    (
        "negative_rate",
        {"per_second_active": "-0.01"},
        60_000,
        "per_second_active.*non-negative",
    ),
    (
        "boolean_rate",
        {"per_second_active": True},
        60_000,
        "per_second_active.*decimal",
    ),
    (
        "nan_rate",
        {"per_second_active": "NaN"},
        60_000,
        "per_second_active.*finite",
    ),
]


@pytest.mark.parametrize(
    "label,provider_cost,timeout_ms,match",
    PER_SECOND_FAILURE_CASES,
    ids=[c[0] for c in PER_SECOND_FAILURE_CASES],
)
def test_per_second_failures(
    label: str,
    provider_cost: Any,
    timeout_ms: int,
    match: str,
) -> None:
    cap = _capability(execution_timeout_ms=timeout_ms)
    with pytest.raises(ValueError, match=match):
        PerSecondEstimator().estimate(cap, provider_cost, {})


# ---------------------------------------------------------------------------
# Per-request table
# ---------------------------------------------------------------------------
PER_REQUEST_CASES: list[tuple[str, Any, Decimal]] = [
    ("flat_fee", {"per_request": "0.005"}, Decimal("0.005")),
    ("zero_fee", {"per_request": "0.0"}, Decimal("0.0")),
    ("integer_value", {"per_request": 0}, Decimal("0")),
    (
        "public_endpoint_nested_cost",
        {"provider_type": "public_endpoint", "cost": {"per_request": "0.005"}},
        Decimal("0.005000"),
    ),
    (
        "large_fee",
        {"per_request": "12.345"},
        Decimal("12.345"),
    ),
]


@pytest.mark.parametrize(
    "label,provider_cost,expected",
    PER_REQUEST_CASES,
    ids=[c[0] for c in PER_REQUEST_CASES],
)
def test_per_request_table(
    label: str,
    provider_cost: Any,
    expected: Decimal,
) -> None:
    cap = _capability(cost_mode="per_request")
    result = PerRequestEstimator().estimate(cap, provider_cost, {})
    assert result == expected


def test_per_request_object_config_cost_map() -> None:
    cap = _capability(cost_mode="per_request")
    provider = SimpleNamespace(
        provider_type="public_endpoint",
        config={"cost": {"per_request": "0.125"}},
    )
    result = PerRequestEstimator().estimate(cap, provider, {})
    assert result == Decimal("0.125000")


# ---------------------------------------------------------------------------
# Per-request missing / invalid key table
# ---------------------------------------------------------------------------
PER_REQUEST_FAILURE_CASES: list[tuple[str, Any, str]] = [
    (
        "missing_per_request",
        {"per_second_active": "0.001"},
        "missing required key 'per_request'",
    ),
    (
        "negative_rate",
        {"per_request": "-0.01"},
        "per_request.*non-negative",
    ),
    (
        "boolean_rate",
        {"per_request": False},
        "per_request.*decimal",
    ),
]


@pytest.mark.parametrize(
    "label,provider_cost,match",
    PER_REQUEST_FAILURE_CASES,
    ids=[c[0] for c in PER_REQUEST_FAILURE_CASES],
)
def test_per_request_failures(
    label: str,
    provider_cost: Any,
    match: str,
) -> None:
    cap = _capability(cost_mode="per_request")
    with pytest.raises(ValueError, match=match):
        PerRequestEstimator().estimate(cap, provider_cost, {})


# ---------------------------------------------------------------------------
# Per-token table
# ---------------------------------------------------------------------------
PER_TOKEN_CASES: list[tuple[str, Any, dict[str, Any], Decimal]] = [
    (
        "explicit_token_counts",
        {
            "per_million_input_tokens": "0.30",
            "per_million_output_tokens": "0.60",
        },
        {"input_tokens": 1000, "output_tokens": 500},
        (Decimal("0.30") * 1000 + Decimal("0.60") * 500) / Decimal(1_000_000),
    ),
    (
        "heuristic_from_prompt_string",
        {
            "per_million_input_tokens": "1.0",
            "per_million_output_tokens": "2.0",
        },
        {"prompt": "a" * 400, "max_tokens": 100},
        (Decimal("1.0") * Decimal(100) + Decimal("2.0") * Decimal(100)) / Decimal(1_000_000),
    ),
    (
        "heuristic_from_input_list",
        {
            "per_million_input_tokens": "0.50",
            "per_million_output_tokens": "1.00",
        },
        {"input": ["hello world", "test"]},
        Decimal("0.000258"),
    ),
    (
        "prompt_and_completion_token_aliases",
        {
            "per_million_input_tokens": "0.30",
            "per_million_output_tokens": "0.60",
        },
        {"prompt_tokens": 2_000, "completion_tokens": 1_000},
        Decimal("0.001200"),
    ),
    (
        "openai_usage_token_counts",
        {
            "per_million_input_tokens": "0.30",
            "per_million_output_tokens": "0.60",
        },
        {"usage": {"prompt_tokens": 2_000, "completion_tokens": 1_000}},
        Decimal("0.001200"),
    ),
    (
        "input_bytes_heuristic",
        {
            "per_million_input_tokens": "0.50",
            "per_million_output_tokens": "1.00",
        },
        {"input_bytes": 400, "max_tokens": 25},
        Decimal("0.000075"),
    ),
    (
        "spec_example_pricing",
        {
            "per_million_input_tokens": "0.30",
            "per_million_output_tokens": "0.60",
        },
        {"input_tokens": 100_000, "output_tokens": 20_000},
        (Decimal("0.30") * 100_000 + Decimal("0.60") * 20_000) / Decimal(1_000_000),
    ),
    (
        "default_output_tokens_when_missing",
        {
            "per_million_input_tokens": "1.0",
            "per_million_output_tokens": "1.0",
        },
        {"input_tokens": 100},
        (Decimal("1.0") * Decimal(100) + Decimal("1.0") * Decimal(256)) / Decimal(1_000_000),
    ),
]


@pytest.mark.parametrize(
    "label,provider_cost,payload,expected",
    PER_TOKEN_CASES,
    ids=[c[0] for c in PER_TOKEN_CASES],
)
def test_per_token_table(
    label: str,
    provider_cost: Any,
    payload: dict[str, Any],
    expected: Decimal,
) -> None:
    cap = _capability(cost_mode="per_token")
    result = PerTokenEstimator().estimate(cap, provider_cost, payload)
    assert result == expected


def test_per_token_openai_messages_heuristic() -> None:
    cap = _capability(cost_mode="per_token")
    provider_cost = {
        "per_million_input_tokens": "1.0",
        "per_million_output_tokens": "2.0",
    }
    payload = {
        "messages": [
            {"role": "system", "content": "abcd"},
            {"role": "user", "content": [{"type": "text", "text": "abcdefgh"}]},
        ],
        "max_output_tokens": 16,
    }
    result = PerTokenEstimator().estimate(cap, provider_cost, payload)
    expected = (Decimal("1.0") * Decimal(3) + Decimal("2.0") * Decimal(16)) / Decimal(1_000_000)
    assert result == expected


def test_per_token_nested_cost_map() -> None:
    cap = _capability(cost_mode="per_token")
    provider = {
        "provider_type": "public_endpoint",
        "cost": {
            "per_million_input_tokens": "0.30",
            "per_million_output_tokens": "0.60",
        },
    }
    payload = {"input_tokens": 100_000, "output_tokens": 20_000}
    result = PerTokenEstimator().estimate(cap, provider, payload)
    assert result == Decimal("0.042000")


def test_per_token_object_config_cost_map() -> None:
    cap = _capability(cost_mode="per_token")
    provider = SimpleNamespace(
        provider_type="public_endpoint",
        config={
            "cost": {
                "per_million_input_tokens": Decimal("0.20"),
                "per_million_output_tokens": Decimal("0.80"),
            }
        },
    )
    payload = {"input_tokens": 10_000, "output_tokens": 5_000}
    result = PerTokenEstimator().estimate(cap, provider, payload)
    assert result == Decimal("0.006000")


# ---------------------------------------------------------------------------
# Tagged pricing model variants
# ---------------------------------------------------------------------------
def test_gpu_hour_pricing_variant_matches_current_per_second_math() -> None:
    cap = _capability(execution_timeout_ms=7_200_000)
    pricing = GpuHourPricing(kind="gpu_hour", per_second_active=Decimal("0.000205"))

    assert pricing.estimate(cap, {}) == Decimal("1.476000")
    assert pricing.upper_bound(cap, {}) == Decimal("1.476000")


def test_per_second_pricing_variant_uses_bid_rate_for_upper_bound() -> None:
    cap = _capability(execution_timeout_ms=2_500)
    pricing = PerSecondPricing(
        kind="per_second",
        rate_per_second=Decimal("0.010"),
        bid_rate_per_second=Decimal("0.025"),
    )

    assert pricing.estimate(cap, {}) == Decimal("0.025000")
    assert pricing.upper_bound(cap, {}) == Decimal("0.062500")


def test_per_second_pricing_rejects_negative_bid_rate_with_field_name() -> None:
    with pytest.raises(ValueError) as exc_info:
        PerSecondPricing(
            kind="per_second",
            rate_per_second=Decimal("0.010"),
            bid_rate_per_second=Decimal("-0.001"),
        )

    assert "bid_rate_per_second must be non-negative" in str(exc_info.value)


def test_per_token_pricing_variant_uses_max_tokens_for_upper_bound() -> None:
    cap = _capability(cost_mode="per_token")
    pricing = PerTokenPricing(
        kind="per_token",
        per_million_input_tokens=Decimal("0.30"),
        per_million_output_tokens=Decimal("0.90"),
    )
    payload = {"input_tokens": 1_000, "output_tokens": 250, "max_tokens": 1_000}

    assert pricing.estimate(cap, payload) == Decimal("0.000525")
    assert pricing.upper_bound(cap, payload) == Decimal("0.001200")


@pytest.mark.parametrize(
    "max_token_key",
    ["max_output_tokens", "max_completion_tokens", "max_new_tokens"],
)
def test_per_token_pricing_upper_bound_accepts_each_output_cap_alias(max_token_key: str) -> None:
    cap = _capability(cost_mode="per_token")
    pricing = PerTokenPricing(
        kind="per_token",
        per_million_input_tokens=Decimal("0.30"),
        per_million_output_tokens=Decimal("0.90"),
    )
    payload = {"input_tokens": 100, "output_tokens": 1, max_token_key: 900}

    assert pricing.upper_bound(cap, payload) == Decimal("0.000840")


@pytest.mark.parametrize(
    "payload",
    [
        {"input_tokens": 1_000, "output_tokens": 250},
        {"prompt_tokens": 1_000, "completion_tokens": 250},
        {"usage": {"prompt_tokens": 1_000, "completion_tokens": 250}},
    ],
    ids=["output_tokens", "completion_tokens", "usage_completion_tokens"],
)
def test_per_token_upper_bound_rejects_open_ended_output_without_max_cap(
    payload: dict[str, Any],
) -> None:
    cap = _capability(cost_mode="per_token")
    pricing = PerTokenPricing(
        kind="per_token",
        per_million_input_tokens=Decimal("0.30"),
        per_million_output_tokens=Decimal("0.90"),
    )

    with pytest.raises(ValueError, match="max_output_tokens.*required"):
        pricing.upper_bound(cap, payload)


def test_per_token_upper_bound_rejects_missing_output_cap_even_when_estimate_defaults() -> None:
    cap = _capability(cost_mode="per_token")
    pricing = PerTokenPricing(
        kind="per_token",
        per_million_input_tokens=Decimal("0.30"),
        per_million_output_tokens=Decimal("0.90"),
    )

    with pytest.raises(ValueError, match="max_output_tokens.*required"):
        pricing.upper_bound(cap, {"input_tokens": 1_000})


def test_per_token_upper_bound_missing_output_cap_reports_exact_contract() -> None:
    cap = _capability(cost_mode="per_token")
    pricing = PerTokenPricing(
        kind="per_token",
        per_million_input_tokens=Decimal("0.30"),
        per_million_output_tokens=Decimal("0.90"),
    )

    with pytest.raises(ValueError) as exc_info:
        pricing.upper_bound(cap, {"input_tokens": 1_000})

    assert str(exc_info.value) == "max_output_tokens is required for per-token upper_bound"


def test_per_token_upper_bound_negative_output_cap_reports_canonical_field_name() -> None:
    cap = _capability(cost_mode="per_token")
    pricing = PerTokenPricing(
        kind="per_token",
        per_million_input_tokens=Decimal("0.30"),
        per_million_output_tokens=Decimal("0.90"),
    )

    with pytest.raises(ValueError) as exc_info:
        pricing.upper_bound(cap, {"input_tokens": 1_000, "max_output_tokens": -1})

    assert str(exc_info.value) == "max_output_tokens must be non-negative"


def test_per_vm_second_pricing_variant_uses_flat_vm_second_rate() -> None:
    cap = _capability(execution_timeout_ms=1_500)
    pricing = PerVmSecondPricing(kind="per_vm_second", rate_per_second=Decimal("0.003"))

    assert pricing.estimate(cap, {}) == Decimal("0.004500")
    assert pricing.upper_bound(cap, {}) == Decimal("0.004500")


def test_per_request_pricing_variant_preserves_flat_fee_behavior() -> None:
    cap = _capability(cost_mode="per_request")
    pricing = PerRequestPricing(kind="per_request", per_request=Decimal("0.125"))

    assert pricing.estimate(cap, {}) == Decimal("0.125000")
    assert pricing.upper_bound(cap, {}) == Decimal("0.125000")


@pytest.mark.parametrize(
    ("provider_cost", "expected_type", "expected"),
    [
        (
            {"kind": "per_second", "rate_per_second": "0.010"},
            PerSecondPricing,
            Decimal("0.600000"),
        ),
        (
            {"model": "per_vm_second", "rate_per_second": "0.003"},
            PerVmSecondPricing,
            Decimal("0.180000"),
        ),
    ],
    ids=["kind_without_cost_mode", "model_alias_without_cost_mode"],
)
def test_parse_pricing_model_uses_tagged_discriminator_without_legacy_cost_mode(
    provider_cost: dict[str, str],
    expected_type: type,
    expected: Decimal,
) -> None:
    cap = _capability(execution_timeout_ms=60_000)

    pricing = parse_pricing_model(provider_cost)

    assert isinstance(pricing, expected_type)
    assert pricing.estimate(cap, {}) == expected


def test_parse_pricing_model_removes_model_alias_and_prefers_explicit_kind() -> None:
    pricing = parse_pricing_model(
        {
            "kind": "per_request",
            "model": "per_second",
            "per_request": "0.125",
        }
    )
    cap = _capability(cost_mode="per_request")

    assert isinstance(pricing, PerRequestPricing)
    assert pricing.estimate(cap, {}) == Decimal("0.125000")


def test_parse_pricing_model_accepts_discriminated_tagged_dict() -> None:
    pricing = parse_pricing_model(
        {
            "kind": "per_token",
            "per_million_input_tokens": "0.30",
            "per_million_output_tokens": "0.60",
        },
        cost_mode=CostMode.PER_TOKEN,
    )
    cap = _capability(cost_mode="per_token")

    assert isinstance(pricing, PerTokenPricing)
    assert pricing.upper_bound(cap, {"input_tokens": 100, "max_tokens": 900}) == Decimal("0.000570")


def test_quote_cost_exposes_uniform_estimate_and_upper_bound_interface() -> None:
    cap = _capability(cost_mode="per_token")
    quote = quote_cost(
        capability=cap,
        provider_cost={
            "kind": "per_token",
            "per_million_input_tokens": "1.00",
            "per_million_output_tokens": "2.00",
        },
        payload={"input_tokens": 100, "output_tokens": 10, "max_tokens": 1_000},
    )

    assert quote.estimate() == Decimal("0.000120")
    assert quote.upper_bound() == Decimal("0.002100")


@pytest.mark.parametrize(
    "estimator",
    [PerSecondEstimator(), PerRequestEstimator(), PerTokenEstimator()],
)
def test_estimator_estimate_preserves_capability_for_tagged_per_second(
    estimator: CostEstimator,
) -> None:
    cap = _capability(execution_timeout_ms=2_500)

    result = estimator.estimate(
        cap,
        {"kind": "per_second", "rate_per_second": "0.010"},
        {},
    )

    assert result == Decimal("0.025000")


@pytest.mark.parametrize(
    "estimator",
    [PerSecondEstimator(), PerRequestEstimator(), PerTokenEstimator()],
)
def test_estimator_estimate_preserves_payload_for_tagged_per_token(
    estimator: CostEstimator,
) -> None:
    cap = _capability(cost_mode="per_token")

    result = estimator.estimate(
        cap,
        {
            "kind": "per_token",
            "per_million_input_tokens": "1.00",
            "per_million_output_tokens": "2.00",
        },
        {"input_tokens": 100, "output_tokens": 20},
    )

    assert result == Decimal("0.000140")


@pytest.mark.parametrize(
    "estimator",
    [PerSecondEstimator(), PerRequestEstimator(), PerTokenEstimator()],
)
def test_estimator_upper_bound_preserves_capability_for_tagged_per_second(
    estimator: CostEstimator,
) -> None:
    cap = _capability(execution_timeout_ms=2_500)

    result = estimator.upper_bound(
        cap,
        {
            "kind": "per_second",
            "rate_per_second": "0.010",
            "bid_rate_per_second": "0.040",
        },
        {},
    )

    assert result == Decimal("0.100000")


@pytest.mark.parametrize(
    "estimator",
    [PerSecondEstimator(), PerRequestEstimator(), PerTokenEstimator()],
)
def test_estimator_upper_bound_preserves_payload_for_tagged_per_token(
    estimator: CostEstimator,
) -> None:
    cap = _capability(cost_mode="per_token")

    result = estimator.upper_bound(
        cap,
        {
            "kind": "per_token",
            "per_million_input_tokens": "1.00",
            "per_million_output_tokens": "2.00",
        },
        {"input_tokens": 100, "output_tokens": 20, "max_output_tokens": 50},
    )

    assert result == Decimal("0.000200")


def test_mapping_config_cost_map_is_used_for_flat_dict_provider() -> None:
    cap = _capability(cost_mode="per_request")

    result = PerRequestEstimator().estimate(
        cap,
        {"config": {"cost": {"per_request": "0.250"}}},
        {},
    )

    assert result == Decimal("0.250000")


def test_invalid_provider_object_reports_cost_mapping_contract() -> None:
    cap = _capability(cost_mode="per_request")

    with pytest.raises(ValueError) as exc_info:
        PerRequestEstimator().estimate(cap, SimpleNamespace(provider_type="missing_cost"), {})

    assert str(exc_info.value) == "provider cost must be a mapping or expose a mapping 'cost'"


def test_legacy_cost_without_cost_mode_reports_required_mode_or_tag() -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_pricing_model({"per_request": "0.005"})

    assert str(exc_info.value) == "legacy provider cost requires cost_mode or tagged pricing kind"


@pytest.mark.parametrize(
    "max_token_key",
    ["max_completion_tokens", "max_new_tokens"],
)
def test_per_token_estimate_accepts_output_cap_aliases_when_output_count_is_absent(
    max_token_key: str,
) -> None:
    cap = _capability(cost_mode="per_token")

    result = PerTokenEstimator().estimate(
        cap,
        {
            "per_million_input_tokens": "0",
            "per_million_output_tokens": "1.00",
        },
        {"input_tokens": 0, max_token_key: 42},
    )

    assert result == Decimal("0.000042")


def test_per_token_estimate_negative_output_cap_reports_canonical_field_name() -> None:
    cap = _capability(cost_mode="per_token")

    with pytest.raises(ValueError) as exc_info:
        PerTokenEstimator().estimate(
            cap,
            {
                "per_million_input_tokens": "0",
                "per_million_output_tokens": "1.00",
            },
            {"input_tokens": 0, "max_completion_tokens": -1},
        )

    assert str(exc_info.value) == "max_output_tokens must be non-negative"


def test_per_token_estimate_negative_input_token_reports_exact_field_name() -> None:
    cap = _capability(cost_mode="per_token")

    with pytest.raises(ValueError) as exc_info:
        PerTokenEstimator().estimate(
            cap,
            {
                "per_million_input_tokens": "1.00",
                "per_million_output_tokens": "0",
            },
            {"input_tokens": -1, "output_tokens": 0},
        )

    assert str(exc_info.value) == "input_tokens must be non-negative"


def test_per_token_estimate_negative_output_token_reports_exact_field_name() -> None:
    cap = _capability(cost_mode="per_token")

    with pytest.raises(ValueError) as exc_info:
        PerTokenEstimator().estimate(
            cap,
            {
                "per_million_input_tokens": "1.00",
                "per_million_output_tokens": "0",
            },
            {"input_tokens": 0, "output_tokens": -1},
        )

    assert str(exc_info.value) == "output_tokens must be non-negative"


def test_per_token_estimate_negative_input_bytes_reports_exact_field_name() -> None:
    cap = _capability(cost_mode="per_token")

    with pytest.raises(ValueError) as exc_info:
        PerTokenEstimator().estimate(
            cap,
            {
                "per_million_input_tokens": "1.00",
                "per_million_output_tokens": "0",
            },
            {"input_bytes": -4, "max_tokens": 0},
        )

    assert str(exc_info.value) == "input_bytes must be non-negative"


def test_per_request_invalid_decimal_reports_exact_field_name() -> None:
    cap = _capability(cost_mode="per_request")

    with pytest.raises(ValueError) as exc_info:
        PerRequestEstimator().estimate(cap, {"per_request": "not-a-decimal"}, {})

    assert str(exc_info.value) == "provider cost 'per_request' must be a decimal value"


def test_per_token_heuristic_includes_top_level_system_text() -> None:
    cap = _capability(cost_mode="per_token")

    result = PerTokenEstimator().estimate(
        cap,
        {
            "per_million_input_tokens": "1.00",
            "per_million_output_tokens": "0",
        },
        {"system": "abcdefgh", "max_tokens": 0},
    )

    assert result == Decimal("0.000002")


def test_per_token_heuristic_counts_nested_prompt_and_input_text_only() -> None:
    cap = _capability(cost_mode="per_token")

    result = PerTokenEstimator().estimate(
        cap,
        {
            "per_million_input_tokens": "4.00",
            "per_million_output_tokens": "0",
        },
        {
            "input": [
                {"prompt": "abcd"},
                {"input": "abcdefgh"},
                {"content": 123},
            ],
            "max_tokens": 0,
        },
    )

    assert result == Decimal("0.000012")


# ---------------------------------------------------------------------------
# Per-token missing / invalid key table
# ---------------------------------------------------------------------------
PER_TOKEN_FAILURE_CASES: list[tuple[str, Any, dict[str, Any], str]] = [
    (
        "missing_output_token_rate",
        {"per_million_input_tokens": "0.30"},
        {"input_tokens": 1000, "output_tokens": 500},
        "missing required key 'per_million_output_tokens'",
    ),
    (
        "missing_input_token_rate",
        {"per_million_output_tokens": "0.60"},
        {"input_tokens": 1000, "output_tokens": 500},
        "missing required key 'per_million_input_tokens'",
    ),
    (
        "negative_input_rate",
        {
            "per_million_input_tokens": "-0.30",
            "per_million_output_tokens": "0.60",
        },
        {"input_tokens": 1000, "output_tokens": 500},
        "per_million_input_tokens.*non-negative",
    ),
    (
        "negative_output_rate",
        {
            "per_million_input_tokens": "0.30",
            "per_million_output_tokens": "-0.60",
        },
        {"input_tokens": 1000, "output_tokens": 500},
        "per_million_output_tokens.*non-negative",
    ),
    (
        "negative_input_token_count",
        {
            "per_million_input_tokens": "0.30",
            "per_million_output_tokens": "0.60",
        },
        {"input_tokens": -1, "output_tokens": 500},
        "input_tokens.*non-negative",
    ),
    (
        "negative_output_token_count",
        {
            "per_million_input_tokens": "0.30",
            "per_million_output_tokens": "0.60",
        },
        {"input_tokens": 100, "output_tokens": -5},
        "output_tokens.*non-negative",
    ),
    (
        "both_rates_missing",
        {},
        {"input_tokens": 1000, "output_tokens": 500},
        "missing required key",
    ),
]


@pytest.mark.parametrize(
    "label,provider_cost,payload,match",
    PER_TOKEN_FAILURE_CASES,
    ids=[c[0] for c in PER_TOKEN_FAILURE_CASES],
)
def test_per_token_failures(
    label: str,
    provider_cost: Any,
    payload: dict[str, Any],
    match: str,
) -> None:
    cap = _capability(cost_mode="per_token")
    with pytest.raises(ValueError, match=match):
        PerTokenEstimator().estimate(cap, provider_cost, payload)


# ---------------------------------------------------------------------------
# get_estimator dispatch table
# ---------------------------------------------------------------------------
GET_ESTIMATOR_CASES: list[tuple[str, CostMode, type]] = [
    ("per_second", CostMode.PER_SECOND, PerSecondEstimator),
    ("per_request", CostMode.PER_REQUEST, PerRequestEstimator),
    ("per_token", CostMode.PER_TOKEN, PerTokenEstimator),
]


@pytest.mark.parametrize(
    "label,mode,expected_type",
    GET_ESTIMATOR_CASES,
    ids=[c[0] for c in GET_ESTIMATOR_CASES],
)
def test_get_estimator_table(
    label: str,
    mode: CostMode,
    expected_type: type,
) -> None:
    est = get_estimator(mode)
    assert isinstance(est, expected_type)
    assert isinstance(est, CostEstimator)


def test_get_estimator_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unsupported cost_mode"):
        get_estimator("unknown")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------
PROTOCOL_CASES: list[tuple[str, type]] = [
    ("per_second", PerSecondEstimator),
    ("per_request", PerRequestEstimator),
    ("per_token", PerTokenEstimator),
]


@pytest.mark.parametrize(
    "label,estimator_cls",
    PROTOCOL_CASES,
    ids=[c[0] for c in PROTOCOL_CASES],
)
def test_protocol_conformance(label: str, estimator_cls: type) -> None:
    assert isinstance(estimator_cls(), CostEstimator)
