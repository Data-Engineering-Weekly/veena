from pathlib import Path

from tests.conftest import AS_OF, bundled_spec
from veena.backends import DuckDBBackend
from veena.evidence import CheckResult, Grade, verification_grade
from veena.planner import IdentityJoin, compile_plan
from veena.verification import build_checks, verify


def _materialize(conn, semantic, name, *, naive=False):
    backend = DuckDBBackend(conn)
    plan = compile_plan(bundled_spec(name), semantic)
    if naive:
        plan = plan.model_copy(
            update={
                "steps": tuple(
                    s.model_copy(update={"respect_validity": False})
                    if isinstance(s, IdentityJoin)
                    else s
                    for s in plan.steps
                )
            }
        )
    sql, _ = backend.render(plan, as_of=AS_OF)
    backend.materialize(name, sql, plan)
    return backend


def test_thirty_day_spec_yields_four_checks(semantic):
    checks = build_checks(bundled_spec("customer_spend_30d"), semantic, "rel", as_of=AS_OF)
    assert [c.name for c in checks] == [
        "unique_grain",
        "population_preserved",
        "reconcile_spend_usd",
        "conserve_spend_usd",
    ]
    assert [c.structural for c in checks] == [True, True, False, False]
    assert checks[2].grade is Grade.RECONCILIATION


def test_correct_implementation_passes_with_reconciliation_grade(scenario_conn, semantic):
    backend = _materialize(scenario_conn, semantic, "customer_spend_30d")
    outcome = verify(
        bundled_spec("customer_spend_30d"),
        semantic,
        "customer_spend_30d",
        backend.execute,
        as_of=AS_OF,
    )
    assert outcome.passed, [c for c in outcome.checks if not c.passed]
    assert outcome.grade is Grade.RECONCILIATION
    assert len(outcome.checks) == 4


def test_naive_implementation_passes_structure_and_fails_reconciliation(scenario_conn, semantic):
    backend = _materialize(scenario_conn, semantic, "customer_spend_30d", naive=True)
    outcome = verify(
        bundled_spec("customer_spend_30d"),
        semantic,
        "customer_spend_30d",
        backend.execute,
        as_of=AS_OF,
    )
    by_name = {c.name: c for c in outcome.checks}
    assert by_name["unique_grain"].passed
    assert by_name["population_preserved"].passed
    assert not by_name["reconcile_spend_usd"].passed
    assert not by_name["conserve_spend_usd"].passed
    assert not outcome.passed
    detail = by_name["reconcile_spend_usd"].detail
    assert "keys differ" in detail
    assert "total discrepancy" in detail
    assert "c0" in detail  # at least one customer id is named
    remapped = {
        r[0]
        for r in scenario_conn.execute(
            "SELECT customer_id FROM identity_mappings "
            "GROUP BY account_id, customer_id HAVING count(*) >= 1"
        ).fetchall()
    }
    assert any(cid in detail for cid in remapped)


def test_daily_asset_verifies(scenario_conn, semantic):
    backend = _materialize(scenario_conn, semantic, "customer_spend_daily")
    outcome = verify(
        bundled_spec("customer_spend_daily"),
        semantic,
        "customer_spend_daily",
        backend.execute,
        as_of=AS_OF,
    )
    assert outcome.passed, [c for c in outcome.checks if not c.passed]
    assert [c.name for c in outcome.checks][:2] == ["unique_grain", "population_known"]


def test_grade_is_sample_without_passing_value_checks():
    structural_only = (
        CheckResult(name="u", description="", grade=Grade.TEST, structural=True, passed=True),
    )
    assert verification_grade(structural_only) is Grade.SAMPLE
    failed_value = (
        *structural_only,
        CheckResult(
            name="r", description="", grade=Grade.RECONCILIATION, structural=False, passed=False
        ),
    )
    assert verification_grade(failed_value) is Grade.SAMPLE


def test_verification_never_imports_the_planner_or_backends():
    root = Path(__file__).resolve().parents[2] / "src" / "veena" / "verification"
    for path in root.glob("*.py"):
        imports = [
            line for line in path.read_text().splitlines() if line.startswith(("from ", "import "))
        ]
        assert not any("veena.planner" in line for line in imports), path
        assert not any("veena.backends" in line for line in imports), path
