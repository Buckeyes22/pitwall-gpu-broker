"""Recommendations engine for Pitwall.

Aggregates signals from scorecards, drift detection, burn-rate forecasting,
and reservation planning into prioritized, actionable operator recommendations.
"""

from __future__ import annotations

from pitwall.recommendations.engine import (
    Recommendation,
    RecommendationCategory,
    RecommendationEngine,
    ScorecardMetric,
)

__all__ = [
    "Recommendation",
    "RecommendationCategory",
    "RecommendationEngine",
    "ScorecardMetric",
]
