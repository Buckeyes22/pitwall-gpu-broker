"""Retry-After parsing helpers for RunPod rate-limit responses."""

from __future__ import annotations

import datetime as dt
import math
from email.utils import parsedate_to_datetime

DEFAULT_MAX_RETRY_AFTER_DELAY_S = 60.0


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _normalize_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def parse_retry_after(
    value: str | None,
    *,
    now: dt.datetime | None = None,
    max_delay_s: float = DEFAULT_MAX_RETRY_AFTER_DELAY_S,
) -> float | None:
    """Parse a Retry-After header into bounded seconds.

    HTTP allows either delay-seconds or an HTTP-date. Invalid or empty values
    return ``None`` so callers can fall back to their normal retry schedule.
    """

    if max_delay_s < 0:
        raise ValueError("max_delay_s must be >= 0")
    if value is None:
        return None

    raw_value = value.strip()
    if not raw_value:
        return None

    try:
        delay_s = float(raw_value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        reference_time = _normalize_utc(now or _utc_now())
        delay_s = (_normalize_utc(retry_at) - reference_time).total_seconds()

    if not math.isfinite(delay_s):
        return None

    bounded_delay = max(0.0, delay_s)
    return min(bounded_delay, max_delay_s)


__all__ = [
    "DEFAULT_MAX_RETRY_AFTER_DELAY_S",
    "parse_retry_after",
]
