"""Live-call gate for Pitwall — gate real RunPod calls behind env flags.

Smoke scripts and production code that need to call real RunPod endpoints
must check ``is_live()`` or ``require_live()`` before proceeding. This
prevents accidental live calls in hermetic / CI environments.

Two env vars control the gate:

* ``RUNPOD_LIVE=1`` (or alias ``PITWALL_RUN_LIVE=1``) — opt-in to live calls.
* ``PITWALL_BASE_URL`` — the base URL of the running Pitwall server. Must be
  non-empty for live calls to proceed.
"""

from __future__ import annotations

import os
import sys

_LIVE_ENV_VARS = ("RUNPOD_LIVE", "PITWALL_RUN_LIVE")
_BASE_URL_ENV = "PITWALL_BASE_URL"


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def is_live() -> bool:
    """Return True when live RunPod calls are explicitly opted into.

    Requires both:
      1. ``RUNPOD_LIVE=1`` (or ``PITWALL_RUN_LIVE=1``)
      2. ``PITWALL_BASE_URL`` is set to a non-empty value
    """
    live_flag = any(_truthy(os.environ.get(name)) for name in _LIVE_ENV_VARS)
    base_url = os.environ.get(_BASE_URL_ENV, "").strip()
    return live_flag and bool(base_url)


def require_live() -> None:
    """Exit with an error if live calls are not opted into.

    Used by smoke scripts and CLI tools that should refuse to run
    unless both ``RUNPOD_LIVE=1`` and ``PITWALL_BASE_URL`` are set.
    """
    if is_live():
        return
    live_flag_set = any(_truthy(os.environ.get(name)) for name in _LIVE_ENV_VARS)
    if not live_flag_set:
        print(
            "live calls require RUNPOD_LIVE=1 (or PITWALL_RUN_LIVE=1)",
            file=sys.stderr,
        )
    else:
        print(
            f"live calls require {_BASE_URL_ENV} to be set",
            file=sys.stderr,
        )
    raise SystemExit(1)
