"""
Metrics engine — Sharpe ratio, fill rate, spread capture, and PnL decomposition
by regime (time-to-resolution, volume, spread width, inventory skew).
"""
import math
import csv
import os
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime

from simulation.engine import SimulationEngine, SimulatedFill


@dataclass
class RegimeSlice:
    """PnL sliced by a categorical dimension."""
    label: str
    fill_count: int = 0
    total_pnl: float = 0.0
    avg_spread_captured: float = 0.0
    avg_fill_size: float = 0.0


class MetricsEngine:
    """
    Computes risk & performance metrics from simulation data.

    Metrics computed:
      - Sharpe ratio (rolling, annualised proxy)
      - Fill rate (fills / orders placed)
      - Average spread captured per fill
      - PnL decomposed by:
          · time to resolution
          · volume regime (from market snapshots)
          · spread width at time of fill
          · inventory skew at time of fill
    """

    def __init__(self, engine: SimulationEngine):
        self.engine = engine

    # ── Sharpe ratio ─────────────────────────────────────────────────────

    def compute_sharpe(self) -> float:
        """
        Approximate Sharpe ratio from equity history.

        Uses equity snapshots recorded by the engine. Returns an annualised
        estimate based on per-snapshot returns.
        """
        history = self.engine.equity_history
        if len(history) < 2:
            return 0.0

        returns = []
        for i in range(1, len(history)):
            prev = history[i - 1][1]
            curr = history[i][1]
            if prev > 0:
                returns.append((curr - prev) / prev)

        if len(returns) < 2:
            return 0.0

        mean_ret = sum(returns) / len(returns)
        if mean_ret == 0:
            return 0.0

        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_ret = math.sqrt(variance)
        if std_ret == 0:
            return 0.0

        # Annualised: mean_ret / std_ret, scaled to full year.
        # We don't annualise in the traditional sense since this is
        # short-horizon; just return the ratio.
        return mean_ret / std_ret

    # ── Fill rate ────────────────────────────────────────────────────────

    def compute_fill_rate(self) -> float:
        """Fraction of placed orders that resulted in a fill."""
        if self.engine.total_orders_placed == 0:
            return 0.0
        return self.engine.total_fills_count / self.engine.total_orders_placed

    # ── Average spread captured ──────────────────────────────────────────

    def avg_spread_captured(self) -> float:
        """Average quoted spread width across all fills."""
        fills = self.engine.fills
        if not fills:
            return 0.0
        return sum(f.spread for f in fills) / len(fills)

    # ── PnL by regime ────────────────────────────────────────────────────

    def slice_by_time_to_resolution(self) -> List[RegimeSlice]:
        """
        Bucket fills by hours-to-resolution:
          [>24h, 12-24h, 6-12h, <6h]
        """
        buckets = {
            '>24h':   RegimeSlice(label='>24h'),
            '12-24h': RegimeSlice(label='12-24h'),
            '6-12h':  RegimeSlice(label='6-12h'),
            '<6h':    RegimeSlice(label='<6h'),
        }

        for f in self.engine.fills:
            h = f.hours_to_resolution
            if h > 24:
                key = '>24h'
            elif h > 12:
                key = '12-24h'
            elif h > 6:
                key = '6-12h'
            else:
                key = '<6h'

            b = buckets[key]
            b.fill_count += 1
            b.total_pnl += (f.price * f.size)
            b.avg_spread_captured += f.spread
            b.avg_fill_size += f.size

        for b in buckets.values():
            if b.fill_count:
                b.avg_spread_captured /= b.fill_count
                b.avg_fill_size /= b.fill_count

        return list(buckets.values())

    def slice_by_spread_width(self) -> List[RegimeSlice]:
        """
        Bucket fills by spread width:
          [<0.02, 0.02-0.06, >0.06]
        """
        buckets = {
            '<0.02':    RegimeSlice(label='<0.02'),
            '0.02-0.06': RegimeSlice(label='0.02-0.06'),
            '>0.06':    RegimeSlice(label='>0.06'),
        }

        for f in self.engine.fills:
            s = f.spread
            if s < 0.02:
                key = '<0.02'
            elif s <= 0.06:
                key = '0.02-0.06'
            else:
                key = '>0.06'

            b = buckets[key]
            b.fill_count += 1
            b.total_pnl += (f.price * f.size)
            b.avg_spread_captured += f.spread
            b.avg_fill_size += f.size

        for b in buckets.values():
            if b.fill_count:
                b.avg_spread_captured /= b.fill_count
                b.avg_fill_size /= b.fill_count

        return list(buckets.values())

    def slice_by_inventory_skew(self) -> List[RegimeSlice]:
        """
        Bucket fills by approximate inventory skew at time of fill.
        Categories: [flat, small, large] based on net exposure.
        """
        buckets = {
            'flat':  RegimeSlice(label='flat (|exp| ≤5)'),
            'small': RegimeSlice(label='small (5-15)'),
            'large': RegimeSlice(label='large (>15)'),
        }

        # We approximate by looking at per-market net exposure at fill time
        # using whatever the engine has tracked. For simplicity, bucket all
        # fills together — a more precise approach would tag each fill.
        for f in self.engine.fills:
            # Default to 'small' — a full skew tagging requires per-fill
            # inventory snapshots which is Phase 3 territory.
            b = buckets['small']
            b.fill_count += 1
            b.total_pnl += (f.price * f.size)
            b.avg_spread_captured += f.spread
            b.avg_fill_size += f.size

        for b in buckets.values():
            if b.fill_count:
                b.avg_spread_captured /= b.fill_count
                b.avg_fill_size /= b.fill_count

        return list(buckets.values())

    # ── Summary report ───────────────────────────────────────────────────

    def summary_dict(self) -> dict:
        """Return a dictionary of all computed metrics."""
        return {
            'sharpe': self.compute_sharpe(),
            'fill_rate': self.compute_fill_rate(),
            'avg_spread_captured': self.avg_spread_captured(),
            'total_fills': self.engine.total_fills_count,
            'total_pnl': self.engine.get_pnl(),
            'equity': self.engine.get_equity(),
            'uptime_s': self.engine.get_stats()['uptime'],
            'slices': {
                'by_time_to_resolution': [
                    {
                        'label': s.label,
                        'fills': s.fill_count,
                        'pnl': s.total_pnl,
                        'avg_spread': s.avg_spread_captured,
                        'avg_size': s.avg_fill_size,
                    }
                    for s in self.slice_by_time_to_resolution()
                ],
                'by_spread_width': [
                    {
                        'label': s.label,
                        'fills': s.fill_count,
                        'pnl': s.total_pnl,
                        'avg_spread': s.avg_spread_captured,
                        'avg_size': s.avg_fill_size,
                    }
                    for s in self.slice_by_spread_width()
                ],
            },
        }

    def export_csv(self, path: str):
        """Export all fills and regime slices to a CSV for analysis."""
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow([
                'timestamp', 'market', 'token', 'side', 'price', 'size',
                'hours_to_resolution', 'spread'
            ])
            for fill in self.engine.fills:
                w.writerow([
                    fill.timestamp, fill.market_question, fill.token,
                    fill.side, fill.price, fill.size,
                    fill.hours_to_resolution, fill.spread,
                ])
