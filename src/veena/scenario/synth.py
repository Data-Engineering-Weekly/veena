"""Generate the synthetic sources: customers, accounts, identity mappings, purchases.

Purchases come in baskets so daily aggregates compress the facts. The generator plants two
traps the engine must handle. Some accounts move from one customer
to another on a known date, so a join on account id alone counts purchases twice. Some
purchases are refunded, so the approved amount definition must filter by status.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from veena.errors import ScenarioError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class ScenarioConfig(BaseModel):
    """Knobs for the generator. Same config and seed give the same tables."""

    model_config = ConfigDict(frozen=True)

    seed: int = 7
    customers: int = Field(default=200, ge=10)
    inactive_share: float = Field(default=0.15, ge=0.0, le=0.9)
    remapped_share: float = Field(default=0.10, ge=0.0, le=0.9)
    refunded_share: float = Field(default=0.05, ge=0.0, le=0.9)
    history_days: int = Field(default=120, ge=30)
    baskets_per_active_customer: float = Field(default=10.0, gt=0.0)
    items_per_basket: float = Field(default=6.0, ge=1.0)
    last_complete_day: date = date(2026, 9, 30)


class ScenarioStats(BaseModel):
    """Row counts and trap counts for a built scenario."""

    model_config = ConfigDict(frozen=True)

    customers: int
    accounts: int
    identity_mappings: int
    purchases: int
    remapped_accounts: int
    refunded_purchases: int
    inactive_customers: int


def build_scenario(target: Path | None, config: ScenarioConfig) -> duckdb.DuckDBPyConnection:
    """Create the four source tables in a DuckDB database.

    Args:
        target: Database file to create. `None` builds an in-memory database.
        config: Generator settings.

    Returns:
        An open connection to the populated database.

    Raises:
        ScenarioError: The target file already exists.
    """
    if target is not None and target.exists():
        msg = f"{target} already exists; remove it or pick another path"
        raise ScenarioError(msg)
    rng = random.Random(config.seed)
    conn = duckdb.connect(str(target) if target is not None else ":memory:")
    _create_tables(conn)
    _populate(conn, rng, config)
    return conn


def scenario_stats(conn: duckdb.DuckDBPyConnection) -> ScenarioStats:
    """Count rows and traps in a built scenario."""

    def one(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0

    return ScenarioStats(
        customers=one("SELECT count(*) FROM customers"),
        accounts=one("SELECT count(*) FROM accounts"),
        identity_mappings=one("SELECT count(*) FROM identity_mappings"),
        purchases=one("SELECT count(*) FROM purchases"),
        remapped_accounts=one(
            "SELECT count(*) FROM (SELECT account_id FROM identity_mappings "
            "GROUP BY account_id HAVING count(*) > 1)"
        ),
        refunded_purchases=one("SELECT count(*) FROM purchases WHERE status = 'refunded'"),
        inactive_customers=one(
            "SELECT count(*) FROM customers c WHERE NOT EXISTS ("
            "SELECT 1 FROM identity_mappings m JOIN purchases p USING (account_id) "
            "WHERE m.customer_id = c.customer_id)"
        ),
    )


def _create_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE customers (
            customer_id VARCHAR PRIMARY KEY,
            created_at  TIMESTAMPTZ NOT NULL,
            region      VARCHAR NOT NULL
        );
        CREATE TABLE accounts (
            account_id VARCHAR PRIMARY KEY,
            opened_at  TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE identity_mappings (
            mapping_id  VARCHAR PRIMARY KEY,
            account_id  VARCHAR NOT NULL,
            customer_id VARCHAR NOT NULL,
            valid_from  TIMESTAMPTZ NOT NULL,
            valid_to    TIMESTAMPTZ
        );
        CREATE TABLE purchases (
            purchase_id  VARCHAR PRIMARY KEY,
            account_id   VARCHAR NOT NULL,
            purchased_at TIMESTAMPTZ NOT NULL,
            amount_usd   DECIMAL(12, 2) NOT NULL,
            status       VARCHAR NOT NULL
        );
        CREATE TABLE households_by_residence (
            household_id VARCHAR NOT NULL,
            customer_id  VARCHAR NOT NULL,
            PRIMARY KEY (household_id, customer_id)
        );
        CREATE TABLE households_by_billing (
            household_id VARCHAR NOT NULL,
            customer_id  VARCHAR NOT NULL,
            PRIMARY KEY (household_id, customer_id)
        );
        """
    )


