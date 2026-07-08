"""Read-only RunPod billing/credits read for FinOps reconciliation.

Provides typed access to RunPod account credit balance and spend metadata,
plus reconciliation helpers that compare provider-reported numbers against
Pitwall's internal budget gate state.

All money fields use :class:`decimal.Decimal`; floats are never accepted or
produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pitwall.runpod_client.graphql import RunpodCreditsBalance, RunpodGraphQLClient


@dataclass(frozen=True)
class BillingSnapshot:
    """RunPod account billing state captured at a point in time."""

    user_id: str | None
    client_balance_usd: Decimal
    current_spend_per_hr_usd: Decimal | None
    spend_limit_usd: Decimal | None
    min_balance_usd: Decimal | None
    under_balance: bool

    @classmethod
    def from_runpod(cls, balance: RunpodCreditsBalance) -> BillingSnapshot:
        """Build a snapshot from a GraphQL :class:`RunpodCreditsBalance`."""
        return cls(
            user_id=balance.user_id,
            client_balance_usd=balance.client_balance,
            current_spend_per_hr_usd=balance.current_spend_per_hr,
            spend_limit_usd=balance.spend_limit,
            min_balance_usd=balance.min_balance,
            under_balance=balance.under_balance,
        )

    def to_serializable_dict(self) -> dict[str, str | bool | None]:
        """Return a stdlib-JSON-safe dict with Decimal values as strings."""
        return {
            "user_id": self.user_id,
            "client_balance_usd": str(self.client_balance_usd),
            "current_spend_per_hr_usd": (
                str(self.current_spend_per_hr_usd)
                if self.current_spend_per_hr_usd is not None
                else None
            ),
            "spend_limit_usd": (
                str(self.spend_limit_usd) if self.spend_limit_usd is not None else None
            ),
            "min_balance_usd": (
                str(self.min_balance_usd) if self.min_balance_usd is not None else None
            ),
            "under_balance": self.under_balance,
        }


@dataclass(frozen=True)
class BudgetReconciliation:
    """Comparison of RunPod provider numbers against Pitwall internal budget."""

    runpod_balance_usd: Decimal
    runpod_spend_per_hr_usd: Decimal | None
    runpod_spend_limit_usd: Decimal | None
    runpod_under_balance: bool
    pitwall_mtd_spend_usd: Decimal
    pitwall_monthly_budget_usd: Decimal
    pitwall_budget_remaining_usd: Decimal
    variance_usd: Decimal

    def to_serializable_dict(self) -> dict[str, str | bool | None]:
        """Return a stdlib-JSON-safe dict with Decimal values as strings."""
        return {
            "runpod_balance_usd": str(self.runpod_balance_usd),
            "runpod_spend_per_hr_usd": (
                str(self.runpod_spend_per_hr_usd)
                if self.runpod_spend_per_hr_usd is not None
                else None
            ),
            "runpod_spend_limit_usd": (
                str(self.runpod_spend_limit_usd)
                if self.runpod_spend_limit_usd is not None
                else None
            ),
            "runpod_under_balance": self.runpod_under_balance,
            "pitwall_mtd_spend_usd": str(self.pitwall_mtd_spend_usd),
            "pitwall_monthly_budget_usd": str(self.pitwall_monthly_budget_usd),
            "pitwall_budget_remaining_usd": str(self.pitwall_budget_remaining_usd),
            "variance_usd": str(self.variance_usd),
        }


@runtime_checkable
class BudgetGateLike(Protocol):
    """Protocol for budget-gate-like objects that can provide MTD spend."""

    monthly_budget_usd: Decimal

    async def current_mtd_spend(self) -> Decimal: ...


async def read_billing_snapshot(
    client: RunpodGraphQLClient,
) -> BillingSnapshot:
    """Fetch RunPod account billing state via GraphQL.

    Raises:
        RunpodGraphQLError: when RunPod returns a GraphQL ``errors`` envelope.
        RunpodGraphQLHTTPError: on non-2xx HTTP status.
        RunpodGraphQLResponseError: on unexpected response shape.
    """
    balance = await client.credits_balance()
    return BillingSnapshot.from_runpod(balance)


async def reconcile_with_budget(
    client: RunpodGraphQLClient,
    budget_gate: BudgetGateLike,
) -> BudgetReconciliation:
    """Reconcile RunPod provider numbers with Pitwall internal budget state.

    Computes ``variance_usd`` as the delta between RunPod's reported
    ``client_balance`` and Pitwall's computed ``budget_remaining``
    (``monthly_budget - mtd_spend``). A positive variance means RunPod
    shows more remaining credit than Pitwall has unspent budget; a
    negative variance means RunPod balance is lower than Pitwall's
    remaining budget.
    """
    billing = await read_billing_snapshot(client)
    mtd_spend = await budget_gate.current_mtd_spend()
    monthly_budget = budget_gate.monthly_budget_usd
    remaining = monthly_budget - mtd_spend
    if remaining < 0:
        remaining = Decimal("0")
    variance = billing.client_balance_usd - remaining

    return BudgetReconciliation(
        runpod_balance_usd=billing.client_balance_usd,
        runpod_spend_per_hr_usd=billing.current_spend_per_hr_usd,
        runpod_spend_limit_usd=billing.spend_limit_usd,
        runpod_under_balance=billing.under_balance,
        pitwall_mtd_spend_usd=mtd_spend,
        pitwall_monthly_budget_usd=monthly_budget,
        pitwall_budget_remaining_usd=remaining,
        variance_usd=variance,
    )


__all__ = [
    "BillingSnapshot",
    "BudgetGateLike",
    "BudgetReconciliation",
    "read_billing_snapshot",
    "reconcile_with_budget",
]
