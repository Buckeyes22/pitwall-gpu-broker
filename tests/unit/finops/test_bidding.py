"""Hermetic unit tests for cross-provider spot-market bidding decisions."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import pytest

from pitwall.cost.circuit_breaker import CircuitBreakerDecision
from pitwall.finops.bidding import (
    BidAction,
    BiddingEngine,
    BiddingPolicy,
    BidPlacementReceipt,
    SpotPrice,
    SpotPriceSnapshot,
    collect_spot_price_snapshot,
    execute_bidding_plan,
)
from pitwall.policy import (
    PolicyEvaluationResult,
    PolicyOperator,
    PolicyTarget,
    PolicyViolation,
)

pytestmark = pytest.mark.anyio

_NOW = dt.datetime(2026, 6, 2, 15, 0, tzinfo=dt.UTC)


def _price(
    provider_id: str,
    *,
    price: str,
    current_bid: str | None = None,
    gpu: str = "NVIDIA L4",
    resource_id: str | None = None,
) -> SpotPrice:
    return SpotPrice(
        provider_id=provider_id,
        resource_id=resource_id or f"{provider_id}-slot",
        gpu=gpu,
        minimum_bid_usd_per_hour=Decimal(price),
        current_bid_usd_per_hour=Decimal(current_bid) if current_bid is not None else None,
    )


def _snapshot(*prices: SpotPrice) -> SpotPriceSnapshot:
    return SpotPriceSnapshot(observed_at=_NOW, prices=prices)


def _policy(**overrides: object) -> BiddingPolicy:
    kwargs: dict[str, object] = {
        "target_price_usd_per_hour": Decimal("0.50"),
        "max_price_usd_per_hour": Decimal("0.80"),
    }
    kwargs.update(overrides)
    return BiddingPolicy(**kwargs)


def test_places_single_best_dry_run_bid_at_target_price() -> None:
    plan = BiddingEngine().evaluate(
        _snapshot(
            _price("runpod", price="0.42"),
            _price("vast", price="0.46"),
        ),
        _policy(),
    )

    assert plan.dry_run is True
    assert plan.selected_actions[0].provider_id == "runpod"
    assert plan.selected_actions[0].action == "place"
    assert plan.selected_actions[0].bid_usd_per_hour == Decimal("0.500000")
    assert [action.provider_id for action in plan.actions] == ["runpod", "vast"]
    assert plan.actions[1].action == "hold"
    assert plan.actions[1].reason == "not selected"


def test_raises_bid_to_clear_market_when_price_is_above_target_but_within_max() -> None:
    plan = BiddingEngine().evaluate(
        _snapshot(_price("runpod", price="0.61", current_bid="0.55")),
        _policy(bid_increment_usd_per_hour=Decimal("0.01")),
    )

    action = plan.selected_actions[0]
    assert action.action == "raise"
    assert action.bid_usd_per_hour == Decimal("0.620000")
    assert action.reason == "raise bid to clear current market"


def test_blocks_over_max_candidate_and_selects_next_cheapest_provider() -> None:
    plan = BiddingEngine().evaluate(
        _snapshot(
            _price("runpod", price="0.91"),
            _price("vast", price="0.58"),
        ),
        _policy(),
    )

    assert plan.selected_actions[0].provider_id == "vast"
    assert plan.selected_actions[0].action == "place"
    assert plan.actions[1].provider_id == "runpod"
    assert plan.actions[1].action == "block"
    assert plan.actions[1].reason == "minimum bid exceeds max price"


def test_lowers_existing_bid_when_market_falls_and_decrease_is_allowed() -> None:
    plan = BiddingEngine().evaluate(
        _snapshot(_price("runpod", price="0.31", current_bid="0.72")),
        _policy(allow_bid_decrease=True),
    )

    action = plan.selected_actions[0]
    assert action.action == "lower"
    assert action.bid_usd_per_hour == Decimal("0.500000")
    assert action.reason == "lower bid to target"


def test_holds_existing_bid_inside_adjustment_band() -> None:
    plan = BiddingEngine().evaluate(
        _snapshot(_price("runpod", price="0.40", current_bid="0.504")),
        _policy(min_adjustment_usd_per_hour=Decimal("0.01")),
    )

    action = plan.selected_actions[0]
    assert action.action == "hold"
    assert action.bid_usd_per_hour == Decimal("0.500000")
    assert action.reason == "current bid already within adjustment band"
    assert plan.executable_actions == ()


def test_policy_denial_blocks_all_bid_adjustments() -> None:
    denied = PolicyEvaluationResult(
        allowed=False,
        decision="deny",
        violations=(
            PolicyViolation(
                policy_id="bid.max",
                target=PolicyTarget.PROVIDER,
                target_id="runpod",
                path="bid",
                operator=PolicyOperator.LTE,
                expected="0.40",
                actual="0.50",
                message="bid exceeds provider policy",
            ),
        ),
    )

    plan = BiddingEngine().evaluate(
        _snapshot(_price("runpod", price="0.40")),
        _policy(policy_evaluation=denied),
    )

    assert plan.blocked is True
    assert plan.selected_actions == ()
    assert plan.actions[0].action == "block"
    assert plan.actions[0].reason == "policy denied: bid.max"


def test_budget_breaker_block_and_downgrade_actions_apply_budget_rails() -> None:
    blocked_plan = BiddingEngine().evaluate(
        _snapshot(_price("runpod", price="0.40")),
        _policy(
            budget_decision=CircuitBreakerDecision(
                action="block",
                reason="budget exhausted",
                state="open",
                headroom_usd=Decimal("0"),
                headroom_pct=Decimal("0"),
                runway_hours=None,
            )
        ),
    )
    downgraded_plan = BiddingEngine().evaluate(
        _snapshot(_price("runpod", price="0.61")),
        _policy(
            budget_decision=CircuitBreakerDecision(
                action="downgrade",
                reason="headroom low",
                state="open",
                headroom_usd=Decimal("10"),
                headroom_pct=Decimal("10"),
                runway_hours=None,
            ),
            bid_increment_usd_per_hour=Decimal("0.01"),
        ),
    )

    assert blocked_plan.blocked is True
    assert blocked_plan.actions[0].reason == "budget exhausted"
    assert downgraded_plan.blocked is True
    assert downgraded_plan.actions[0].action == "block"
    assert downgraded_plan.actions[0].reason == "minimum bid exceeds max price"


async def test_collects_fake_price_feeds_and_gates_actual_bid_placement() -> None:
    class FakeFeed:
        def __init__(self, prices: Sequence[SpotPrice]) -> None:
            self._prices = tuple(prices)

        async def spot_prices(self) -> Sequence[SpotPrice]:
            return self._prices

    class FakePlacer:
        def __init__(self) -> None:
            self.calls: list[BidAction] = []

        async def place_bid(self, action: BidAction) -> BidPlacementReceipt:
            self.calls.append(action)
            return BidPlacementReceipt(
                provider_id=action.provider_id,
                resource_id=action.resource_id,
                bid_usd_per_hour=action.bid_usd_per_hour,
                applied=True,
                raw={"provider_id": action.provider_id},
            )

    snapshot = await collect_spot_price_snapshot(
        [FakeFeed([_price("runpod", price="0.42")]), FakeFeed([_price("vast", price="0.50")])],
        observed_at=_NOW,
    )
    plan = BiddingEngine().evaluate(snapshot, _policy())
    placer = FakePlacer()

    dry_run_receipts = await execute_bidding_plan(plan, placer, apply=False)
    applied_receipts = await execute_bidding_plan(plan, placer, apply=True)

    assert [price.provider_id for price in snapshot.prices] == ["runpod", "vast"]
    assert dry_run_receipts == ()
    assert len(placer.calls) == 1
    assert applied_receipts[0].provider_id == "runpod"
    assert applied_receipts[0].applied is True
