"""Property tests for demand-forecast prewarm planning."""

from __future__ import annotations

import datetime as dt

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.routing import DemandSample, PrewarmPolicy, forecast_demand

pytestmark = pytest.mark.property

_NOW = dt.datetime(2026, 6, 2, 12, 0, 0, tzinfo=dt.UTC)


def _history_from_window_counts(counts: list[int]) -> list[DemandSample]:
    oldest_to_newest_offsets = [14, 9, 4]
    return [
        DemandSample(
            capability_id="cap_embed",
            observed_at=_NOW - dt.timedelta(minutes=offset),
            request_count=count,
        )
        for offset, count in zip(oldest_to_newest_offsets, counts, strict=True)
    ]


@given(
    counts=st.lists(st.integers(min_value=0, max_value=10_000), min_size=3, max_size=3),
    increment=st.integers(min_value=1, max_value=10_000),
)
def test_forecast_does_not_decrease_when_most_recent_window_increases(
    counts: list[int],
    increment: int,
) -> None:
    policy = PrewarmPolicy(
        lookback=dt.timedelta(minutes=15),
        sample_window=dt.timedelta(minutes=5),
        forecast_window=dt.timedelta(minutes=5),
        headroom=1.0,
    )
    higher_counts = [*counts[:-1], counts[-1] + increment]

    base = forecast_demand(_history_from_window_counts(counts), now=_NOW, policy=policy)[0]
    higher = forecast_demand(_history_from_window_counts(higher_counts), now=_NOW, policy=policy)[0]

    assert higher.projected_requests >= base.projected_requests
