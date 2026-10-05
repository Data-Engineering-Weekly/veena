"""Generate a realistic dbt manifest and warehouse query history for an e-commerce project.

The project is "shoply": raw sources, staging, intermediate, and mart models, exposures, and
MetricFlow semantic models and metrics, as dbt 1.9 writes them for a Snowflake adapter. Three
teams own three models that mean the same thing, which is the duplicate the engine must find.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from veena.importer.history import QueryRecord

if TYPE_CHECKING:
    from pathlib import Path

PROJECT = "shoply"
DATABASE = "analytics"
RAW_DATABASE = "raw"
SCHEMA_VERSION = "https://schemas.getdbt.com/dbt/manifest/v12.json"


class DbtFixtureConfig(BaseModel):
    """Knobs for the fixture generator."""

    model_config = ConfigDict(frozen=True)

    seed: int = 7
    history_days: int = Field(default=14, ge=1)
    generated_at: datetime = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)


# ---- sources ---------------------------------------------------------------------------------

_SOURCES: dict[str, tuple[str, dict[str, str], str]] = {
    # name: (description, columns, loader)
    "customers": (
        "One row per registered customer.",
        {
            "customer_id": "varchar",
            "email": "varchar",
            "created_at": "timestamp_tz",
            "region": "varchar",
        },
        "fivetran",
    ),
    "addresses": (
        "Customer addresses with a type: shipping, billing, residence.",
        {
            "address_id": "varchar",
            "customer_id": "varchar",
            "address_type": "varchar",
            "postal_code": "varchar",
            "valid_from": "timestamp_tz",
            "valid_to": "timestamp_tz",
        },
        "fivetran",
    ),
    "accounts": (
        "Login accounts. An account may change hands between customers.",
        {"account_id": "varchar", "opened_at": "timestamp_tz", "status": "varchar"},
        "fivetran",
    ),
    "account_customer_map": (
        "Which customer owned an account over which interval.",
        {
            "mapping_id": "varchar",
            "account_id": "varchar",
            "customer_id": "varchar",
            "valid_from": "timestamp_tz",
            "valid_to": "timestamp_tz",
        },
        "fivetran",
    ),
    "orders": (
        "One row per order header.",
        {
            "order_id": "varchar",
            "account_id": "varchar",
            "ordered_at": "timestamp_tz",
            "status": "varchar",
            "currency": "varchar",
            "shipping_address_id": "varchar",
            "campaign_id": "varchar",
        },
        "fivetran",
    ),
    "order_items": (
        "One row per line item.",
        {
            "order_item_id": "varchar",
            "order_id": "varchar",
            "product_id": "varchar",
            "quantity": "integer",
            "unit_price": "number(12,2)",
            "discount": "number(12,2)",
        },
        "fivetran",
    ),
    "payments": (
        "Payment attempts against orders.",
        {
            "payment_id": "varchar",
            "order_id": "varchar",
            "paid_at": "timestamp_tz",
            "amount": "number(12,2)",
            "method": "varchar",
            "status": "varchar",
        },
        "stripe",
    ),
    "refunds": (
        "Refunds issued against payments.",
        {
            "refund_id": "varchar",
            "payment_id": "varchar",
            "refunded_at": "timestamp_tz",
            "amount": "number(12,2)",
            "reason": "varchar",
        },
        "stripe",
    ),
    "products": (
        "Product catalog.",
        {
            "product_id": "varchar",
            "sku": "varchar",
            "name": "varchar",
            "category_id": "varchar",
            "list_price": "number(12,2)",
            "active": "boolean",
        },
        "fivetran",
    ),
    "categories": (
        "Product categories, two levels.",
        {"category_id": "varchar", "name": "varchar", "parent_category_id": "varchar"},
        "fivetran",
    ),
    "inventory_snapshots": (
        "Daily on-hand inventory per product and warehouse.",
        {
            "snapshot_date": "date",
            "product_id": "varchar",
            "warehouse": "varchar",
            "on_hand": "integer",
            "reserved": "integer",
        },
        "airbyte",
    ),
    "web_sessions": (
        "Web sessions from the analytics SDK.",
        {
            "session_id": "varchar",
            "account_id": "varchar",
            "started_at": "timestamp_tz",
            "device": "varchar",
            "utm_campaign": "varchar",
            "landing_page": "varchar",
        },
        "segment",
    ),
    "page_views": (
        "Page views within sessions.",
        {
            "page_view_id": "varchar",
            "session_id": "varchar",
            "viewed_at": "timestamp_tz",
            "path": "varchar",
            "product_id": "varchar",
        },
        "segment",
    ),
    "marketing_campaigns": (
        "Campaigns with spend and channel.",
        {
            "campaign_id": "varchar",
            "name": "varchar",
            "channel": "varchar",
            "started_at": "timestamp_tz",
            "ended_at": "timestamp_tz",
            "spend_usd": "number(12,2)",
        },
        "airbyte",
    ),
    "email_events": (
        "Email sends, opens, clicks.",
        {
            "event_id": "varchar",
            "customer_id": "varchar",
            "campaign_id": "varchar",
            "event_type": "varchar",
            "occurred_at": "timestamp_tz",
        },
        "braze",
    ),
}

# ---- models ----------------------------------------------------------------------------------
# name: (layer, materialized, owner, description, depends_on names, columns, sql)
# `sql` uses {{ ref() }} and {{ source() }}; compiled code substitutes fully qualified names.

_MODELS: dict[str, dict[str, Any]] = {}


def _model(
    name: str,
    *,
    layer: str,
    materialized: str,
    owner: str,
    description: str,
    depends_on: list[str],
    columns: dict[str, str],
    sql: str,
    tags: list[str] | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    _MODELS[name] = {
        "layer": layer,
        "materialized": materialized,
        "owner": owner,
        "description": description,
        "depends_on": depends_on,
        "columns": columns,
        "sql": sql.strip(),
        "tags": tags or [],
        "meta": meta or {},
    }


for _src, (_desc, _cols, _) in _SOURCES.items():
    _model(
        f"stg_{_src}",
        layer="staging",
        materialized="view",
        owner="data-platform",
        description=f"Staged {_src}: renamed and typed, no business logic. {_desc}",
        depends_on=[f"source:{_src}"],
        columns=dict(_cols),
        sql=f"select {', '.join(_cols)} from {{{{ source('raw', '{_src}') }}}}",
    )

_model(
    "int_customer_identity",
    layer="intermediate",
    materialized="table",
    owner="data-platform",
    description="Point-in-time account to customer mapping with validity intervals.",
    depends_on=["stg_account_customer_map"],
    columns={
        "account_id": "varchar",
        "customer_id": "varchar",
        "valid_from": "timestamp_tz",
        "valid_to": "timestamp_tz",
    },
    sql="select account_id, customer_id, valid_from, valid_to from {{ ref('stg_account_customer_map') }}",
)
_model(
    "int_orders_with_payments",
    layer="intermediate",
    materialized="table",
    owner="data-platform",
    description="Orders joined to their settled payments and refunds. One row per order.",
    depends_on=["stg_orders", "stg_payments", "stg_refunds"],
    columns={
        "order_id": "varchar",
        "account_id": "varchar",
        "ordered_at": "timestamp_tz",
        "status": "varchar",
        "paid_usd": "number(12,2)",
        "refunded_usd": "number(12,2)",
        "net_usd": "number(12,2)",
    },
    sql="""
