"""Parse actual token usage from OpenAI-compatible response payloads.

Supports two transport shapes:

* **Non-streaming JSON** — a single JSON object whose top-level ``usage``
  dict contains ``prompt_tokens``, ``completion_tokens``, and optionally
  ``total_tokens``.
* **SSE byte stream** — a sequence of ``data: {…}\\n\\n`` frames, where
  the last JSON frame before ``data: [DONE]`` may carry a ``usage`` dict
  (OpenAI `stream_options.include_usage`).

Both functions return a :class:`TokenUsage` dataclass or ``None`` when no
usage information is found.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    """Actual token counts captured from a provider response."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


def parse_usage_json(body: dict[str, Any]) -> TokenUsage | None:
    """Extract token counts from a non-streaming JSON response body.

    Accepts the full OpenAI chat-completion response dict and reads the
    ``usage`` field.  Returns ``None`` when ``usage`` is absent or empty.
    """
    usage = body.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None

    prompt_valid, prompt = _safe_int(usage.get("prompt_tokens"))
    completion_valid, completion = _safe_int(usage.get("completion_tokens"))
    total_valid, total = _safe_int(usage.get("total_tokens"))

    if not prompt_valid or not completion_valid or not total_valid:
        return None

    if prompt is None and completion is None:
        return None

    if prompt is None:
        prompt = 0
    if completion is None:
        completion = 0
    if total is None:
        total = prompt + completion

    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )


def parse_usage_sse(raw: bytes | str) -> TokenUsage | None:
    """Extract token counts from an SSE byte stream.

    Scans all ``data: {…}`` frames for a ``usage`` dict.  The *last*
    frame that contains usage wins (matching OpenAI's
    ``stream_options.include_usage`` contract where usage arrives in a
    trailing empty-delta chunk).

    Returns ``None`` when no frame carries usage information.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")

    text = raw.decode("utf-8", errors="replace")
    last_usage: TokenUsage | None = None

    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        usage = parse_usage_json(obj)
        if usage is not None:
            last_usage = usage

    return last_usage


def _safe_int(value: Any) -> tuple[bool, int | None]:
    if value is None:
        return True, None
    if isinstance(value, bool):
        return False, None
    if isinstance(value, int):
        if value < 0:
            return False, None
        return True, value
    if isinstance(value, str):
        if not value or not value.isascii() or not value.isdecimal():
            return False, None
        return True, int(value)
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            return False, None
        return True, int(value)
    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            return False, None
        return True, int(value)
    return False, None


__all__ = [
    "TokenUsage",
    "parse_usage_json",
    "parse_usage_sse",
]