def _populate(conn: duckdb.DuckDBPyConnection, rng: random.Random, config: ScenarioConfig) -> None:
    end = datetime.combine(config.last_complete_day + timedelta(days=1), datetime.min.time(), UTC)
    start = end - timedelta(days=config.history_days)
    regions = ("na", "eu", "apac")

    customers = [
        (f"c{i:05d}", start - timedelta(days=rng.randint(30, 400)), rng.choice(regions))
        for i in range(config.customers)
    ]
    inactive_count = int(config.customers * config.inactive_share)
    inactive = {c[0] for c in rng.sample(customers, inactive_count)}
    active = [c for c in customers if c[0] not in inactive]

    accounts: list[tuple[str, datetime]] = []
    mappings: list[tuple[str, str, str, datetime, datetime | None]] = []
    remapped_count = int(len(active) * config.remapped_share)
    remapped = {c[0] for c in rng.sample(active, remapped_count)}

    for customer_id, created_at, _ in active:
        account_id = f"a{len(accounts):05d}"
        accounts.append((account_id, created_at))
        if customer_id in remapped:
            # The account belonged to another active customer first and moved inside the window,
            # so purchases on it straddle two mapping rows.
            previous = rng.choice([c for c in active if c[0] != customer_id])[0]
            switch = start + timedelta(days=rng.randint(10, config.history_days - 10))
            mappings.append((f"m{len(mappings):05d}", account_id, previous, created_at, switch))
            mappings.append((f"m{len(mappings):05d}", account_id, customer_id, switch, None))
        else:
            mappings.append((f"m{len(mappings):05d}", account_id, customer_id, created_at, None))

    # Purchases arrive in baskets: a customer buys several items in one visit, so one
    # customer-day holds several rows and a daily aggregate is much smaller than the facts.
    purchases: list[tuple[str, str, datetime, float, str]] = []
    span_seconds = int((end - start).total_seconds())
    for account_id, _ in accounts:
        baskets = max(1, int(rng.expovariate(1.0 / config.baskets_per_active_customer)))
        for _ in range(baskets):
            visit = start + timedelta(seconds=rng.randint(0, span_seconds - 1))
            items = max(1, int(rng.expovariate(1.0 / config.items_per_basket)))
            for item in range(items):
                at = visit + timedelta(seconds=item * rng.randint(5, 90))
                amount = round(rng.lognormvariate(3.0, 0.8), 2)
                status = "refunded" if rng.random() < config.refunded_share else "settled"
                purchases.append((f"p{len(purchases):06d}", account_id, at, amount, status))

    # Two approved ways to group customers into households. They disagree on purpose, so a
    # request that says "household" without choosing one is ambiguous.
    residence = _group(rng, [c[0] for c in customers], prefix="hr", mean_size=2.2)
    billing = _group(rng, [c[0] for c in customers], prefix="hb", mean_size=1.6)

    _bulk_insert(conn, "customers", customers)
    _bulk_insert(conn, "accounts", accounts)
    _bulk_insert(conn, "households_by_residence", residence)
    _bulk_insert(conn, "households_by_billing", billing)
    _bulk_insert(conn, "identity_mappings", mappings)
    _bulk_insert(conn, "purchases", purchases)


def _group(
    rng: random.Random, customer_ids: list[str], *, prefix: str, mean_size: float
) -> list[tuple[str, str]]:
    shuffled = list(customer_ids)
    rng.shuffle(shuffled)
    rows: list[tuple[str, str]] = []
    index = 0
    household = 0
    while index < len(shuffled):
        size = max(1, int(rng.expovariate(1.0 / mean_size)))
        for customer_id in shuffled[index : index + size]:
            rows.append((f"{prefix}{household:05d}", customer_id))
        index += size
        household += 1
    return rows


def _bulk_insert(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    rows: Sequence[tuple[object, ...]],
    batch: int = 2000,
) -> None:
    # Literal multi-row VALUES parse in milliseconds; binding thousands of parameters does not.
    for start in range(0, len(rows), batch):
        chunk = rows[start : start + batch]
        values = ", ".join("(" + ", ".join(_literal(v) for v in row) + ")" for row in chunk)
        conn.execute(f"INSERT INTO {table} VALUES {values}")


def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, datetime):
        return f"TIMESTAMPTZ '{value.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')}+00'"
    if isinstance(value, int | float):
        return repr(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"
