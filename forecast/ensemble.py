"""
Point-in-time ensemble cycle selection.

GEFS runs at 00/06/12/18Z and publishes roughly 3.5–4 h later.  The old
`pm-weather` code did:

    modelInit = datetime.now().replace(hour=0, ...)      # ← lookahead

which asks for a cycle that may not exist yet and, in a historical test,
selects data that was not available at the decision time.  Every historical
result built on it is contaminated.

The rule here is one line: **use the latest cycle whose publication time is
at or before the decision timestamp**, and record which cycle that was.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence, Tuple

GEFS_CYCLES_HOURS: Tuple[int, ...] = (0, 6, 12, 18)
DEFAULT_PUBLICATION_LAG = timedelta(hours=4)
LOOKBACK_DAYS = 2


class LookaheadError(ValueError):
    """A cycle was used before its data could have been available."""


def as_utc(moment: datetime) -> datetime:
    """Coerce to timezone-aware UTC (naive input is assumed UTC)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def cycle_datetime(day, hour: int) -> datetime:
    """UTC datetime of a model cycle init."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def publication_time(cycle: datetime,
                     lag: timedelta = DEFAULT_PUBLICATION_LAG) -> datetime:
    """When the cycle's data becomes available to a decision maker."""
    return as_utc(cycle) + lag


def is_available(cycle: datetime, as_of: datetime,
                 lag: timedelta = DEFAULT_PUBLICATION_LAG) -> bool:
    """Was `cycle` published by `as_of`?"""
    return publication_time(cycle, lag) <= as_utc(as_of)


def latest_available_cycle(
    as_of: datetime,
    cycles: Sequence[int] = GEFS_CYCLES_HOURS,
    lag: timedelta = DEFAULT_PUBLICATION_LAG,
    lookback_days: int = LOOKBACK_DAYS,
) -> datetime:
    """
    Latest cycle init whose data was available at `as_of` (UTC).

    Looks back `lookback_days` days, so a decision shortly after midnight
    still finds the previous day's 18Z cycle.
    """
    reference = as_utc(as_of)
    best: Optional[datetime] = None

    for offset in range(lookback_days + 1):
        day = (reference - timedelta(days=lookback_days)).date() \
            + timedelta(days=offset)
        for hour in cycles:
            candidate = cycle_datetime(day, hour)
            if publication_time(candidate, lag) > reference:
                continue
            if best is None or candidate > best:
                best = candidate

    if best is None:
        raise LookaheadError(
            f"no cycle published by {reference.isoformat()} within "
            f"{lookback_days} days"
        )
    return best


def assert_point_in_time(cycle: datetime, as_of: datetime,
                         lag: timedelta = DEFAULT_PUBLICATION_LAG) -> None:
    """Guard for historical reconstruction: raise if `cycle` was not published."""
    if not is_available(cycle, as_of, lag):
        raise LookaheadError(
            f"cycle {as_utc(cycle).isoformat()} publishes at "
            f"{publication_time(cycle, lag).isoformat()}, after decision time "
            f"{as_utc(as_of).isoformat()}"
        )


def describe_cycle(as_of: datetime,
                   lag: timedelta = DEFAULT_PUBLICATION_LAG) -> dict:
    """Point-in-time record to store next to every probability (ROADMAP §5)."""
    cycle = latest_available_cycle(as_of, lag=lag)
    return {
        "as_of": as_utc(as_of).isoformat(),
        "cycle_init": as_utc(cycle).isoformat(),
        "published_at": publication_time(cycle, lag).isoformat(),
        "publication_lag_hours": lag.total_seconds() / 3600.0,
    }
