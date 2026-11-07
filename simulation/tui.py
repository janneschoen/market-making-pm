"""
Rich-powered TUI dashboard for the paper-trading simulation engine.

Displays:
  ┌─ Per-market ladder (left column)
  ├─ Strategy parameters & last fills (right column)
  └─ Aggregate dashboard (bottom) — equity curve, Sharpe, fill rate, PnL

Interactive controls: [Q]uit  [S]tart/Stop  [</>]Speed  [C]onfig
"""
import asyncio
import time
import math
from typing import List, Dict, Optional

from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box
from rich.align import Align

from simulation.engine import SimulationEngine, MarketSnap
from simulation.metrics import MetricsEngine
from config import YES, NO


# ── Sparkline helper ────────────────────────────────────────────────────────

def _sparkline(values: List[float], width: int = 40) -> str:
    """Build a unicode sparkline from a list of equity values."""
    if len(values) < 2:
        return "─" * width

    mn = min(values)
    mx = max(values)
    rng = mx - mn if mx != mn else 1.0
    chars = "▁▂▃▄▅▆▇█"
    n = len(chars) - 1

    # Downsample to fit width
    step = max(1, len(values) // width)
    sampled = values[::step][-width:]

    line = ""
    for v in sampled:
        idx = min(n, int((v - mn) / rng * n))
        line += chars[idx]

    return line


# ── Rendering helpers ───────────────────────────────────────────────────────

def _format_hours(h: float) -> str:
    """Nicely format hours-to-resolution."""
    if h >= 24:
        days = h / 24
        return f"{days:.1f}d"
    return f"{h:.1f}h"


def _bar(value: float, max_val: float, width: int = 10,
         filled_char: str = "█", empty_char: str = "░") -> str:
    """Proportional bar, clamped to [0, max_val]."""
    if max_val <= 0:
        return empty_char * width
    ratio = min(1.0, max(0.0, value / max_val))
    n = int(round(ratio * width))
    return filled_char * n + empty_char * (width - n)


def _pnl_color(pnl: float) -> str:
    if pnl > 0:
        return "green"
    elif pnl < 0:
        return "red"
    return "white"


def _sign(pnl: float) -> str:
    if pnl > 0:
        return "+"
    return ""


# ── Per-market ladder panel ─────────────────────────────────────────────────

def render_market_ladder(snaps: List[MarketSnap], max_markets: int = 12) -> Panel:
    """Render the left-column per-market ladder."""
    if not snaps:
        return Panel("No markets active", title="Market Ladder", border_style="dim blue")

    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("market", style="bold cyan")
    table.add_column("bars", justify="left")
    table.add_column("stats", justify="right")

    # Show markets sorted by hours-to-resolution ascending
    sorted_snaps = sorted(snaps, key=lambda s: s.hours_to_resolution)

    for snap in sorted_snaps[:max_markets]:
        # Truncate question
        q = snap.question
        if len(q) > 25:
            q = q[:22] + "..."

        h_str = _format_hours(snap.hours_to_resolution)

        # Volume bars — show relative quote levels
        max_price = 1.0
        y_bar = _bar(snap.yes_quote, max_price)
        n_bar = _bar(snap.no_quote, max_price)

        # Inventory
        inv_color = "green" if abs(snap.net_exposure) < 10 else "yellow"
        inv_str = f"Y={snap.yes_inventory:.0f} N={snap.no_inventory:.0f}"

        # Fills indicator
        fill_str = ""
        if snap.fills_this_cycle:
            fill_str = f" [bold yellow]◆{snap.fills_this_cycle}[/]"

        market_line = f"{q} ({h_str}){fill_str}"
        bars_line = f"[green]{y_bar}[/] Y\n        [red]{n_bar}[/] N"
        stats_line = (f"[dim]bid:[/] {snap.yes_bid:.4f}/{snap.no_bid:.4f}\n"
                      f"[dim]qte:[/] {snap.yes_quote:.4f}/{snap.no_quote:.4f}\n"
                      f"[{inv_color}]{inv_str}[/]")

        table.add_row(market_line, bars_line, stats_line)
        table.add_row("", "", "")  # spacer

    return Panel(table, title="Market Ladder", border_style="bright_blue")


# ── Strategy panel ──────────────────────────────────────────────────────────

def render_strategy_panel(snaps: List[MarketSnap], engine: SimulationEngine) -> Panel:
    """Render strategy parameters and per-market details."""
    if not snaps:
        return Panel("Waiting...", title="Strategy", border_style="dim green")

    table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
    table.add_column("Market", style="cyan", max_width=22)
    table.add_column("Mid", justify="right")
    table.add_column("OwnMid", justify="right")
    table.add_column("Spread", justify="right")
    table.add_column("Exposure", justify="right")

    sorted_snaps = sorted(snaps, key=lambda s: s.hours_to_resolution)

    for snap in sorted_snaps[:12]:
        q = snap.question
        if len(q) > 20:
            q = q[:17] + "..."

        exp_color = "green" if abs(snap.net_exposure) < 10 else "yellow"
        table.add_row(
            q,
            f"{snap.market_mid:.4f}",
            f"{snap.own_mid:.4f}",
            f"{snap.spread:.4f}",
            f"[{exp_color}]{snap.net_exposure:+.0f}[/]",
        )

    return Panel(table, title="Strategy Parameters", border_style="bright_green")


# ── Last fills panel ────────────────────────────────────────────────────────

def render_fills_panel(engine: SimulationEngine, max_rows: int = 8) -> Panel:
    """Show the most recent hypothetical fills."""
    fills = engine.fills[-max_rows:]

    if not fills:
        return Panel("No fills yet", title="Last Fills", border_style="dim yellow")

    lines = []
    for f in reversed(fills):
        ts = time.strftime("%H:%M:%S", time.localtime(f.timestamp))
        q = f.market_question
        if len(q) > 28:
            q = q[:25] + "..."
        side_arrow = "↑" if f.side == "BUY" else "↓"
        side_color = "green" if f.side == "BUY" else "red"
        lines.append(
            f"[dim]{ts}[/] [{side_color}]{side_arrow} {f.side}[/] "
            f"{f.size}x{f.token} @ {f.price:.4f}  [dim]{q}[/]"
        )

    text = Text("\n".join(lines))
    return Panel(text, title="Last Fills (hypothetical)", border_style="bright_yellow")


# ── Aggregate dashboard ─────────────────────────────────────────────────────

def render_dashboard(engine: SimulationEngine, metrics: MetricsEngine) -> Panel:
    """Bottom panel with equity curve, Sharpe, fill rate, PnL."""
    stats = engine.get_stats()

    pnl = stats['pnl']
    pnl_color = _pnl_color(pnl)

    # Sparkline
    eq_vals = [v for _, v in stats['equity_history']]
    spark = _sparkline(eq_vals) if len(eq_vals) > 1 else "─" * 40

    sharpe = metrics.compute_sharpe()
    fill_rate = metrics.compute_fill_rate()
    avg_spread = metrics.avg_spread_captured()

    uptime_h = stats['uptime'] / 3600

    lines = [
        f"  Equity Curve:  [{pnl_color}]{spark}[/]",
        "",
        f"  Sharpe: [bold]{sharpe:.2f}[/]  │  "
        f"Fill Rate: [bold]{fill_rate:.1%}[/]  │  "
        f"Avg Spread Capt: [bold]{avg_spread:.3f}[/]",
        "",
        f"  PnL: [bold {pnl_color}]{_sign(pnl)}${pnl:.2f}[/]  │  "
        f"Markets Active: {stats['markets_active']}  │  "
        f"Uptime: {uptime_h:.1f}h  │  "
        f"Fills: {stats['total_fills']}  │  "
        f"Cycles: {stats['quote_cycles']}",
        "",
        f"  [dim][Q]uit  [S]tart/Stop  [</>]Speed ({engine.speed_multiplier:.1f}x)  [C]onfig[/]",
    ]

    text = Text("\n".join(lines))
    return Panel(text, title="Aggregate Dashboard", border_style="bright_magenta")


# ── Layout builder ──────────────────────────────────────────────────────────

def build_layout(engine: SimulationEngine, metrics: MetricsEngine,
                 snaps: List[MarketSnap]) -> Layout:
    """Assemble the full TUI layout."""
    layout = Layout()

    layout.split(
        Layout(name="top", ratio=3),
        Layout(name="bottom", ratio=2),
    )

    layout["top"].split_row(
        Layout(name="left", ratio=2, minimum_size=40),
        Layout(name="right", ratio=3, minimum_size=40),
    )

    layout["right"].split(
        Layout(name="right_top"),
        Layout(name="right_bottom"),
    )

    layout["left"].update(render_market_ladder(snaps))
    layout["right_top"].update(render_strategy_panel(snaps, engine))
    layout["right_bottom"].update(render_fills_panel(engine))
    layout["bottom"].update(render_dashboard(engine, metrics))

    return layout


# ── TUI runner ──────────────────────────────────────────────────────────────

class SimulationTUI:
    """
    Rich Live TUI that renders the simulation state in real-time.

    Usage:
        tui = SimulationTUI(engine, metrics, markets)
        await tui.run()
    """

    def __init__(self, engine: SimulationEngine, metrics: MetricsEngine, markets: list):
        self.engine = engine
        self.metrics = metrics
        self.markets = markets
        self.console = Console()
        self._keyboard_task: Optional[asyncio.Task] = None

    async def run(self):
        """
        Main TUI loop. Renders the dashboard continuously until
        engine.running is set to False.
        """
        # Start keyboard input handler
        self._keyboard_task = asyncio.create_task(self._read_keys())

        with Live(
            self._render(),
            console=self.console,
            refresh_per_second=4,
            screen=True,
        ) as live:
            while self.engine.running:
                await asyncio.sleep(0.25)
                live.update(self._render())

        if self._keyboard_task:
            self._keyboard_task.cancel()
            try:
                await self._keyboard_task
            except asyncio.CancelledError:
                pass

    def _render(self) -> Layout:
        snaps = self.engine.get_market_snaps(self.markets)
        return build_layout(self.engine, self.metrics, snaps)

    async def _read_keys(self):
        """Read keyboard input in a non-blocking way."""
        import sys
        import tty
        import termios
        import select

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while self.engine.running:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if r:
                    ch = sys.stdin.read(1)
                    if ch:
                        await self._handle_key(ch.lower())
                await asyncio.sleep(0.05)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    async def _handle_key(self, key: str):
        if key == 'q':
            self.engine.running = False
        elif key == 's':
            self.engine.paused = not self.engine.paused
        elif key == '.' or key == '>':
            self.engine.speed_multiplier = min(10.0, self.engine.speed_multiplier + 0.5)
        elif key == ',' or key == '<':
            self.engine.speed_multiplier = max(0.1, self.engine.speed_multiplier - 0.5)
        elif key == 'c':
            # Placeholder — could open a config overlay
            pass


# ── Headless status printer ─────────────────────────────────────────────────

def print_headless_status(engine: SimulationEngine, metrics: MetricsEngine):
    """Print a one-line status for headless mode."""
    stats = engine.get_stats()
    pnl = stats['pnl']
    pnl_s = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    uptime_m = stats['uptime'] / 60
    print(
        f"[{time.strftime('%H:%M:%S')}] "
        f"PnL: {pnl_s}  │  "
        f"Fills: {stats['total_fills']}  │  "
        f"Cycles: {stats['quote_cycles']}  │  "
        f"Fill Rate: {stats['fill_rate']:.1%}  │  "
        f"Uptime: {uptime_m:.0f}m"
    )