select o.order_id, o.account_id, o.ordered_at, o.status,
       coalesce(p.paid_usd, 0) as paid_usd, coalesce(r.refunded_usd, 0) as refunded_usd,
       coalesce(p.paid_usd, 0) - coalesce(r.refunded_usd, 0) as net_usd
from {{ ref('stg_orders') }} o
left join (select order_id, sum(amount) as paid_usd from {{ ref('stg_payments') }} where status = 'settled' group by 1) p on p.order_id = o.order_id
left join (select pay.order_id, sum(rf.amount) as refunded_usd from {{ ref('stg_refunds') }} rf join {{ ref('stg_payments') }} pay on pay.payment_id = rf.payment_id group by 1) r on r.order_id = o.order_id
""",
)
_model(
    "int_order_items_enriched",
    layer="intermediate",
    materialized="table",
    owner="data-platform",
    description="Line items with product and category attributes and extended price.",
    depends_on=["stg_order_items", "stg_products", "stg_categories"],
    columns={
        "order_item_id": "varchar",
        "order_id": "varchar",
        "product_id": "varchar",
        "category_id": "varchar",
        "category_name": "varchar",
        "quantity": "integer",
        "extended_price": "number(12,2)",
    },
    sql="""
select i.order_item_id, i.order_id, i.product_id, p.category_id, c.name as category_name,
       i.quantity, i.quantity * i.unit_price - i.discount as extended_price
from {{ ref('stg_order_items') }} i
join {{ ref('stg_products') }} p on p.product_id = i.product_id
left join {{ ref('stg_categories') }} c on c.category_id = p.category_id
""",
)
_model(
    "int_customer_daily_spend",
    layer="intermediate",
    materialized="incremental",
    owner="data-platform",
    description="Net settled spend per customer per UTC day, attributed by the account owner at order time.",
    depends_on=["int_orders_with_payments", "int_customer_identity"],
    columns={
        "customer_id": "varchar",
        "order_day": "date",
        "net_usd": "number(12,2)",
        "order_count": "integer",
    },
    sql="""
select m.customer_id, cast(convert_timezone('UTC', o.ordered_at) as date) as order_day,
       sum(o.net_usd) as net_usd, count(*) as order_count
from {{ ref('int_orders_with_payments') }} o
join {{ ref('int_customer_identity') }} m
  on m.account_id = o.account_id and o.ordered_at >= m.valid_from and (m.valid_to is null or o.ordered_at < m.valid_to)
group by 1, 2
""",
    tags=["internal"],
)
_model(
    "int_sessions_attributed",
    layer="intermediate",
    materialized="table",
    owner="marketing-analytics",
    description="Sessions attributed to a customer through the account owner at session start.",
    depends_on=["stg_web_sessions", "int_customer_identity"],
    columns={
        "session_id": "varchar",
        "customer_id": "varchar",
        "started_at": "timestamp_tz",
        "utm_campaign": "varchar",
        "device": "varchar",
    },
    sql="""
select s.session_id, m.customer_id, s.started_at, s.utm_campaign, s.device
from {{ ref('stg_web_sessions') }} s
left join {{ ref('int_customer_identity') }} m
  on m.account_id = s.account_id and s.started_at >= m.valid_from and (m.valid_to is null or s.started_at < m.valid_to)
""",
    tags=["internal"],
)

# ---- marts ----
_model(
    "dim_customers",
    layer="marts",
    materialized="table",
    owner="data-platform",
    description="One row per customer with current residence postal code and first order date.",
    depends_on=["stg_customers", "stg_addresses", "int_customer_daily_spend"],
    columns={
        "customer_id": "varchar",
        "email": "varchar",
        "region": "varchar",
        "residence_postal_code": "varchar",
        "first_order_day": "date",
    },
    sql="""
select c.customer_id, c.email, c.region, a.postal_code as residence_postal_code, min(d.order_day) as first_order_day
from {{ ref('stg_customers') }} c
left join {{ ref('stg_addresses') }} a on a.customer_id = c.customer_id and a.address_type = 'residence' and a.valid_to is null
left join {{ ref('int_customer_daily_spend') }} d on d.customer_id = c.customer_id
group by 1, 2, 3, 4
""",
)
_model(
    "dim_products",
    layer="marts",
    materialized="table",
    owner="merchandising",
    description="Product dimension with category hierarchy.",
    depends_on=["stg_products", "stg_categories"],
    columns={
        "product_id": "varchar",
        "sku": "varchar",
        "name": "varchar",
        "category_name": "varchar",
        "parent_category_name": "varchar",
        "list_price": "number(12,2)",
        "active": "boolean",
    },
    sql="""
