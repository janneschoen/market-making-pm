"""
Risk limits: notional, per-market position, daily loss, kill switch.

All limits default to `None` (disabled), so Phase 0 restructure is
behaviour-neutral until limits are configured.  The pre-live gate in
ROADMAP §9/Phase 3 requires them to be set before any real capital.

The manager is deliberately synchronous and dependency-free so it can be
unit-tested and called from either the live or paper path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from engine.clock import now as clock_now


@dataclass(frozen=True)
class RiskLimits:
    """Hard limits. `None` disables the corresponding check."""

    max_notional: Optional[float] = None          # per UTC day, USDC
    max_position_per_market: Optional[float] = None  # shares, absolute
    max_daily_loss: Optional[float] = None        # USDC, positive number

    @property
    def enabled(self) -> bool:
        return any(v is not None for v in
                   (self.max_notional, self.max_position_per_market,
                    self.max_daily_loss))


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""


ALLOW = RiskDecision(True)


class RiskManager:
    """
    Tracks intraday notional/PnL and gates every order.

    Usage per quote tick:

        decision = risk.check_order(price=q, size=s, position=abs(exposure))
        if not decision.allowed:
            ...skip and log...

    `record_fill` must be called for every executed order; `record_pnl`
    should be fed the account's realised+unrealised PnL at least once per
    tick (the kill switch latches once the daily loss limit is breached).
    """

    def __init__(self, limits: Optional[RiskLimits] = None,
                 day: Optional[date] = None):
        self.limits = limits or RiskLimits()
        self.day = day or clock_now().date()
        self.notional_today = 0.0
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.killed = False
        self.halt_reason = ""

    # ── State ────────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self.limits.enabled

    @property
    def daily_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    def kill(self, reason: str) -> None:
        """Latch the kill switch; only an explicit `resume` clears it."""
        if not self.killed:
            self.killed = True
            self.halt_reason = reason

    def resume(self) -> None:
        self.killed = False
        self.halt_reason = ""

    def roll_day(self, day: Optional[date] = None) -> None:
        """Reset per-day counters when the UTC date changes."""
        today = day or clock_now().date()
        if today != self.day:
            self.day = today
            self.notional_today = 0.0
            self.realized_pnl = 0.0

    # ── Updates ──────────────────────────────────────────────────────────

    def record_fill(self, price: float, size: float,
                    current_time: Optional[datetime] = None) -> None:
        self.roll_day((current_time or clock_now()).date())
        self.notional_today += abs(float(price) * float(size))

    def record_pnl(self, realized: float, unrealized: float = 0.0) -> None:
        self.realized_pnl = float(realized)
        self.unrealized_pnl = float(unrealized)
        self._check_daily_loss()

    # ── Checks ───────────────────────────────────────────────────────────

    def check_order(self, *, price: float, size: float,
                    position: float = 0.0) -> RiskDecision:
        """Gate a single order against the configured limits."""
        if not self.enabled:
            return ALLOW
        if self.killed:
            return RiskDecision(False, f"kill switch: {self.halt_reason}")

        limits = self.limits
        size = abs(float(size))
        position = abs(float(position))

        if (limits.max_position_per_market is not None
                and position + size > limits.max_position_per_market):
            return RiskDecision(
                False,
                f"position {position + size:.2f} > max "
                f"{limits.max_position_per_market:.2f}",
            )

        if limits.max_notional is not None:
            order_notional = abs(float(price) * size)
            if self.notional_today + order_notional > limits.max_notional:
                return RiskDecision(
                    False,
                    f"notional {self.notional_today + order_notional:.2f} > max "
                    f"{limits.max_notional:.2f}",
                )

        return ALLOW

    # ── Internals ────────────────────────────────────────────────────────

    def _check_daily_loss(self) -> None:
        limit = self.limits.max_daily_loss
        if limit is not None and self.daily_pnl <= -abs(limit):
            self.kill(f"daily loss {self.daily_pnl:.2f} <= -{abs(limit):.2f}")
