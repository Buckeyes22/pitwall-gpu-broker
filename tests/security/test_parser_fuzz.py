"""Security: untrusted-parser fuzzing never crashes unexpectedly."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.core.enums import CostMode
from pitwall.cost.estimator import get_estimator
from pitwall.webhook_receiver.runpod import normalize_runpod_webhook
from tests.conftest import make_llm_capability

pytestmark = [pytest.mark.security, pytest.mark.fuzz]

_json_scalars = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text()
_json_values = st.recursive(
    _json_scalars,
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=8), children, max_size=5)
    ),
    max_leaves=20,
)
_json_objects = st.dictionaries(st.text(max_size=12), _json_values, max_size=8)
_headers = st.dictionaries(st.text(max_size=16), st.text(max_size=32), max_size=6)

_normalizer_extra_keys = st.text(max_size=12).filter(
    lambda key: key not in {"id", "job_id", "jobId", "runpod_job_id", "status", "output", "error"}
)
_normalizer_extras = st.dictionaries(_normalizer_extra_keys, _json_values, max_size=8)

_decimal_like = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-10, max_value=10_000)
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=10)
)
_cost_maps = st.dictionaries(
    st.sampled_from(
        [
            "per_second_active",
            "per_request",
            "per_million_input_tokens",
            "per_million_output_tokens",
            "junk",
        ]
    ),
    _decimal_like,
    max_size=5,
)
_payloads = st.dictionaries(st.text(max_size=10), _decimal_like, max_size=6)


@given(payload=_json_objects, headers=_headers)
def test_normalize_runpod_webhook_never_unexpectedly_raises(
    payload: dict[str, Any],
    headers: dict[str, str],
) -> None:
    try:
        event = normalize_runpod_webhook(payload, headers)
    except ValueError:
        return

    assert event.runpod_job_id and event.runpod_job_id.strip()
    assert isinstance(event.status, str)
    assert 1 <= event.attempt <= 3


@given(
    job_id=st.text(min_size=1, max_size=40).filter(lambda value: bool(value.strip())),
    status=st.text(max_size=20),
    extra=_normalizer_extras,
)
def test_normalize_with_valid_id_is_total(
    job_id: str,
    status: str,
    extra: dict[str, Any],
) -> None:
    payload = {**extra, "id": job_id, "status": status}
    event = normalize_runpod_webhook(payload, {})
    assert event.runpod_job_id == job_id.strip()


@pytest.mark.parametrize(
    "mode",
    [CostMode.PER_SECOND, CostMode.PER_REQUEST, CostMode.PER_TOKEN],
    ids=["per_second", "per_request", "per_token"],
)
@given(provider_cost=_cost_maps, payload=_payloads)
def test_estimator_totality(
    mode: CostMode,
    provider_cost: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    estimator = get_estimator(mode)
    capability = make_llm_capability(cost_mode=mode.value)

    try:
        result = estimator.estimate(capability, provider_cost, payload)
    except ValueError:
        return

    assert isinstance(result, Decimal)
    assert result >= Decimal("0")
    assert result == result.quantize(Decimal("0.000001"))
