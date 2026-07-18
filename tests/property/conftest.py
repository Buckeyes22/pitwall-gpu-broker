"""Hypothesis profiles for Pitwall property-based tests.

Profiles:
    dev   — fast, for local iteration (default; ~50 examples)
    ci    — thorough, for CI (~1000 examples, no deadline)
    debug — tiny + verbose, for minimizing a counterexample

Select with: HYPOTHESIS_PROFILE=ci pytest -m property
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, Verbosity, settings

settings.register_profile("dev", max_examples=50, deadline=None)
settings.register_profile(
    "ci",
    max_examples=1000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("debug", max_examples=10, verbosity=Verbosity.verbose, deadline=None)

settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "dev"))
