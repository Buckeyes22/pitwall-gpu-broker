"""FinOps analytics for Pitwall."""

from __future__ import annotations

from pitwall.finops.bidding import (
    BidAction,
    BidActionKind,
    BiddingEngine,
    BiddingPlan,
    BiddingPolicy,
    BidPlacementReceipt,
    BidPlacer,
    SpotPrice,
    SpotPriceFeed,
    SpotPriceSnapshot,
    collect_spot_price_snapshot,
    execute_bidding_plan,
)
from pitwall.finops.burn_rate import (
    BurnRateForecast,
    BurnRateForecaster,
    SpendPoint,
    forecast_from_cost_daily,
)
from pitwall.finops.reservations import (
    DemandForecast,
    PlanEvaluation,
    RecommendationAction,
    ReservationCandidate,
    ReservationLine,
    ReservationRecommendation,
    ReservationRecommender,
    recommend_reservations,
)
from pitwall.finops.time_machine import (
    CounterfactualScenario,
    HistoricalRoutingDecision,
    TimeMachineReplay,
    TimeMachineReport,
    TimeMachineSummary,
    TimeMachineWorkloadReport,
)

__all__ = [
    "BurnRateForecast",
    "BurnRateForecaster",
    "SpendPoint",
    "forecast_from_cost_daily",
    "DemandForecast",
    "PlanEvaluation",
    "RecommendationAction",
    "ReservationCandidate",
    "ReservationLine",
    "ReservationRecommendation",
    "ReservationRecommender",
    "recommend_reservations",
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
    "CounterfactualScenario",
    "HistoricalRoutingDecision",
    "TimeMachineReport",
    "TimeMachineReplay",
    "TimeMachineSummary",
    "TimeMachineWorkloadReport",
]
