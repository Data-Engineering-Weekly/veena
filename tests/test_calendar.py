from datetime import UTC, datetime

from veena.calendar import last_complete_day, sql_timestamptz, window_bounds


def test_utc_window_ends_at_midnight_of_current_day():
    as_of = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
    start, end = window_bounds(as_of, 30, "UTC")
    assert end == datetime(2026, 10, 1, tzinfo=UTC)
    assert start == datetime(2026, 9, 1, tzinfo=UTC)
    assert last_complete_day(as_of, "UTC").isoformat() == "2026-09-30"


def test_window_moves_with_the_day_boundary():
    before = window_bounds(datetime(2026, 9, 30, 23, 59, tzinfo=UTC), 30, "UTC")
    after = window_bounds(datetime(2026, 10, 1, 0, 0, tzinfo=UTC), 30, "UTC")
    assert after[0] == before[0].replace(day=1, month=9)
    assert after[1] == datetime(2026, 10, 1, tzinfo=UTC)
    assert before[1] == datetime(2026, 9, 30, tzinfo=UTC)


def test_non_utc_zone_shifts_the_boundary():
    as_of = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)  # 2026-09-30 22:00 in New York
    start, end = window_bounds(as_of, 1, "America/New_York")
    assert end == datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
    assert start == datetime(2026, 9, 29, 4, 0, tzinfo=UTC)


def test_sql_literal():
    assert (
        sql_timestamptz(datetime(2026, 10, 1, tzinfo=UTC)) == "TIMESTAMPTZ '2026-10-01 00:00:00+00'"
    )
