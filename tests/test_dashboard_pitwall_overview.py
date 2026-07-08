"""Tests for the Pitwall Grafana dashboard JSON.

Add dashboard coverage — validates required panel titles,
datasource usage, and absence of legacy metric names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DASHBOARD_FILE = _REPO_ROOT / "dashboards" / "pitwall-overview.json"

_REQUIRED_PANEL_TITLES = frozenset(
    [
        "Monthly burn rate",
        "Cost per workload",
        "Active cloud fleet",
        "Cost per provider",
        "Monthly spend total",
        "Recent kills",
    ]
)

_FORBIDDEN_METRIC_PREFIXES = ("legacy_cloud", "legacy_")


@pytest.fixture
def dashboard() -> dict:
    return json.loads(_DASHBOARD_FILE.read_text())


def test_dashboard_file_exists() -> None:
    assert _DASHBOARD_FILE.exists(), f"Dashboard not found at {_DASHBOARD_FILE}"


def test_dashboard_title(dashboard: dict) -> None:
    assert dashboard.get("title") == "Pitwall Overview"


def test_dashboard_uid(dashboard: dict) -> None:
    assert dashboard.get("uid") == "pitwall-overview"


def test_dashboard_has_panels(dashboard: dict) -> None:
    panels = dashboard.get("panels", [])
    assert len(panels) >= 6, f"Expected at least 6 panels, got {len(panels)}"


def test_all_panels_have_required_titles(dashboard: dict) -> None:
    panels = dashboard.get("panels", [])
    actual_titles = {p["title"] for p in panels}
    missing = _REQUIRED_PANEL_TITLES - actual_titles
    assert not missing, f"Missing required panel titles: {missing}"


def test_all_panels_use_prometheus_datasource(dashboard: dict) -> None:
    panels = dashboard.get("panels", [])
    for panel in panels:
        ds = panel.get("datasource", {})
        assert ds.get("type") == "prometheus", (
            f"Panel '{panel.get('title')}' has datasource type '{ds.get('type')}', "
            "expected 'prometheus'"
        )
        assert ds.get("uid") == "${DS_PROMETHEUS}", (
            f"Panel '{panel.get('title')}' has datasource uid '{ds.get('uid')}', "
            "expected '${DS_PROMETHEUS}'"
        )


def test_no_legacy_metric_names_in_dashboard(dashboard: dict) -> None:
    dashboard_str = json.dumps(dashboard)
    for prefix in _FORBIDDEN_METRIC_PREFIXES:
        pattern = rf"{re.escape(prefix)}\w+"
        matches = re.findall(pattern, dashboard_str)
        assert not matches, (
            f"Found legacy metric names in dashboard: {matches}. "
            "Dashboard should use pitwall_* metrics, not legacy_* metrics."
        )


def test_no_legacy_metric_names_in_panel_expressions(dashboard: dict) -> None:
    panels = dashboard.get("panels", [])
    for panel in panels:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            for prefix in _FORBIDDEN_METRIC_PREFIXES:
                pattern = rf"{re.escape(prefix)}\w+"
                matches = re.findall(pattern, expr)
                assert not matches, (
                    f"Panel '{panel.get('title')}' target contains legacy metric "
                    f"'{matches[0]}' in expression: {expr}"
                )
