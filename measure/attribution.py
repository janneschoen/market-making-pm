"""
Per-trade attribution: where did the PnL come from?

`PnL = Σ (model_p − price − fees)` for taker trades (ROADMAP §3), plus, for
maker fills, the spread captured and the change in inventory value.  Keeping
the components separate is what makes a live result explainable instead of
a single unexplained number.

All functions are pure; the JSONL log (`measure.logs`) is the input source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

CENTS = 100.0


@dataclass(frozen=True)
class Trade:
    """One executed trade, as reconstructed from the event log."""

    condition_id: str
    token: str                 # "YES" / "NO"
    side: str                  # "BUY" / "SELL"
    price: float               # fill price, 0..1
    size: float                # shares
    fees: float = 0.0          # USDC paid in fees (not per share)
    mid_at_fill: Optional[float] = None
    model_p: Optional[float] = None


@dataclass
class Attribution:
    """Decomposed PnL for a set of trades."""

    trades: int = 0
    notional: float = 0.0
    alpha: float = 0.0        # Σ (model_p − price) × size, taker edge
    spread: float = 0.0       # Σ (mid_at_fill − price) × size, maker edge
    fees: float = 0.0
    gross: float = 0.0
    net: float = 0.0
    edge_cents: List[float] = field(default_factory=list)

    @property
    def avg_edge_cents(self) -> float:
        if not self.edge_cents:
            return 0.0
        return sum(self.edge_cents) / len(self.edge_cents)


def per_trade_edge(model_p: float, price: float, fees: float = 0.0,
                   size: float = 1.0) -> float:
    """
    Expected edge on a trade in USDC.

    `model_p` is our calibrated probability for the token we buy; `price` is
    what we pay.  Fees are an absolute USDC amount (default 0, matching
    Polymarket's current maker/taker schedule — verify per ROADMAP §11).
    """
    return (model_p - price) * size - fees


def realized_spread(fill_price: float, mid_at_fill: float, side: str) -> float:
    """
    Spread captured by a maker fill, per share (positive = good).

    BUY below mid and SELL above mid both earn the half-spread.
    """
    sign = 1.0 if side.upper() == "BUY" else -1.0
    return (mid_at_fill - fill_price) * sign


def alpha_pnl(trade: Trade) -> float:
    """Model edge component of a taker trade (0 when `model_p` unknown)."""
    if trade.model_p is None:
        return 0.0
    return (trade.model_p - trade.price) * trade.size


def spread_pnl(trade: Trade) -> float:
    """Spread-capture component of a maker trade (0 when mid unknown)."""
    if trade.mid_at_fill is None:
        return 0.0
    return realized_spread(trade.price, trade.mid_at_fill, trade.side) * trade.size


def attribute(trades: Sequence[Trade]) -> Attribution:
    """Decompose a set of trades into alpha, spread, fees and net PnL."""
    result = Attribution()
    for trade in trades:
        result.trades += 1
        result.notional += abs(trade.price * trade.size)
        result.alpha += alpha_pnl(trade)
        result.spread += spread_pnl(trade)
        result.fees += trade.fees
        if trade.model_p is not None:
            result.edge_cents.append(
                (trade.model_p - trade.price) * CENTS
            )

    result.gross = result.alpha + result.spread
    result.net = result.gross - result.fees
    return result


def from_events(events: Iterable[dict]) -> List[Trade]:
    """Reconstruct trades from `fill` records in the JSONL event log."""
    trades: List[Trade] = []
    for event in events:
        if event.get("event") != "fill":
            continue
        try:
            trades.append(Trade(
                condition_id=str(event.get("condition_id", "")),
                token=str(event.get("token", "")),
                side=str(event.get("side", "")),
                price=float(event["price"]),
                size=float(event["size"]),
                fees=float(event.get("fees", 0.0) or 0.0),
                mid_at_fill=_optional_float(event.get("mid_at_fill")),
                model_p=_optional_float(event.get("model_p")),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return trades


def _optional_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
