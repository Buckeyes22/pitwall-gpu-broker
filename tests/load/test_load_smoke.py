"""Smoke checks for the Locust load profile (release program).

The actual load run is operator/CI-driven (needs a running Pitwall host), so it
is left as a skipped placeholder. The shape check below catches a broken
locustfile (renamed task, dropped weight, wrong route) by parsing it with ``ast``
— it does NOT import locust, whose gevent monkey-patching is unsafe once ssl is
already imported in-process. ``slow``-marked so it stays out of the fast lane.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

_LOCUSTFILE = Path(__file__).parent / "locustfile.py"


def _task_weights(tree: ast.Module) -> dict[str, int]:
    """Map each @task(<weight>)-decorated method name to its integer weight."""
    weights: dict[str, int] = {}
    user_cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "PitwallUser"
    )
    for item in user_cls.body:
        if not isinstance(item, ast.FunctionDef):
            continue
        for dec in item.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Name)
                and dec.func.id == "task"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                weights[item.name] = int(dec.args[0].value)
    return weights


def test_locustfile_shape() -> None:
    source = _LOCUSTFILE.read_text()
    tree = ast.parse(source)

    # PitwallUser subclasses HttpUser with the expected weighted tasks.
    assert "class PitwallUser(HttpUser)" in source
    assert _task_weights(tree) == {"list_capabilities": 8, "dry_run_inference": 2}

    # The exercised routes are the public read + a no-spend dry-run write.
    assert '"/v1/capabilities"' in source
    assert '"/v1/inference"' in source
    assert '"dry_run": True' in source


@pytest.mark.skip(reason="operator/CI load run: needs a live Pitwall host (make load)")
def test_load_run_against_live_host() -> None:  # pragma: no cover
    raise AssertionError("run `make load` against a deployed Pitwall instance")
