"""Shared error rendering for operator-console screens."""

from __future__ import annotations

_MAX_REASON_LEN = 200


def source_failure_message(prefix: str, exc: BaseException) -> str:
    """One-line failure text that keeps the underlying reason visible.

    A bare "state source failed" forces the operator to re-run the data
    source by hand to learn what broke (missing env var, removed API field,
    unreachable service). Keep the reason inline, truncated to one line.
    """
    reason = " ".join(str(exc).split()) or type(exc).__name__
    if len(reason) > _MAX_REASON_LEN:
        reason = reason[: _MAX_REASON_LEN - 3] + "..."
    return f"{prefix}: {reason}"


__all__ = ["source_failure_message"]
