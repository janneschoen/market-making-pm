"""Point-in-time ensemble cycle selection — the no-lookahead test."""
from datetime import datetime, timedelta, timezone

import pytest

from forecast.ensemble import (
    DEFAULT_PUBLICATION_LAG,
    GEFS_CYCLES_HOURS,
    LookaheadError,
    assert_point_in_time,
    describe_cycle,
    is_available,
    latest_available_cycle,
    publication_time,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def next_cycle(cycle: datetime) -> datetime:
    """The GEFS cycle immediately after `cycle`."""
    for day_offset in (0, 1):
        for hour in GEFS_CYCLES_HOURS:
            candidate = cycle.replace(
                hour=hour, minute=0, second=0, microsecond=0
            ) + timedelta(days=day_offset)
            if candidate > cycle:
                return candidate
    raise AssertionError("unreachable")


def test_before_first_publication_uses_previous_evening_cycle():
    # 03:00Z on the 12th: 00Z is not yet published (04:00Z), 18Z on the 11th is.
    assert latest_available_cycle(utc(2026, 6, 12, 3)) == utc(2026, 6, 11, 18)


def test_uses_new_cycle_exactly_at_publication():
    assert latest_available_cycle(utc(2026, 6, 12, 4)) == utc(2026, 6, 12, 0)
    assert latest_available_cycle(utc(2026, 6, 12, 3, 59)) == utc(2026, 6, 11, 18)


def test_never_selects_an_unpublished_cycle():
    lag = DEFAULT_PUBLICATION_LAG
    start = utc(2026, 1, 1)
    for hour in range(0, 24 * 4):
        as_of = start + timedelta(hours=hour)
        cycle = latest_available_cycle(as_of)
        assert publication_time(cycle, lag) <= as_of
        # ...and it is the *latest* such cycle: the next one is not out yet.
        assert publication_time(next_cycle(cycle), lag) > as_of


def test_month_and_year_boundaries():
    assert latest_available_cycle(utc(2026, 3, 1, 2)) == utc(2026, 2, 28, 18)
    assert latest_available_cycle(utc(2027, 1, 1, 2)) == utc(2026, 12, 31, 18)


def test_naive_timestamp_is_treated_as_utc():
    naive = datetime(2026, 6, 12, 3)
    assert latest_available_cycle(naive) == utc(2026, 6, 11, 18)


def test_assert_point_in_time_rejects_lookahead():
    as_of = utc(2026, 6, 12, 3)
    assert_point_in_time(utc(2026, 6, 11, 18), as_of)   # fine
    with pytest.raises(LookaheadError):
        assert_point_in_time(utc(2026, 6, 12, 0), as_of)


def test_is_available_matches_publication_time():
    cycle = utc(2026, 6, 12, 0)
    assert not is_available(cycle, utc(2026, 6, 12, 3, 59))
    assert is_available(cycle, utc(2026, 6, 12, 4))


def test_describe_cycle_records_the_cycle_used():
    record = describe_cycle(utc(2026, 6, 12, 3))
    assert record["cycle_init"] == "2026-06-11T18:00:00+00:00"
    assert record["published_at"] == "2026-06-11T22:00:00+00:00"
    assert record["as_of"] == "2026-06-12T03:00:00+00:00"
