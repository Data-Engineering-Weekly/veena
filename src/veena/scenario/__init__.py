"""A synthetic DuckDB workspace that replays the article's customer-spend story."""

from veena.scenario.synth import (
    DriftKind,
    ScenarioConfig,
    ScenarioStats,
    apply_drift,
    build_scenario,
    scenario_stats,
)
from veena.scenario.truth import approved_spend_sql, naive_spend_sql

__all__ = [
    "DriftKind",
    "ScenarioConfig",
    "ScenarioStats",
    "apply_drift",
    "approved_spend_sql",
    "build_scenario",
    "naive_spend_sql",
    "scenario_stats",
]
