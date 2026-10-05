from tests.conftest import bundled_spec, make_spec
from veena.overlap import derive_shared, find_overlap, shared_asset_id
from veena.spec import Verdict


def test_find_overlap_runs_both_directions():
    report = find_overlap(bundled_spec("customer_spend_90d"), [bundled_spec("customer_spend_30d")])
    assert report.provider is None
    assert len(report.considered) == 2
    assert report.served_by[0].verdict is Verdict.CANNOT
    assert "window differs" in report.considered[0]


def test_find_overlap_finds_a_provider():
    report = find_overlap(
        bundled_spec("customer_spend_90d"), [bundled_spec("customer_spend_daily")]
    )
    assert report.provider is not None
    assert report.provider.verdict is Verdict.SERVES_WITH


def test_derived_shared_spec_matches_the_bundled_daily_spec():
    a, b = bundled_spec("customer_spend_30d"), bundled_spec("customer_spend_90d")
    derived = derive_shared(a, b, asset_id="customer_spend_daily", origin="proposal:p28")
    assert derived is not None
    expected = bundled_spec("customer_spend_daily")
    assert derived.model_dump(exclude={"provenance"}) == expected.model_dump(exclude={"provenance"})
    assert str(derived.provenance["grain.time_bucket"]) == "proposal:p28"
    assert str(derived.provenance["serves.customer_spend_30d"]) == "asset:customer_spend_30d@1"
    assert str(derived.provenance["serves.customer_spend_90d"]) == "asset:customer_spend_90d@1"
    assert shared_asset_id(a, b) == "customer_spend_daily"


def test_no_sharing_across_identity_access_or_timezone():
    base = make_spec()
    for other in (
        make_spec("x", identity="semantic:customer_identity_current@1"),
        make_spec("x", access="policy:restricted_access@1"),
        make_spec("x", timezone="Asia/Kolkata"),
    ):
        assert derive_shared(base, other, asset_id="shared", origin="proposal:p1") is None


def test_no_sharing_for_non_reaggregable_measures():
    from veena.spec import Aggregation

    a = make_spec(aggregation=Aggregation.AVG)
    b = make_spec("y", aggregation=Aggregation.AVG, complete_days=90)
    assert derive_shared(a, b, asset_id="shared", origin="proposal:p1") is None