select p.product_id, p.sku, p.name, c.name as category_name, pc.name as parent_category_name, p.list_price, p.active
from {{ ref('stg_products') }} p
left join {{ ref('stg_categories') }} c on c.category_id = p.category_id
left join {{ ref('stg_categories') }} pc on pc.category_id = c.parent_category_id
""",
)
_model(
    "fct_orders",
    layer="marts",
    materialized="incremental",
    owner="data-platform",
    description="One row per order with customer attribution and net settled amount.",
    depends_on=["int_orders_with_payments", "int_customer_identity"],
    columns={
        "order_id": "varchar",
        "customer_id": "varchar",
        "ordered_at": "timestamp_tz",
        "status": "varchar",
        "net_usd": "number(12,2)",
    },
    sql="""
select o.order_id, m.customer_id, o.ordered_at, o.status, o.net_usd
from {{ ref('int_orders_with_payments') }} o
join {{ ref('int_customer_identity') }} m
  on m.account_id = o.account_id and o.ordered_at >= m.valid_from and (m.valid_to is null or o.ordered_at < m.valid_to)
""",
)
_model(
    "fct_order_items",
    layer="marts",
    materialized="incremental",
    owner="merchandising",
    description="One row per line item with customer and category.",
    depends_on=["int_order_items_enriched", "fct_orders"],
    columns={
        "order_item_id": "varchar",
        "order_id": "varchar",
        "customer_id": "varchar",
        "product_id": "varchar",
        "category_name": "varchar",
        "quantity": "integer",
        "extended_price": "number(12,2)",
        "ordered_at": "timestamp_tz",
    },
    sql="""
select i.order_item_id, i.order_id, o.customer_id, i.product_id, i.category_name, i.quantity, i.extended_price, o.ordered_at
from {{ ref('int_order_items_enriched') }} i join {{ ref('fct_orders') }} o on o.order_id = i.order_id
""",
)
_model(
    "fct_payments",
    layer="marts",
    materialized="table",
    owner="finance",
    description="Settled payments with order and customer.",
    depends_on=["stg_payments", "fct_orders"],
    columns={
        "payment_id": "varchar",
        "order_id": "varchar",
        "customer_id": "varchar",
        "paid_at": "timestamp_tz",
        "amount": "number(12,2)",
        "method": "varchar",
    },
    sql="""
select p.payment_id, p.order_id, o.customer_id, p.paid_at, p.amount, p.method
from {{ ref('stg_payments') }} p join {{ ref('fct_orders') }} o on o.order_id = p.order_id where p.status = 'settled'
""",
)
_model(
    "fct_refunds",
    layer="marts",
    materialized="table",
    owner="finance",
    description="Refunds with the customer who received them.",
    depends_on=["stg_refunds", "fct_payments"],
    columns={
        "refund_id": "varchar",
        "payment_id": "varchar",
        "customer_id": "varchar",
        "refunded_at": "timestamp_tz",
        "amount": "number(12,2)",
        "reason": "varchar",
    },
    sql="""
select r.refund_id, r.payment_id, p.customer_id, r.refunded_at, r.amount, r.reason
from {{ ref('stg_refunds') }} r join {{ ref('fct_payments') }} p on p.payment_id = r.payment_id
""",
)
_model(
    "fct_sessions",
    layer="marts",
    materialized="incremental",
    owner="marketing-analytics",
    description="Sessions with page view counts and whether an order followed within the session.",
    depends_on=["int_sessions_attributed", "stg_page_views"],
    columns={
        "session_id": "varchar",
        "customer_id": "varchar",
        "started_at": "timestamp_tz",
        "utm_campaign": "varchar",
        "device": "varchar",
        "page_views": "integer",
    },
    sql="""
select s.session_id, s.customer_id, s.started_at, s.utm_campaign, s.device, count(v.page_view_id) as page_views
from {{ ref('int_sessions_attributed') }} s left join {{ ref('stg_page_views') }} v on v.session_id = s.session_id
group by 1, 2, 3, 4, 5
""",
)

# Three teams, three models, one meaning: net settled spend per customer over 30 complete UTC days.
_SPEND_30 = """
select c.customer_id, coalesce(sum(d.net_usd), 0) as {col}
from {{{{ ref('stg_customers') }}}} c
left join {{{{ ref('int_customer_daily_spend') }}}} d
  on d.customer_id = c.customer_id
 and d.order_day >= dateadd(day, -30, current_date()) and d.order_day < current_date()
group by 1
"""
_model(
    "customer_spend_30d",
    layer="marts",
    materialized="table",
    owner="sales-engineering",
    description="Net settled spend per customer over the previous 30 complete UTC days, including customers with no orders. Feeds the sales application.",
    depends_on=["stg_customers", "int_customer_daily_spend"],
    columns={"customer_id": "varchar", "spend_usd": "number(12,2)"},
    sql=_SPEND_30.format(col="spend_usd"),
    meta={"sla_minutes": 5},
)
_model(
    "cust_spend_last_30d",
    layer="marts",
    materialized="table",
    owner="marketing-analytics",
    description="Customer spend, last 30 days, for the marketing dashboard.",
    depends_on=["stg_customers", "int_customer_daily_spend"],
    columns={"customer_id": "varchar", "spend_last_30d": "number(12,2)"},
    sql=_SPEND_30.format(col="spend_last_30d"),
)
_model(
    "customer_spend_30d_v2",
    layer="marts",
    materialized="table",
    owner="support-tools",
    description="Copy of customer_spend_30d for the support agent, built when the sales table was slow.",
    depends_on=["stg_customers", "int_customer_daily_spend"],
    columns={"customer_id": "varchar", "spend_usd": "number(12,2)"},
    sql=_SPEND_30.format(col="spend_usd"),
)
_model(
    "customer_spend_90d",
    layer="marts",
    materialized="table",
    owner="support-tools",
    description="Net settled spend per customer over the previous 90 complete UTC days, including customers with no orders.",
    depends_on=["stg_customers", "int_customer_daily_spend"],
    columns={"customer_id": "varchar", "spend_usd": "number(12,2)"},
    sql=_SPEND_30.replace("-30", "-90").format(col="spend_usd"),
)
_model(
    "customer_order_count_30d",
    layer="marts",
    materialized="table",
    owner="sales-engineering",
    description="Number of orders per customer over the previous 30 complete UTC days.",
    depends_on=["stg_customers", "int_customer_daily_spend"],
    columns={"customer_id": "varchar", "order_count": "integer"},
    sql="""
