"""
Adverse-selection mark-outs.

Mark-out = mid at fill + horizon − fill price, signed so that positive means
we are up.  Persistently negative mark-outs mean we are being picked off:
the counterparty who filled us knew more than the model (ROADMAP §7).

The data source (fills + later mids) only exists once paper/live trading is
running; this module is the pure arithmetic that consumes it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional, Sequence

CENTS = 100.0
DEFAULT_HORIZONS = ("1m", "5m", "1h")


@dataclass(frozen=True)
class Markout:
    horizon: str
    mid: float
    cents: float          # signed: + = favourable, − = picked off


def markout_cents(fill_price: float, later_mid: float, side: str) -> float:
    """
    Signed mark-out in cents per share.

    BUY  → `mid − fill` (we want the mid to rise after buying)
    SELL → `fill − mid` (we want the mid to fall after selling)
    """
    sign = 1.0 if side.upper() == "BUY" else -1.0
    return (later_mid - fill_price) * sign * CENTS


def markout_series(fill_price: float, side: str,
                   mids: Mapping[str, float],
                   horizons: Sequence[str] = DEFAULT_HORIZONS) -> List[Markout]:
    """Mark-outs for the horizons present in `mids` (e.g. {"1m": 0.52})."""
    series: List[Markout] = []
    for horizon in horizons:
        mid = mids.get(horizon)
        if mid is None:
            continue
        series.append(Markout(
            horizon=horizon,
            mid=float(mid),
            cents=markout_cents(fill_price, float(mid), side),
        ))
    return series


def average_markout(series: Iterable[Markout], horizon: str) -> Optional[float]:
    """Mean mark-out in cents at one horizon, or None if absent."""
    values = [m.cents for m in series if m.horizon == horizon]
    if not values:
        return None
    return sum(values) / len(values)
