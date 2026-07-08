"""Property tests for Policy-as-Code evaluation invariants."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pitwall.policy import Policy, PolicyRule, PolicySet, PolicyTarget, evaluate_policies

pytestmark = pytest.mark.property


class _AuditConfig:
    def __init__(self, providers: list[dict[str, object]]) -> None:
        self._providers = providers

    def provider_fixtures(self) -> list[dict[str, object]]:
        return self._providers


@given(has_forbidden_keys=st.lists(st.booleans(), max_size=8))
def test_contains_none_violates_exactly_matching_provider_targets(
    has_forbidden_keys: list[bool],
) -> None:
    policies = PolicySet(
        policies=[
            Policy(
                id="provider.no-static-r2-env",
                target=PolicyTarget.PROVIDER,
                rules=[
                    PolicyRule(
                        path="config.env_vars",
                        operator="contains_none",
                        value=["R2_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"],
                    )
                ],
            )
        ]
    )
    providers = [
        {
            "id": f"provider_{index}",
            "provider_type": "pod_lease",
            "config": (
                {"env_vars": {"R2_ACCESS_KEY": f"secret_{index}"}}
                if has_forbidden
                else {"env_vars": {"SAFE_PUBLIC_FLAG": "1"}}
            ),
        }
        for index, has_forbidden in enumerate(has_forbidden_keys)
    ]

    result = evaluate_policies(policies, _AuditConfig(providers))

    assert [violation.target_id for violation in result.violations] == [
        f"provider_{index}"
        for index, has_forbidden in enumerate(has_forbidden_keys)
        if has_forbidden
    ]
    assert result.allowed is (not any(has_forbidden_keys))
