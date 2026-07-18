from __future__ import annotations

import string
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import given
from hypothesis import strategies as st

from pitwall.gitops import PlanAction, PlanEntityType, build_reconcile_plan, load_desired_state

_NAME = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=12)


def _write_state(path: Path, names: list[str]) -> Path:
    body = ["apiVersion: pitwall.dev/v1", "capabilities:"]
    for name in names:
        body.extend(
            [
                f"  - name: embedding.{name}",
                "    class: embedding",
                "    cost_mode: per_second",
            ]
        )
    path.write_text("\n".join(body), encoding="utf-8")
    return path


@given(names=st.lists(_NAME, min_size=1, max_size=12, unique=True))
def test_gitops_create_plan_order_is_deterministic_for_any_yaml_order(
    names: list[str],
) -> None:
    with TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        forward = load_desired_state([_write_state(tmp_path / "forward.yaml", names)])
        reverse = load_desired_state(
            [_write_state(tmp_path / "reverse.yaml", list(reversed(names)))]
        )

    forward_plan = build_reconcile_plan(
        forward,
        current_capabilities=[],
        current_providers=[],
    )
    reverse_plan = build_reconcile_plan(
        reverse,
        current_capabilities=[],
        current_providers=[],
    )

    forward_operations = [
        (op.entity_type, op.action, op.entity_id)
        for op in forward_plan.operations
        if op.entity_type == PlanEntityType.CAPABILITY
    ]
    reverse_operations = [
        (op.entity_type, op.action, op.entity_id)
        for op in reverse_plan.operations
        if op.entity_type == PlanEntityType.CAPABILITY
    ]

    assert forward_operations == reverse_operations
    assert forward_operations == [
        (PlanEntityType.CAPABILITY, PlanAction.CREATE, f"cap_embedding_{name}")
        for name in sorted(names)
    ]
