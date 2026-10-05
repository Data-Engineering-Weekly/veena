from tests.conftest import make_spec
from veena.spec import Aggregation, Operation, TimeBucket, Verdict, check, load_spec


def test_identical_specs_are_equal():
    a = make_spec()
    b = make_spec()
    result = check(a, b)
    assert result.verdict is Verdict.EQUAL
    assert result.operations == ()


def test_thirty_day_window_cannot_serve_ninety():
    result = check(make_spec(complete_days=30), make_spec("customer_spend_90d", complete_days=90))
    assert result.verdict is Verdict.CANNOT
    assert any("window differs" in r for r in result.reasons)


def test_daily_asset_serves_both_windows_with_window_and_population_join(spec_dir):
    daily = load_spec(spec_dir / "customer_spend_daily.yaml")
    for name in ("customer_spend_30d", "customer_spend_90d"):
        result = check(daily, load_spec(spec_dir / f"{name}.yaml"))
        assert result.verdict is Verdict.SERVES_WITH, result.reasons
        assert result.operations == (Operation.POPULATION_JOIN, Operation.WINDOW)


def test_daily_asset_with_short_history_cannot_serve_ninety():
    daily = make_spec(
        "customer_spend_daily", time_bucket=TimeBucket.DAY, complete_days=None, history_days=60
    )
    result = check(daily, make_spec("customer_spend_90d", complete_days=90))
    assert result.verdict is Verdict.CANNOT
    assert any("insufficient history" in r for r in result.reasons)


def test_windowed_asset_cannot_serve_daily():
    daily = make_spec(
        "customer_spend_daily", time_bucket=TimeBucket.DAY, complete_days=None, history_days=120
    )
    result = check(make_spec(), daily)
    assert result.verdict is Verdict.CANNOT


def test_different_identity_policy_blocks_sharing():
    other = make_spec("customer_spend_30d_naive", identity="semantic:customer_identity_current@1")
    result = check(other, make_spec())
    assert result.verdict is Verdict.CANNOT
    assert any("identity policy differs" in r for r in result.reasons)


def test_different_access_policy_blocks_sharing():
    result = check(make_spec(access="policy:restricted_access@1"), make_spec())
    assert result.verdict is Verdict.CANNOT


def test_different_timezone_blocks_sharing():
    result = check(make_spec(timezone="America/New_York"), make_spec())
    assert result.verdict is Verdict.CANNOT


def test_non_reaggregable_measure_blocks_rollup_from_daily():
    daily = make_spec(
        "customer_spend_daily",
        time_bucket=TimeBucket.DAY,
        complete_days=None,
        history_days=120,
        aggregation=Aggregation.AVG,
    )
    consumer = make_spec(aggregation=Aggregation.AVG)
    result = check(daily, consumer)
    assert result.verdict is Verdict.CANNOT
    assert any("re-aggregated" in r for r in result.reasons)


def test_all_reasons_are_reported_together():
    provider = make_spec(
        "other",
        identity="semantic:customer_identity_current@1",
        access="policy:restricted_access@1",
        timezone="Asia/Kolkata",
    )
    result = check(provider, make_spec())
    assert len(result.reasons) == 3
