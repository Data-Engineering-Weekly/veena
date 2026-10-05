"""Reference SQL for customer spend under the approved definitions, and the naive version.

These queries are the independent basis a reconciliation check compares against. They are
written from the semantic definitions, not from any candidate implementation.
"""

from __future__ import annotations

from datetime import date, timedelta


def approved_spend_sql(window_days: int, last_complete_day: date) -> str:
    """Spend per customer over the trailing complete UTC days, attributing each purchase once.

    Args:
        window_days: Number of complete calendar days in the window.
        last_complete_day: The last day inside the window.

    Returns:
        SQL returning `customer_id, spend_usd` for every customer, zero when none qualifies.
    """
    start, end = _bounds(window_days, last_complete_day)
    return f"""
    WITH qualifying AS (
        SELECT m.customer_id, p.amount_usd
        FROM purchases p
        JOIN identity_mappings m
          ON m.account_id = p.account_id
         AND p.purchased_at >= m.valid_from
         AND (m.valid_to IS NULL OR p.purchased_at < m.valid_to)
        WHERE p.status = 'settled'
          AND p.purchased_at >= TIMESTAMPTZ '{start.isoformat()} 00:00:00+00'
          AND p.purchased_at <  TIMESTAMPTZ '{end.isoformat()} 00:00:00+00'
    )
    SELECT c.customer_id, COALESCE(SUM(q.amount_usd), 0) AS spend_usd
    FROM customers c
    LEFT JOIN qualifying q USING (customer_id)
    GROUP BY c.customer_id
    ORDER BY c.customer_id
    """


def naive_spend_sql(window_days: int, last_complete_day: date) -> str:
    """The same window joined on account id alone, which double counts remapped accounts.

    Args:
        window_days: Number of complete calendar days in the window.
        last_complete_day: The last day inside the window.

    Returns:
        SQL with the same shape as `approved_spend_sql`.
    """
    start, end = _bounds(window_days, last_complete_day)
    return f"""
    WITH qualifying AS (
        SELECT m.customer_id, p.amount_usd
        FROM purchases p
        JOIN identity_mappings m ON m.account_id = p.account_id
        WHERE p.status = 'settled'
          AND p.purchased_at >= TIMESTAMPTZ '{start.isoformat()} 00:00:00+00'
          AND p.purchased_at <  TIMESTAMPTZ '{end.isoformat()} 00:00:00+00'
    )
    SELECT c.customer_id, COALESCE(SUM(q.amount_usd), 0) AS spend_usd
    FROM customers c
    LEFT JOIN qualifying q USING (customer_id)
    GROUP BY c.customer_id
    ORDER BY c.customer_id
    """


def _bounds(window_days: int, last_complete_day: date) -> tuple[date, date]:
    end = last_complete_day + timedelta(days=1)
    return end - timedelta(days=window_days), end