select c.customer_id, coalesce(sum(d.order_count), 0) as order_count
from {{ ref('stg_customers') }} c
left join {{ ref('int_customer_daily_spend') }} d
  on d.customer_id = c.customer_id and d.order_day >= dateadd(day, -30, current_date()) and d.order_day < current_date()
group by 1
""",
)
_model(
    "customer_ltv",
    layer="marts",
    materialized="table",
    owner="finance",
    description="Lifetime net settled spend and order count per customer.",
    depends_on=["stg_customers", "int_customer_daily_spend"],
    columns={
        "customer_id": "varchar",
        "lifetime_net_usd": "number(12,2)",
        "lifetime_orders": "integer",
    },
    sql="""
select c.customer_id, coalesce(sum(d.net_usd), 0) as lifetime_net_usd, coalesce(sum(d.order_count), 0) as lifetime_orders
from {{ ref('stg_customers') }} c left join {{ ref('int_customer_daily_spend') }} d on d.customer_id = c.customer_id group by 1
""",
)
_model(
    "household_spend_90d",
    layer="marts",
    materialized="table",
    owner="support-tools",
    description="Net spend per household over 90 days, where a household is customers sharing a residence postal code.",
    depends_on=["dim_customers", "int_customer_daily_spend"],
    columns={"residence_postal_code": "varchar", "spend_usd": "number(12,2)", "members": "integer"},
    sql="""
select c.residence_postal_code, coalesce(sum(d.net_usd), 0) as spend_usd, count(distinct c.customer_id) as members
from {{ ref('dim_customers') }} c
left join {{ ref('int_customer_daily_spend') }} d on d.customer_id = c.customer_id and d.order_day >= dateadd(day, -90, current_date()) and d.order_day < current_date()
group by 1
""",
)
_model(
    "product_sales_daily",
    layer="marts",
    materialized="incremental",
    owner="merchandising",
    description="Units and revenue per product per day.",
    depends_on=["fct_order_items"],
    columns={
        "product_id": "varchar",
        "order_day": "date",
        "units": "integer",
        "revenue_usd": "number(12,2)",
    },
    sql="select product_id, cast(convert_timezone('UTC', ordered_at) as date) as order_day, sum(quantity) as units, sum(extended_price) as revenue_usd from {{ ref('fct_order_items') }} group by 1, 2",
)
_model(
    "category_sales_monthly",
    layer="marts",
    materialized="table",
    owner="merchandising",
    description="Revenue per category per calendar month.",
    depends_on=["fct_order_items"],
    columns={"category_name": "varchar", "order_month": "date", "revenue_usd": "number(12,2)"},
    sql="select category_name, date_trunc('month', convert_timezone('UTC', ordered_at))::date as order_month, sum(extended_price) as revenue_usd from {{ ref('fct_order_items') }} group by 1, 2",
)
_model(
    "daily_revenue",
    layer="marts",
    materialized="table",
    owner="finance",
    description="Gross settled payments, refunds, and net revenue per UTC day.",
    depends_on=["fct_payments", "fct_refunds"],
    columns={
        "day": "date",
        "gross_usd": "number(12,2)",
        "refunds_usd": "number(12,2)",
        "net_usd": "number(12,2)",
    },
    sql="""
with p as (select cast(convert_timezone('UTC', paid_at) as date) as day, sum(amount) as gross_usd from {{ ref('fct_payments') }} group by 1),
     r as (select cast(convert_timezone('UTC', refunded_at) as date) as day, sum(amount) as refunds_usd from {{ ref('fct_refunds') }} group by 1)
select p.day, p.gross_usd, coalesce(r.refunds_usd, 0) as refunds_usd, p.gross_usd - coalesce(r.refunds_usd, 0) as net_usd
from p left join r on r.day = p.day
""",
)
_model(
    "campaign_attribution",
    layer="marts",
    materialized="table",
    owner="marketing-analytics",
    description="Orders and revenue attributed to campaigns by last session before the order.",
    depends_on=["fct_sessions", "fct_orders", "stg_marketing_campaigns"],
    columns={
        "campaign_id": "varchar",
        "campaign_name": "varchar",
        "channel": "varchar",
        "orders": "integer",
        "revenue_usd": "number(12,2)",
        "spend_usd": "number(12,2)",
    },
    sql="""
select c.campaign_id, c.name as campaign_name, c.channel, count(distinct o.order_id) as orders, sum(o.net_usd) as revenue_usd, any_value(c.spend_usd) as spend_usd
from {{ ref('stg_marketing_campaigns') }} c
join {{ ref('fct_sessions') }} s on s.utm_campaign = c.name
join {{ ref('fct_orders') }} o on o.customer_id = s.customer_id and o.ordered_at between s.started_at and dateadd(hour, 24, s.started_at)
group by 1, 2, 3
""",
)
_model(
    "inventory_health",
    layer="marts",
    materialized="table",
    owner="merchandising",
    description="Days of cover per product from latest inventory and trailing 30-day units.",
    depends_on=["stg_inventory_snapshots", "product_sales_daily"],
    columns={
        "product_id": "varchar",
        "on_hand": "integer",
        "units_30d": "integer",
        "days_of_cover": "number(12,2)",
    },
    sql="""
