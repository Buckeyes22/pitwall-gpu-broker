"""Release-tier test fixtures for Pitwall's public-alpha validation harness.

This package holds the tiered release tests:
  - dry-run    : validate configuration without spending
  - sovereignty: validate data residency / region constraints
  - BGE-M3 smoke: validate live BGE-M3 endpoint exit criteria
  - kill drill : validate L15 kill-switch separation

Release tests are gated behind the ``release`` marker. They are skipped by
default and must be run explicitly with ``pytest -m release`` or via the
CI release workflow.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "release: marks a test as a public-alpha validation tier (skipped by default)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("-m", default=None) == "release":
        return
    for item in items:
        if "release" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="release tier: run with -m release"))
