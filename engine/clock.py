"""
Injectable UTC clock.

Every timestamp in the repo must be UTC and timezone-aware: market
`endDate`s come back as UTC while `datetime.now()` is local, and mixing the
two silently shifts the trading window (ROADMAP §8, §10).

Tests inject a fixed clock instead of sleeping or patching `datetime`:

    with use_clock(lambda: datetime(2026, 6, 12, 3, tzinfo=timezone.utc)):
        assert market_slug("London", 1) == "...-june-13-2026"
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Iterator

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Wall-clock time, always timezone-aware UTC."""
    return datetime.now(timezone.utc)


_clock: Clock = utc_now


def now() -> datetime:
    """Current time from the active clock (UTC, timezone-aware)."""
    value = _clock()
    if value.tzinfo is None:
        # Naive clocks are local in the old code; assume UTC instead of
        # silently comparing against aware `endDate` values.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def set_clock(clock: Clock | None) -> None:
    """Replace the global clock (`None` restores wall-clock UTC)."""
    global _clock
    _clock = clock or utc_now


@contextmanager
def use_clock(clock: Clock) -> Iterator[Clock]:
    """Temporarily install `clock`, restoring the previous one on exit."""
    previous = _clock
    set_clock(clock)
    try:
        yield clock
    finally:
        set_clock(previous)