with inv as (select product_id, sum(on_hand - reserved) as on_hand from {{ ref('stg_inventory_snapshots') }} where snapshot_date = (select max(snapshot_date) from {{ ref('stg_inventory_snapshots') }}) group by 1),
     u as (select product_id, sum(units) as units_30d from {{ ref('product_sales_daily') }} where order_day >= dateadd(day, -30, current_date()) group by 1)
select inv.product_id, inv.on_hand, coalesce(u.units_30d, 0) as units_30d, inv.on_hand / nullif(u.units_30d / 30.0, 0) as days_of_cover
from inv left join u on u.product_id = inv.product_id
""",
)
_model(
    "cohort_retention",
    layer="marts",
    materialized="table",
    owner="finance",
    description="Share of each first-order-month cohort that ordered again in each later month.",
    depends_on=["dim_customers", "int_customer_daily_spend"],
    columns={
        "cohort_month": "date",
        "months_since": "integer",
        "customers": "integer",
        "retained": "integer",
    },
    sql="""
select date_trunc('month', c.first_order_day)::date as cohort_month,
       datediff('month', c.first_order_day, d.order_day) as months_since,
       count(distinct c.customer_id) as customers, count(distinct d.customer_id) as retained
from {{ ref('dim_customers') }} c left join {{ ref('int_customer_daily_spend') }} d on d.customer_id = c.customer_id
group by 1, 2
""",
)
_model(
    "email_engagement_weekly",
    layer="marts",
    materialized="table",
    owner="marketing-analytics",
    description="Sends, opens, and clicks per campaign per week.",
    depends_on=["stg_email_events", "stg_marketing_campaigns"],
    columns={
        "campaign_id": "varchar",
        "week": "date",
        "sends": "integer",
        "opens": "integer",
        "clicks": "integer",
    },
    sql="""
select e.campaign_id, date_trunc('week', convert_timezone('UTC', e.occurred_at))::date as week,
       count_if(e.event_type = 'send') as sends, count_if(e.event_type = 'open') as opens, count_if(e.event_type = 'click') as clicks
