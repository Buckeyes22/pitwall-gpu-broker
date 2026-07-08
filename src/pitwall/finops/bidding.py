"""Deterministic cross-provider spot-market bidding decisions."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Literal, Protocol, cast

from pitwall.cost.circuit_breaker import CircuitBreakerDecision
from pitwall.policy import PolicyEvaluationResult

_USD_QUANTUM = Decimal("0.000001")

BidActionKind = Literal["place", "raise", "lower", "hold", "block"]


class SpotPriceFeed(Protocol):
    """Async source of normalized spot prices for one provider."""

    async def spot_prices(self) -> Sequence[SpotPrice]: ...


class BidPlacer(Protocol):
    """Applies one concrete bid action through a provider-specific adapter."""

    async def place_bid(self, action: BidAction) -> BidPlacementReceipt: ...


@dataclass(frozen=True, slots=True)
class SpotPrice:
    """One live spot-market lane normalized to hourly USD bidding units."""

    provider_id: str
    resource_id: str
    gpu: str
    minimum_bid_usd_per_hour: Decimal
    current_bid_usd_per_hour: Decimal | None = None
    uninterruptible_usd_per_hour: Decimal | None = None
    gpu_count: int = 1
    available: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must be non-empty")
        if not self.resource_id.strip():
            raise ValueError("resource_id must be non-empty")
        if not self.gpu.strip():
            raise ValueError("gpu must be non-empty")
        if self.gpu_count < 1:
            raise ValueError("gpu_count must be >= 1")
        object.__setattr__(
            self,
            "minimum_bid_usd_per_hour",
            _usd(self.minimum_bid_usd_per_hour, "minimum_bid_usd_per_hour"),
        )
        object.__setattr__(
            self,
            "current_bid_usd_per_hour",
            _optional_usd(self.current_bid_usd_per_hour, "current_bid_usd_per_hour"),
        )
        object.__setattr__(
            self,
            "uninterruptible_usd_per_hour",
            _optional_usd(self.uninterruptible_usd_per_hour, "uninterruptible_usd_per_hour"),
        )
        object.__setattr__(
            self,
            "metadata",
            cast(Mapping[str, object], MappingProxyType(dict(sorted(self.metadata.items())))),
        )

    def to_dict(self) -> dict[str, object]:
        """Return a stable, JSON-compatible representation."""

        return {
            "provider_id": self.provider_id,
            "resource_id": self.resource_id,
            "gpu": self.gpu,
            "minimum_bid_usd_per_hour": _decimal_to_str(self.minimum_bid_usd_per_hour),
            "current_bid_usd_per_hour": _optional_decimal_to_str(self.current_bid_usd_per_hour),
            "uninterruptible_usd_per_hour": _optional_decimal_to_str(
                self.uninterruptible_usd_per_hour
            ),
            "gpu_count": self.gpu_count,
            "available": self.available,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SpotPriceSnapshot:
    """Deterministic cross-provider spot-price snapshot."""

    observed_at: dt.datetime
    prices: tuple[SpotPrice, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _normalize_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "prices", tuple(self.prices))

    def to_dict(self) -> dict[str, object]:
        """Return a stable, JSON-compatible representation."""

        return {
            "observed_at": self.observed_at.isoformat(),
            "prices": [price.to_dict() for price in self.prices],
        }


@dataclass(frozen=True, slots=True)
class BiddingPolicy:
    """Target, max, and policy/budget rails for one bid evaluation."""

    target_price_usd_per_hour: Decimal
    max_price_usd_per_hour: Decimal
    target_capacity_units: int = 1
    max_parallel_bids: int = 1
    bid_increment_usd_per_hour: Decimal = Decimal("0")
    min_adjustment_usd_per_hour: Decimal = Decimal("0.000001")
    max_total_bid_usd_per_hour: Decimal | None = None
    allow_bid_decrease: bool = False
    dry_run: bool = True
    policy_evaluation: PolicyEvaluationResult | None = None
    budget_decision: CircuitBreakerDecision | None = None

    def __post_init__(self) -> None:
        target = _usd(self.target_price_usd_per_hour, "target_price_usd_per_hour")
        max_price = _usd(self.max_price_usd_per_hour, "max_price_usd_per_hour")
        if max_price < target:
            raise ValueError("max_price_usd_per_hour must be >= target_price_usd_per_hour")
        if self.target_capacity_units < 1:
            raise ValueError("target_capacity_units must be >= 1")
        if self.max_parallel_bids < 1:
            raise ValueError("max_parallel_bids must be >= 1")
        object.__setattr__(self, "target_price_usd_per_hour", target)
        object.__setattr__(self, "max_price_usd_per_hour", max_price)
        object.__setattr__(
            self,
            "bid_increment_usd_per_hour",
            _usd(self.bid_increment_usd_per_hour, "bid_increment_usd_per_hour"),
        )
        object.__setattr__(
            self,
            "min_adjustment_usd_per_hour",
            _usd(self.min_adjustment_usd_per_hour, "min_adjustment_usd_per_hour"),
        )
        object.__setattr__(
            self,
            "max_total_bid_usd_per_hour",
            _optional_usd(self.max_total_bid_usd_per_hour, "max_total_bid_usd_per_hour"),
        )

    @property
    def selection_limit(self) -> int:
        """Maximum count of spot lanes the evaluator may select."""

        return min(self.target_capacity_units, self.max_parallel_bids)

    def effective_max_price(self) -> Decimal:
        """Return the max price after budget-breaker downgrade rails."""

        if self.budget_decision is not None and self.budget_decision.action == "downgrade":
            return min(self.max_price_usd_per_hour, self.target_price_usd_per_hour)
        return self.max_price_usd_per_hour


@dataclass(frozen=True, slots=True)
class BidAction:
    """One recommended provider bid placement or adjustment."""

    provider_id: str
    resource_id: str
    gpu: str
    action: BidActionKind
    bid_usd_per_hour: Decimal
    minimum_bid_usd_per_hour: Decimal
    previous_bid_usd_per_hour: Decimal | None = None
    gpu_count: int = 1
    selected: bool = False
    rank: int | None = None
    dry_run: bool = True
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must be non-empty")
        if not self.resource_id.strip():
            raise ValueError("resource_id must be non-empty")
        if not self.gpu.strip():
            raise ValueError("gpu must be non-empty")
        if self.gpu_count < 1:
            raise ValueError("gpu_count must be >= 1")
        if self.rank is not None and self.rank < 1:
            raise ValueError("rank must be >= 1")
        object.__setattr__(self, "bid_usd_per_hour", _usd(self.bid_usd_per_hour, "bid_usd"))
        object.__setattr__(
            self,
            "minimum_bid_usd_per_hour",
            _usd(self.minimum_bid_usd_per_hour, "minimum_bid_usd_per_hour"),
        )
        object.__setattr__(
            self,
            "previous_bid_usd_per_hour",
            _optional_usd(self.previous_bid_usd_per_hour, "previous_bid_usd_per_hour"),
        )

    @property
    def executable(self) -> bool:
        """Whether this action can be sent to a provider bid API."""

        return self.selected and self.action in {"place", "raise", "lower"}

    def selected_copy(self, *, rank: int, dry_run: bool) -> BidAction:
        """Return this action marked as selected."""

        return BidAction(
            provider_id=self.provider_id,
            resource_id=self.resource_id,
            gpu=self.gpu,
            action=self.action,
            bid_usd_per_hour=self.bid_usd_per_hour,
            minimum_bid_usd_per_hour=self.minimum_bid_usd_per_hour,
            previous_bid_usd_per_hour=self.previous_bid_usd_per_hour,
            gpu_count=self.gpu_count,
            selected=True,
            rank=rank,
            dry_run=dry_run,
            reason=self.reason,
        )

    def hold_copy(self, *, reason: str, dry_run: bool) -> BidAction:
        """Return this action as an unselected hold recommendation."""

        return BidAction(
            provider_id=self.provider_id,
            resource_id=self.resource_id,
            gpu=self.gpu,
            action="hold",
            bid_usd_per_hour=self.bid_usd_per_hour,
            minimum_bid_usd_per_hour=self.minimum_bid_usd_per_hour,
            previous_bid_usd_per_hour=self.previous_bid_usd_per_hour,
            gpu_count=self.gpu_count,
            selected=False,
            rank=None,
            dry_run=dry_run,
            reason=reason,
        )

    def block_copy(self, *, reason: str, dry_run: bool) -> BidAction:
        """Return this action as an unselected block recommendation."""

        return BidAction(
            provider_id=self.provider_id,
            resource_id=self.resource_id,
            gpu=self.gpu,
            action="block",
            bid_usd_per_hour=self.bid_usd_per_hour,
            minimum_bid_usd_per_hour=self.minimum_bid_usd_per_hour,
            previous_bid_usd_per_hour=self.previous_bid_usd_per_hour,
            gpu_count=self.gpu_count,
            selected=False,
            rank=None,
            dry_run=dry_run,
            reason=reason,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a stable, JSON-compatible representation."""

        return {
            "provider_id": self.provider_id,
            "resource_id": self.resource_id,
            "gpu": self.gpu,
            "action": self.action,
            "bid_usd_per_hour": _decimal_to_str(self.bid_usd_per_hour),
            "minimum_bid_usd_per_hour": _decimal_to_str(self.minimum_bid_usd_per_hour),
            "previous_bid_usd_per_hour": _optional_decimal_to_str(self.previous_bid_usd_per_hour),
            "gpu_count": self.gpu_count,
            "selected": self.selected,
            "rank": self.rank,
            "dry_run": self.dry_run,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class BiddingPlan:
    """Deterministic plan emitted for one spot-price snapshot."""

    observed_at: dt.datetime
    effective_max_price_usd_per_hour: Decimal
    actions: tuple[BidAction, ...]
    dry_run: bool = True
    budget_action: str | None = None
    policy_allowed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _normalize_utc(self.observed_at, "observed_at"))
        object.__setattr__(
            self,
            "effective_max_price_usd_per_hour",
            _usd(
                self.effective_max_price_usd_per_hour,
                "effective_max_price_usd_per_hour",
            ),
        )
        object.__setattr__(self, "actions", tuple(self.actions))

    @property
    def selected_actions(self) -> tuple[BidAction, ...]:
        """Actions selected to satisfy target capacity."""

        return tuple(action for action in self.actions if action.selected)

    @property
    def executable_actions(self) -> tuple[BidAction, ...]:
        """Selected actions that would call provider bid APIs."""

        return tuple(action for action in self.selected_actions if action.executable)

    @property
    def blocked(self) -> bool:
        """Whether the plan could not select any bid lane."""

        return not self.selected_actions

    @property
    def total_selected_bid_usd_per_hour(self) -> Decimal:
        """Total hourly bid value across selected lanes."""

        return _usd(
            sum((action.bid_usd_per_hour for action in self.selected_actions), Decimal("0")),
            "total_selected_bid_usd_per_hour",
        )

    def to_dict(self) -> dict[str, object]:
        """Return a stable, JSON-compatible representation."""

        return {
            "observed_at": self.observed_at.isoformat(),
            "effective_max_price_usd_per_hour": _decimal_to_str(
                self.effective_max_price_usd_per_hour
            ),
            "dry_run": self.dry_run,
            "budget_action": self.budget_action,
            "policy_allowed": self.policy_allowed,
            "blocked": self.blocked,
            "total_selected_bid_usd_per_hour": _decimal_to_str(
                self.total_selected_bid_usd_per_hour
            ),
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class BidPlacementReceipt:
    """Provider adapter result for one applied bid."""

    provider_id: str
    resource_id: str
    bid_usd_per_hour: Decimal
    applied: bool
    raw: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider_id.strip():
            raise ValueError("provider_id must be non-empty")
        if not self.resource_id.strip():
            raise ValueError("resource_id must be non-empty")
        object.__setattr__(
            self,
            "bid_usd_per_hour",
            _usd(self.bid_usd_per_hour, "bid_usd_per_hour"),
        )
        object.__setattr__(
            self,
            "raw",
            cast(Mapping[str, object], MappingProxyType(dict(sorted(self.raw.items())))),
        )

    def to_dict(self) -> dict[str, object]:
        """Return a stable, JSON-compatible representation."""

        return {
            "provider_id": self.provider_id,
            "resource_id": self.resource_id,
            "bid_usd_per_hour": _decimal_to_str(self.bid_usd_per_hour),
            "applied": self.applied,
            "raw": dict(self.raw),
        }


class BiddingEngine:
    """Pure cross-provider spot-bidding evaluator."""

    def evaluate(self, snapshot: SpotPriceSnapshot, policy: BiddingPolicy) -> BiddingPlan:
        """Return a deterministic plan for one price snapshot and policy rail set."""

        hard_block_reason = _hard_block_reason(policy)
        effective_max = policy.effective_max_price()
        budget_action = (
            policy.budget_decision.action if policy.budget_decision is not None else None
        )
        policy_allowed = (
            policy.policy_evaluation.allowed if policy.policy_evaluation is not None else True
        )
        if hard_block_reason is not None:
            return BiddingPlan(
                observed_at=snapshot.observed_at,
                effective_max_price_usd_per_hour=effective_max,
                actions=tuple(
                    _blocked_action(price, reason=hard_block_reason, dry_run=policy.dry_run)
                    for price in _sorted_prices(snapshot.prices)
                ),
                dry_run=policy.dry_run,
                budget_action=budget_action,
                policy_allowed=policy_allowed,
            )

        candidate_actions = [
            _candidate_action(price, policy, effective_max=effective_max)
            for price in _sorted_prices(snapshot.prices)
        ]
        selected: list[BidAction] = []
        final_actions: list[BidAction] = []
        running_total = Decimal("0")

        for action in candidate_actions:
            if action.action == "block":
                final_actions.append(action)
                continue
            if len(selected) >= policy.selection_limit:
                final_actions.append(
                    action.hold_copy(reason="not selected", dry_run=policy.dry_run)
                )
                continue
            next_total = _usd(
                running_total + action.bid_usd_per_hour,
                "next_total_bid_usd_per_hour",
            )
            if (
                policy.max_total_bid_usd_per_hour is not None
                and next_total > policy.max_total_bid_usd_per_hour
            ):
                final_actions.append(
                    action.block_copy(
                        reason="budget bid cap exceeded",
                        dry_run=policy.dry_run,
                    )
                )
                continue
            selected_action = action.selected_copy(rank=len(selected) + 1, dry_run=policy.dry_run)
            selected.append(selected_action)
            final_actions.append(selected_action)
            running_total = next_total

        return BiddingPlan(
            observed_at=snapshot.observed_at,
            effective_max_price_usd_per_hour=effective_max,
            actions=tuple(final_actions),
            dry_run=policy.dry_run,
            budget_action=budget_action,
            policy_allowed=policy_allowed,
        )


async def collect_spot_price_snapshot(
    feeds: Iterable[SpotPriceFeed],
    *,
    observed_at: dt.datetime,
) -> SpotPriceSnapshot:
    """Collect prices from provider feeds while preserving configured feed order."""

    prices: list[SpotPrice] = []
    for feed in feeds:
        prices.extend(await feed.spot_prices())
    return SpotPriceSnapshot(observed_at=observed_at, prices=tuple(prices))


async def execute_bidding_plan(
    plan: BiddingPlan,
    placer: BidPlacer,
    *,
    apply: bool = False,
) -> tuple[BidPlacementReceipt, ...]:
    """Apply executable bid actions only when the caller sets ``apply=True``."""

    if not apply:
        return ()
    receipts: list[BidPlacementReceipt] = []
    for action in plan.executable_actions:
        receipts.append(await placer.place_bid(action))
    return tuple(receipts)


def _candidate_action(
    price: SpotPrice,
    policy: BiddingPolicy,
    *,
    effective_max: Decimal,
) -> BidAction:
    if not price.available:
        return _blocked_action(price, reason="capacity unavailable", dry_run=policy.dry_run)

    desired_bid = _desired_bid(price, policy)
    if desired_bid > effective_max:
        return _blocked_action(
            price,
            reason="minimum bid exceeds max price",
            dry_run=policy.dry_run,
            bid_usd_per_hour=effective_max,
        )

    action, reason = _action_for_bid(price, policy, desired_bid)
    return BidAction(
        provider_id=price.provider_id,
        resource_id=price.resource_id,
        gpu=price.gpu,
        action=action,
        bid_usd_per_hour=desired_bid,
        minimum_bid_usd_per_hour=price.minimum_bid_usd_per_hour,
        previous_bid_usd_per_hour=price.current_bid_usd_per_hour,
        gpu_count=price.gpu_count,
        dry_run=policy.dry_run,
        reason=reason,
    )


def _desired_bid(price: SpotPrice, policy: BiddingPolicy) -> Decimal:
    if price.minimum_bid_usd_per_hour <= policy.target_price_usd_per_hour:
        return policy.target_price_usd_per_hour
    return _usd(
        price.minimum_bid_usd_per_hour + policy.bid_increment_usd_per_hour,
        "desired_bid_usd_per_hour",
    )


def _action_for_bid(
    price: SpotPrice,
    policy: BiddingPolicy,
    desired_bid: Decimal,
) -> tuple[BidActionKind, str]:
    current = price.current_bid_usd_per_hour
    if current is None:
        reason = (
            "place bid at target"
            if desired_bid == policy.target_price_usd_per_hour
            else "place bid to clear current market"
        )
        return "place", reason

    if desired_bid - current > policy.min_adjustment_usd_per_hour:
        return "raise", "raise bid to clear current market"

    if current - desired_bid > policy.min_adjustment_usd_per_hour:
        if policy.allow_bid_decrease:
            reason = (
                "lower bid to target"
                if desired_bid == policy.target_price_usd_per_hour
                else "lower bid to current market"
            )
            return "lower", reason
        return "hold", "current bid above desired; decrease disabled"

    return "hold", "current bid already within adjustment band"


def _blocked_action(
    price: SpotPrice,
    *,
    reason: str,
    dry_run: bool,
    bid_usd_per_hour: Decimal | None = None,
) -> BidAction:
    return BidAction(
        provider_id=price.provider_id,
        resource_id=price.resource_id,
        gpu=price.gpu,
        action="block",
        bid_usd_per_hour=bid_usd_per_hour or price.minimum_bid_usd_per_hour,
        minimum_bid_usd_per_hour=price.minimum_bid_usd_per_hour,
        previous_bid_usd_per_hour=price.current_bid_usd_per_hour,
        gpu_count=price.gpu_count,
        selected=False,
        dry_run=dry_run,
        reason=reason,
    )


def _hard_block_reason(policy: BiddingPolicy) -> str | None:
    if policy.policy_evaluation is not None and not policy.policy_evaluation.allowed:
        if policy.policy_evaluation.violations:
            first_violation = policy.policy_evaluation.violations[0]
            return f"policy denied: {first_violation.policy_id}"
        return "policy denied"
    if policy.budget_decision is not None and policy.budget_decision.action == "block":
        return policy.budget_decision.reason
    return None


def _sorted_prices(prices: Sequence[SpotPrice]) -> tuple[SpotPrice, ...]:
    return tuple(
        sorted(
            prices,
            key=lambda price: (
                price.minimum_bid_usd_per_hour,
                price.provider_id,
                price.resource_id,
                price.gpu,
            ),
        )
    )


def _normalize_utc(value: dt.datetime, field_name: str) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(dt.UTC)


def _usd(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return parsed.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)


def _optional_usd(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _usd(value, field_name)


def _decimal_to_str(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _decimal_to_str(value)


__all__ = [
    "BidAction",
    "BidActionKind",
    "BidPlacementReceipt",
    "BidPlacer",
    "BiddingEngine",
    "BiddingPlan",
    "BiddingPolicy",
    "SpotPrice",
    "SpotPriceFeed",
    "SpotPriceSnapshot",
    "collect_spot_price_snapshot",
    "execute_bidding_plan",
]
