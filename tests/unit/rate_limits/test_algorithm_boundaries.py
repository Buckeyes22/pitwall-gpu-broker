from __future__ import annotations

import math

from pitwall.rate_limits.algorithm import (
    TokenBucket,
    refill_tokens,
    seconds_until_available,
)


def test_refill_tokens_uses_elapsed_rate_and_caps_at_capacity() -> None:
    assert (
        refill_tokens(
            tokens=1.5,
            capacity=10,
            elapsed_s=2.5,
            refill_window_s=10.0,
        )
        == 4.0
    )
    assert (
        refill_tokens(
            tokens=9.0,
            capacity=10,
            elapsed_s=2.0,
            refill_window_s=10.0,
        )
        == 10.0
    )


def test_token_bucket_allows_exact_available_after_refill() -> None:
    bucket = TokenBucket(
        capacity=10,
        tokens=4.0,
        last_refilled_at_s=100.0,
        refill_window_s=10.0,
    )

    assert bucket.try_consume(7.0, now_s=103.0) is True
    assert bucket.tokens == 0.0
    assert bucket.last_refilled_at_s == 103.0


def test_token_bucket_denies_one_over_available_after_refill() -> None:
    bucket = TokenBucket(
        capacity=10,
        tokens=4.0,
        last_refilled_at_s=100.0,
        refill_window_s=10.0,
    )

    assert bucket.try_consume(7.000001, now_s=103.0) is False
    assert bucket.tokens == 7.0
    assert bucket.last_refilled_at_s == 103.0


def test_seconds_until_available_handles_exact_shortfall_and_impossible_request() -> None:
    assert (
        seconds_until_available(
            tokens=5.0,
            capacity=10,
            tokens_needed=5.0,
            refill_window_s=10.0,
        )
        == 0.0
    )
    assert (
        seconds_until_available(
            tokens=4.0,
            capacity=10,
            tokens_needed=5.0,
            refill_window_s=10.0,
        )
        == 1.0
    )
    assert math.isinf(
        seconds_until_available(
            tokens=0.0,
            capacity=10,
            tokens_needed=11.0,
            refill_window_s=10.0,
        )
    )