from {{ ref('stg_email_events') }} e group by 1, 2
""",
)

# ---- exposures --------------------------------------------------------------------------------

_EXPOSURES: dict[str, dict[str, Any]] = {
    "sales_app": {
        "type": "application",
        "owner": {"name": "Sales Engineering", "email": "sales-eng@shoply.example"},
        "depends_on": ["customer_spend_30d", "customer_order_count_30d"],
        "description": "The sales application shows each customer's 30-day spend on the account page.",
        "url": "https://sales.shoply.example",
        "maturity": "high",
        "meta": {"sla_minutes": 5},
    },
    "support_agent": {
        "type": "application",
        "owner": {"name": "Support Tools", "email": "support-tools@shoply.example"},
        "depends_on": ["customer_spend_90d", "customer_spend_30d_v2", "household_spend_90d"],
        "description": "An agent that answers support questions about a customer's recent spend.",
        "url": "https://support.shoply.example/agent",
        "maturity": "medium",
        "meta": {"sla_minutes": 15},
    },
    "marketing_dashboard": {
        "type": "dashboard",
        "owner": {"name": "Marketing Analytics", "email": "mkt-analytics@shoply.example"},
        "depends_on": ["cust_spend_last_30d", "campaign_attribution", "email_engagement_weekly"],
        "description": "Weekly marketing performance dashboard.",
        "url": "https://bi.shoply.example/dashboards/marketing",
        "maturity": "medium",
        "meta": {"sla_minutes": 1440},
    },
    "finance_daily_report": {
        "type": "analysis",
        "owner": {"name": "Finance", "email": "finance@shoply.example"},
        "depends_on": ["daily_revenue", "fct_payments", "fct_refunds"],
        "description": "Daily revenue report emailed at 06:00 UTC.",
        "url": "https://bi.shoply.example/reports/daily-revenue",
        "maturity": "high",
        "meta": {"sla_minutes": 60},
    },
    "exec_kpis": {
        "type": "dashboard",
        "owner": {"name": "Finance", "email": "finance@shoply.example"},
        "depends_on": ["customer_ltv", "cohort_retention", "daily_revenue"],
        "description": "Executive KPI dashboard.",
        "url": "https://bi.shoply.example/dashboards/exec",
        "maturity": "high",
        "meta": {"sla_minutes": 1440},
    },
}

# ---- MetricFlow -------------------------------------------------------------------------------

_SEMANTIC_MODELS: dict[str, dict[str, Any]] = {
    "customers": {
        "model": "dim_customers",
        "description": "One row per customer.",
        "defaults": {},
        "entities": [{"name": "customer", "type": "primary", "expr": "customer_id"}],
        "dimensions": [
            {"name": "region", "type": "categorical"},
            {"name": "residence_postal_code", "type": "categorical"},
        ],
        "measures": [],
    },
    "orders": {
        "model": "fct_orders",
        "description": "One row per order, attributed to the customer who owned the account at order time.",
        "defaults": {"agg_time_dimension": "ordered_at"},
        "entities": [
            {"name": "order", "type": "primary", "expr": "order_id"},
            {"name": "customer", "type": "foreign", "expr": "customer_id"},
        ],
        "dimensions": [
            {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "day"}},
            {"name": "status", "type": "categorical"},
        ],
        "measures": [
            {
                "name": "net_order_usd",
                "agg": "sum",
                "expr": "net_usd",
                "description": "Net settled spend in USD after refunds: what the customer actually paid for the order.",
            },
            {
                "name": "order_count",
                "agg": "count",
                "expr": "order_id",
                "description": "Number of orders.",
            },
        ],
    },
    "payments": {
        "model": "fct_payments",
        "description": "Settled payments.",
        "defaults": {"agg_time_dimension": "paid_at"},
        "entities": [
            {"name": "payment", "type": "primary", "expr": "payment_id"},
            {"name": "customer", "type": "foreign", "expr": "customer_id"},
            {"name": "order", "type": "foreign", "expr": "order_id"},
        ],
        "dimensions": [
            {"name": "paid_at", "type": "time", "type_params": {"time_granularity": "day"}},
            {"name": "method", "type": "categorical"},
        ],
        "measures": [
            {
                "name": "gross_payment_usd",
                "agg": "sum",
                "expr": "amount",
                "description": "Settled payment amount in USD.",
            }
        ],
    },
    "refunds": {
        "model": "fct_refunds",
        "description": "Refunds issued.",
        "defaults": {"agg_time_dimension": "refunded_at"},
        "entities": [
            {"name": "refund", "type": "primary", "expr": "refund_id"},
            {"name": "customer", "type": "foreign", "expr": "customer_id"},
        ],
        "dimensions": [
            {"name": "refunded_at", "type": "time", "type_params": {"time_granularity": "day"}}
        ],
        "measures": [
            {
                "name": "refund_usd",
                "agg": "sum",
                "expr": "amount",
                "description": "Refunded amount in USD.",
            }
        ],
    },
}

_METRICS: dict[str, dict[str, Any]] = {
    "revenue": {
        "type": "simple",
        "label": "Net revenue",
        "description": "Net settled order revenue in USD.",
        "type_params": {"measure": {"name": "net_order_usd"}},
    },
    "orders": {
        "type": "simple",
        "label": "Orders",
        "description": "Count of orders.",
        "type_params": {"measure": {"name": "order_count"}},
    },
    "average_order_value": {
        "type": "ratio",
        "label": "Average order value",
        "description": "Revenue over orders.",
        "type_params": {"numerator": {"name": "revenue"}, "denominator": {"name": "orders"}},
    },
    "refund_rate": {
        "type": "ratio",
        "label": "Refund rate",
        "description": "Refunds over gross payments.",
        "type_params": {
            "numerator": {"name": "refunds"},
            "denominator": {"name": "gross_payments"},
        },
    },
    "refunds": {
        "type": "simple",
        "label": "Refunds",
        "description": "Refunded USD.",
        "type_params": {"measure": {"name": "refund_usd"}},
    },
    "gross_payments": {
        "type": "simple",
        "label": "Gross payments",
        "description": "Settled payments USD.",
        "type_params": {"measure": {"name": "gross_payment_usd"}},
    },
}


# ---- builders ---------------------------------------------------------------------------------


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _relation(name: str) -> str:
    layer = _MODELS[name]["layer"]
    return f"{DATABASE}.{layer}.{name}"


def _compile(sql: str) -> str:
    out = sql
    for name in _MODELS:
        out = out.replace(f"{{{{ ref('{name}') }}}}", _relation(name))
    for src in _SOURCES:
        out = out.replace(f"{{{{ source('raw', '{src}') }}}}", f"{RAW_DATABASE}.public.{src}")
    return out


def build_manifest(config: DbtFixtureConfig | None = None) -> dict[str, Any]:
    """Build the manifest as a plain dict in dbt's v12 shape."""
    config = config or DbtFixtureConfig()
    generated = config.generated_at.isoformat()
    sources: dict[str, Any] = {}
    for src, (desc, cols, loader) in _SOURCES.items():
        sources[f"source.{PROJECT}.raw.{src}"] = {
            "unique_id": f"source.{PROJECT}.raw.{src}",
            "resource_type": "source",
            "package_name": PROJECT,
            "path": "models/staging/sources.yml",
            "original_file_path": "models/staging/sources.yml",
            "name": src,
            "source_name": "raw",
            "database": RAW_DATABASE,
            "schema": "public",
            "identifier": src,
            "loader": loader,
            "description": desc,
            "columns": {c: {"name": c, "description": "", "data_type": t} for c, t in cols.items()},
            "meta": {},
            "tags": [],
            "relation_name": f"{RAW_DATABASE}.public.{src}",
            "freshness": {
                "warn_after": {"count": 12, "period": "hour"},
                "error_after": {"count": 24, "period": "hour"},
            },
            "loaded_at_field": "_loaded_at",
        }
    nodes: dict[str, Any] = {}
    for name, m in _MODELS.items():
        depends = [
            f"source.{PROJECT}.raw.{d.removeprefix('source:')}"
            if d.startswith("source:")
            else f"model.{PROJECT}.{d}"
            for d in m["depends_on"]
        ]
        compiled = _compile(m["sql"])
        nodes[f"model.{PROJECT}.{name}"] = {
            "unique_id": f"model.{PROJECT}.{name}",
            "resource_type": "model",
            "package_name": PROJECT,
            "name": name,
            "alias": name,
            "database": DATABASE,
            "schema": m["layer"],
            "path": f"{m['layer']}/{name}.sql",
            "original_file_path": f"models/{m['layer']}/{name}.sql",
            "relation_name": _relation(name),
            "description": m["description"],
            "columns": {
                c: {"name": c, "description": "", "data_type": t} for c, t in m["columns"].items()
            },
            "config": {
                "materialized": m["materialized"],
                "tags": m["tags"],
                "meta": {"owner": m["owner"], **m["meta"]},
                "on_schema_change": "ignore",
                "incremental_strategy": "merge" if m["materialized"] == "incremental" else None,
            },
            "tags": m["tags"],
            "meta": {"owner": m["owner"], **m["meta"]},
            "depends_on": {"nodes": depends, "macros": []},
            "refs": [
                {"name": d, "package": None, "version": None}
                for d in m["depends_on"]
                if not d.startswith("source:")
            ],
            "sources": [
                ["raw", d.removeprefix("source:")]
                for d in m["depends_on"]
                if d.startswith("source:")
            ],
            "raw_code": m["sql"],
            "compiled_code": compiled,
            "compiled": True,
            "language": "sql",
            "checksum": {"name": "sha256", "checksum": _checksum(m["sql"])},
            "created_at": config.generated_at.timestamp(),
            "fqn": [PROJECT, m["layer"], name],
            "unrendered_config": {"materialized": m["materialized"]},
            "access": "protected",
            "group": m["owner"],
        }
    exposures: dict[str, Any] = {}
    for name, e in _EXPOSURES.items():
        exposures[f"exposure.{PROJECT}.{name}"] = {
            "unique_id": f"exposure.{PROJECT}.{name}",
            "resource_type": "exposure",
            "package_name": PROJECT,
            "name": name,
            "label": name.replace("_", " ").title(),
            "type": e["type"],
            "owner": e["owner"],
            "description": e["description"],
            "url": e["url"],
            "maturity": e["maturity"],
            "meta": e["meta"],
            "tags": [],
            "depends_on": {
                "nodes": [f"model.{PROJECT}.{d}" for d in e["depends_on"]],
                "macros": [],
            },
            "refs": [{"name": d, "package": None, "version": None} for d in e["depends_on"]],
            "path": "models/marts/exposures.yml",
            "original_file_path": "models/marts/exposures.yml",
            "fqn": [PROJECT, name],
            "created_at": config.generated_at.timestamp(),
        }
    semantic_models: dict[str, Any] = {}
    for name, sm in _SEMANTIC_MODELS.items():
        semantic_models[f"semantic_model.{PROJECT}.{name}"] = {
            "unique_id": f"semantic_model.{PROJECT}.{name}",
            "resource_type": "semantic_model",
            "package_name": PROJECT,
            "name": name,
            "description": sm["description"],
            "model": f"ref('{sm['model']}')",
            "node_relation": {
                "alias": sm["model"],
                "schema_name": _MODELS[sm["model"]]["layer"],
                "database": DATABASE,
                "relation_name": _relation(sm["model"]),
            },
            "defaults": sm["defaults"],
            "entities": sm["entities"],
            "dimensions": sm["dimensions"],
            "measures": sm["measures"],
            "depends_on": {"nodes": [f"model.{PROJECT}.{sm['model']}"], "macros": []},
            "path": "models/semantic/semantic_models.yml",
            "original_file_path": "models/semantic/semantic_models.yml",
            "fqn": [PROJECT, name],
            "created_at": config.generated_at.timestamp(),
        }
    metrics: dict[str, Any] = {}
    for name, mt in _METRICS.items():
        metrics[f"metric.{PROJECT}.{name}"] = {
            "unique_id": f"metric.{PROJECT}.{name}",
            "resource_type": "metric",
            "package_name": PROJECT,
            "name": name,
            "label": mt["label"],
            "type": mt["type"],
            "description": mt["description"],
            "type_params": mt["type_params"],
            "filter": None,
            "meta": {},
            "tags": [],
            "path": "models/semantic/metrics.yml",
            "original_file_path": "models/semantic/metrics.yml",
            "fqn": [PROJECT, name],
            "created_at": config.generated_at.timestamp(),
        }
    children: dict[str, list[str]] = {k: [] for k in (*sources, *nodes)}
    parents: dict[str, list[str]] = {k: list(v["depends_on"]["nodes"]) for k, v in nodes.items()}
    for node, deps in parents.items():
        for dep in deps:
            children.setdefault(dep, []).append(node)
    for exp_id, exp in exposures.items():
        parents[exp_id] = list(exp["depends_on"]["nodes"])
        for dep in parents[exp_id]:
            children.setdefault(dep, []).append(exp_id)
    return {
        "metadata": {
            "dbt_schema_version": SCHEMA_VERSION,
            "dbt_version": "1.9.4",
            "generated_at": generated,
            "invocation_id": "8d7c0a1e-6b3f-4c2e-9f1a-3e5b7c9d2f40",
            "env": {},
            "project_name": PROJECT,
            "project_id": _checksum(PROJECT)[:32],
            "user_id": None,
            "send_anonymous_usage_stats": False,
            "adapter_type": "snowflake",
        },
        "nodes": nodes,
        "sources": sources,
        "macros": {},
        "docs": {},
        "exposures": exposures,
        "metrics": metrics,
        "semantic_models": semantic_models,
        "saved_queries": {},
        "groups": {
            f"group.{PROJECT}.{g}": {
                "name": g,
                "owner": {"name": g, "email": f"{g}@shoply.example"},
            }
            for g in sorted({m["owner"] for m in _MODELS.values()})
        },
        "selectors": {},
        "disabled": {},
        "parent_map": parents,
        "child_map": children,
        "unit_tests": {},
    }


