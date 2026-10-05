from datetime import timedelta
from pathlib import Path

import pytest

from veena.spec import (
    Aggregation,
    AssetSpec,
    Grain,
    Measure,
    Population,
    ServiceRequirements,
    TimeBucket,
    Window,
)

SPECS = Path(__file__).resolve().parents[1] / "src" / "veena" / "scenario" / "specs"
SEMANTIC = Path(__file__).resolve().parents[1] / "src" / "veena" / "scenario" / "semantic.yaml"


def make_spec(
    asset_id: str = "customer_spend_30d",
    *,
    time_bucket: TimeBucket = TimeBucket.NONE,
    complete_days: int | None = 30,
    history_days: int | None = None,
    include_inactive: bool = True,
    aggregation: Aggregation = Aggregation.SUM,
    identity: str = "semantic:customer_identity_point_in_time@2",
    access: str = "policy:workspace_access@1",
    timezone: str = "UTC",
    keys: tuple[str, ...] = ("semantic:customer@1",),
) -> AssetSpec:
    return AssetSpec.model_validate(
        {
            "asset_id": asset_id,
            "population": Population.model_validate(
                {"entity": "semantic:customer@1", "include_inactive": include_inactive}
            ),
            "grain": Grain.model_validate({"keys": keys, "time_bucket": time_bucket}),
            "measures": (
                Measure.model_validate(
                    {
                        "name": "spend_usd",
                        "calculation": "semantic:purchase_amount_usd@4",
                        "aggregation": aggregation,
                    }
                ),
            ),
            "window": Window(
                complete_calendar_days=complete_days, history_days=history_days, timezone=timezone
            ),
            "identity_policy": identity,
            "service": ServiceRequirements.model_validate(
                {
                    "incorporate_within": timedelta(minutes=5),
                    "access_policy": access,
                    "budget_policy": "policy:workspace_budget@1",
                }
            ),
        }
    )


@pytest.fixture
def spec_dir() -> Path:
    return SPECS


@pytest.fixture
def semantic_path() -> Path:
    return SEMANTIC


AS_OF = __import__("datetime").datetime(2026, 10, 1, 0, 0, tzinfo=__import__("datetime").UTC)


@pytest.fixture(scope="session")
def semantic():
    from veena.semantic import load_semantic_model

    return load_semantic_model(SEMANTIC)


@pytest.fixture
def scenario_conn():
    from veena.scenario import ScenarioConfig, build_scenario

    conn = build_scenario(None, ScenarioConfig())
    yield conn
    conn.close()


def bundled_spec(name: str):
    from veena.spec import load_spec

    return load_spec(SPECS / f"{name}.yaml")
