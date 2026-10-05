from datetime import date

import pytest

from veena.errors import ScenarioError
from veena.scenario import (
    ScenarioConfig,
    approved_spend_sql,
    build_scenario,
    naive_spend_sql,
    scenario_stats,
)


@pytest.fixture(scope="module")
def conn():
    connection = build_scenario(None, ScenarioConfig())
    yield connection
    connection.close()


def test_scenario_has_every_trap(conn):
    stats = scenario_stats(conn)
    assert stats.customers == 200
    assert stats.inactive_customers >= 30
    assert stats.remapped_accounts >= 10
    assert stats.refunded_purchases > 0
    assert stats.purchases > stats.accounts


def test_same_seed_gives_same_tables():
    a = build_scenario(None, ScenarioConfig(seed=11))
    b = build_scenario(None, ScenarioConfig(seed=11))
    assert (
        a.execute("SELECT * FROM purchases ORDER BY purchase_id").fetchall()
        == b.execute("SELECT * FROM purchases ORDER BY purchase_id").fetchall()
    )


def test_approved_spend_has_one_row_per_customer_including_zero(conn):
    rows = conn.execute(approved_spend_sql(30, date(2026, 9, 30))).fetchall()
    assert len(rows) == 200
    assert len({r[0] for r in rows}) == 200
    assert any(r[1] == 0 for r in rows)


def test_naive_join_double_counts_remapped_accounts(conn):
    approved = dict(conn.execute(approved_spend_sql(90, date(2026, 9, 30))).fetchall())
    naive = dict(conn.execute(naive_spend_sql(90, date(2026, 9, 30))).fetchall())
    inflated = [c for c in approved if naive[c] > approved[c]]
    assert inflated, "the naive join should over count at least one customer"
    assert all(naive[c] >= approved[c] for c in approved)
    assert sum(naive.values()) > sum(approved.values())


def test_every_settled_purchase_counts_exactly_once(conn):
    total_settled = conn.execute(
        """
        SELECT COALESCE(SUM(amount_usd), 0) FROM purchases
        WHERE status = 'settled'
          AND purchased_at >= TIMESTAMPTZ '2026-07-03 00:00:00+00'
          AND purchased_at <  TIMESTAMPTZ '2026-10-01 00:00:00+00'
        """
    ).fetchone()[0]
    attributed = sum(
        r[1] for r in conn.execute(approved_spend_sql(90, date(2026, 9, 30))).fetchall()
    )
    assert attributed == total_settled


def test_build_refuses_to_overwrite(tmp_path):
    target = tmp_path / "scenario.duckdb"
    build_scenario(target, ScenarioConfig(customers=20)).close()
    with pytest.raises(ScenarioError, match="already exists"):
        build_scenario(target, ScenarioConfig(customers=20))