# ---- query history ----------------------------------------------------------------------------

_TABLE_BYTES = {
    "staging": 2 * 10**8,
    "intermediate": 6 * 10**8,
    "marts": 1 * 10**8,
}


def build_query_history(config: DbtFixtureConfig | None = None) -> list[QueryRecord]:
    """Build the readers and builders a real warehouse would show for this project.

    Readers: the sales application every five minutes, the support agent hourly, the finance
    report daily at 06:00, marketing analysts a few times a day, and one analyst reading an
    internal intermediate table directly. Builders: dbt Cloud rebuilding every table model once a
    day at 04:00.
    """
    config = config or DbtFixtureConfig()
    rng = random.Random(config.seed)
    end = config.generated_at.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=config.history_days)
    rows: list[QueryRecord] = []
    counter = 0

    def add(
        user: str,
        role: str,
        warehouse: str,
        at: datetime,
        text: str,
        relation: str,
        kind: str = "SELECT",
    ) -> None:
        nonlocal counter
        counter += 1
        layer = relation.split(".")[1] if relation.count(".") == 2 else "marts"
        size = _TABLE_BYTES.get(layer, 10**8)
        scanned = (
            int(size * rng.uniform(0.6, 1.4))
            if kind == "SELECT"
            else int(size * rng.uniform(2.0, 4.0))
        )
        rows.append(
            QueryRecord(
                query_id=f"01b{counter:08d}-0000-0000-0000-{rng.randrange(16**12):012x}",
                user_name=user,
                role_name=role,
                warehouse_name=warehouse,
                start_time=at,
                total_elapsed_ms=int(scanned / 5_000_000 * rng.uniform(0.8, 1.5)) + 120,
                bytes_scanned=scanned,
                credits_used_cloud_services=round(scanned / 10**9 * 0.0023, 6),
                query_type="CREATE_TABLE_AS_SELECT" if kind == "CTAS" else "SELECT",
                query_text=text,
            )
        )

    day = start
    while day < end:
        # dbt Cloud builds every table and incremental model at 04:00.
        build_at = day + timedelta(hours=4)
        for name, m in _MODELS.items():
            if m["materialized"] in {"table", "incremental"}:
                build_at += timedelta(seconds=rng.randint(20, 240))
                add(
                    "DBT_CLOUD",
                    "TRANSFORMER",
                    "TRANSFORM_WH",
                    build_at,
                    f"create or replace transient table {_relation(name)} as {_compile(m['sql'])[:160]} ...",
                    _relation(name),
                    kind="CTAS",
                )
        # Sales app: every five minutes, point lookups on customer_spend_30d and order count.
        for minute in range(0, 24 * 60, 5):
            if rng.random() < 0.85:
                cid = f"c{rng.randint(0, 50000):06d}"
                add(
                    "SVC_SALES_APP",
                    "APP_READER",
                    "APP_WH",
                    day + timedelta(minutes=minute),
                    f"select spend_usd from {_relation('customer_spend_30d')} where customer_id = '{cid}'",
                    _relation("customer_spend_30d"),
                )
                if rng.random() < 0.3:
                    add(
                        "SVC_SALES_APP",
                        "APP_READER",
                        "APP_WH",
                        day + timedelta(minutes=minute, seconds=2),
                        f"select order_count from {_relation('customer_order_count_30d')} where customer_id = '{cid}'",
                        _relation("customer_order_count_30d"),
                    )
        # Support agent: hourly, 90-day and the v2 copy, sometimes households.
        for hour in range(24):
            if rng.random() < 0.7:
                cid = f"c{rng.randint(0, 50000):06d}"
                at = day + timedelta(hours=hour, minutes=rng.randint(0, 59))
                add(
                    "SVC_SUPPORT_AGENT",
                    "APP_READER",
                    "APP_WH",
                    at,
                    f"select spend_usd from {_relation('customer_spend_90d')} where customer_id = '{cid}'",
                    _relation("customer_spend_90d"),
                )
                add(
                    "SVC_SUPPORT_AGENT",
                    "APP_READER",
                    "APP_WH",
                    at + timedelta(seconds=1),
                    f"select spend_usd from {_relation('customer_spend_30d_v2')} where customer_id = '{cid}'",
                    _relation("customer_spend_30d_v2"),
                )
                if rng.random() < 0.2:
                    add(
                        "SVC_SUPPORT_AGENT",
                        "APP_READER",
                        "APP_WH",
                        at + timedelta(seconds=3),
                        f"select * from {_relation('household_spend_90d')} where residence_postal_code = '{rng.randint(10000, 99999)}'",
                        _relation("household_spend_90d"),
                    )
        # Finance report: 06:00 sharp, daily.
        at = day + timedelta(hours=6)
        add(
            "SVC_REPORTING",
            "REPORT_READER",
            "BI_WH",
            at,
            f"select * from {_relation('daily_revenue')} where day >= dateadd(day, -35, current_date())",
            _relation("daily_revenue"),
        )
        add(
            "SVC_REPORTING",
            "REPORT_READER",
            "BI_WH",
            at + timedelta(seconds=4),
            f"select method, sum(amount) from {_relation('fct_payments')} where paid_at >= dateadd(day, -1, current_date()) group by 1",
            _relation("fct_payments"),
        )
        # Marketing analysts: ad hoc, business hours, a few a day.
        for _ in range(rng.randint(1, 5)):
            at = day + timedelta(hours=rng.randint(9, 18), minutes=rng.randint(0, 59))
            table = rng.choice(
                [
                    "cust_spend_last_30d",
                    "campaign_attribution",
                    "email_engagement_weekly",
                    "fct_sessions",
                ]
            )
            add(
                rng.choice(["JDOE", "APATEL", "MGARCIA"]),
                "ANALYST",
                "ADHOC_WH",
                at,
                f"select * from {_relation(table)} limit 500",
                _relation(table),
            )
        # Exec dashboard: morning and evening.
        for hour in (8, 17):
            add(
                "SVC_BI",
                "REPORT_READER",
                "BI_WH",
                day + timedelta(hours=hour),
                f"select cohort_month, months_since, retained / customers from {_relation('cohort_retention')}",
                _relation("cohort_retention"),
            )
            add(
                "SVC_BI",
                "REPORT_READER",
                "BI_WH",
                day + timedelta(hours=hour, seconds=2),
                f"select sum(lifetime_net_usd) from {_relation('customer_ltv')}",
                _relation("customer_ltv"),
            )
        # One analyst reads an internal intermediate table directly. That is an unmanaged read.
        if rng.random() < 0.6:
            add(
                "RCHEN",
                "ANALYST",
                "ADHOC_WH",
                day + timedelta(hours=rng.randint(10, 16)),
                f"select customer_id, sum(net_usd) from {_relation('int_customer_daily_spend')} where order_day >= dateadd(day, -30, current_date()) group by 1",
                _relation("int_customer_daily_spend"),
            )
        day += timedelta(days=1)
    rows.sort(key=lambda r: r.start_time)
    return rows


def write_fixture(out_dir: Path, config: DbtFixtureConfig | None = None) -> tuple[Path, Path]:
    """Write manifest.json and query_history.jsonl into `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    history_path = out_dir / "query_history.jsonl"
    manifest_path.write_text(json.dumps(build_manifest(config), indent=2) + "\n")
    with history_path.open("w") as handle:
        for row in build_query_history(config):
            handle.write(row.model_dump_json() + "\n")
    return manifest_path, history_path


__all__ = [
    "DbtFixtureConfig",
    "QueryRecord",
    "build_manifest",
    "build_query_history",
    "write_fixture",
]
