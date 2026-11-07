# PLANNING — Market-Making Research Platform

This document captures the design decisions and next steps discussed for
the Polymarket market-making project. The core pivot: **transform from a
live trading bot into a market-making research platform with a simulation
TUI and paper-trading engine.**

---

## Why Not Live Trade with $100 Right Now?

Three reasons:

| Problem | Detail |
|---|---|
| **Statistical insignificance** | One week across ~10 markets = ~200 quote cycles. A positive week could be noise; a negative week could be bad luck — not strategy failure. Recruiter doesn't know which. |
| **Splitting/merging not implemented** | When both YES and NO quotes fill, you hold paired tokens. Without redeeming them for USDC, capital gets locked. With $100 across 10 markets at $20 quote size, you run dry after ~2-3 market fills. The bot stops quoting due to capital constraint, not strategy. |
| **Single-config, no insight** | Only one spread/skew/refresh config. No way to slice PnL by time-to-resolution, volume regime, or spread width. No visual feedback during execution. |

**Conclusion:** A paper-trading TUI that runs the full strategy against live
market data without sending real orders is more valuable — both as a
learning tool and as a portfolio piece.

---

## Target End State: Live Simulation TUI

```
┌──────────────────────────────────────────────────────────────┐
│  Market-Making Simulation Engine (Paper-Trading)             │
├───────────────────────┬──────────────────────────────────────┤
│                       │                                      │
│  Per-Market Ladder    │  Strategy Parameters (per market)    │
│                       │                                      │
│  Berlin (+4.2h)       │  Market Mid:  0.52                  │
│  YES bid: 0.54        │  Spread:      0.04                  │
│  NO  bid: 0.44        │  Skew Intens: 0.01                  │
│  ████████░░ Y vol     │  Inventory:   Y=12  N=8  (+4 net)   │
│  ░░░░██████ N vol     │  ownMid:      0.46 (market: 0.50)   │
│                       │  Quote YES:   0.44                   │
│  London (+6.1h)       │  Quote NO:    0.56                   │
│  YES bid: 0.48        │                                      │
│  NO  bid: 0.49        │  Last Fills (hypothetical):         │
│  ███░░░░░░░ Y vol     │  ↑ Bought  20Y @ 0.44 (+$0.12)     │
│  ░░░░░░████ N vol     │  ↓ Bought  20N @ 0.56 (+$0.12)     │
│                       │  ↑ Bought  20Y @ 0.43 (+$0.14)     │
│  ...                  │                                      │
├───────────────────────┴──────────────────────────────────────┤
│  Aggregate Dashboard                                         │
│  ┌──────────────────────────────────────────────────────────┐│
│  │  Equity Curve (paper-PnL, rolling)                      ││
│  │  ···················↗·········↘↘········↗····          ││
│  └──────────────────────────────────────────────────────────┘│
│  Sharpe: 1.4  |  Fill Rate: 62%  |  Avg Spread Capt: 2.8¢  │
│  PnL: +$6.20  |  Markets Active: 8  |  Uptime: 3d 14h      │
│  [Q]uit  [S]tart/Stop  [</>]Speed  [C]onfig                │
└──────────────────────────────────────────────────────────────┘
```

### Modes

1. **Interactive TUI (local terminal)** — FTXUI-powered real-time dashboard
   matching the style of the `limit-order-book` project. Good for demos
   and debugging.

2. **Headless paper-trader (server)** — Long-running data collection. Logs
   every quote cycle, hypothetical fill, and aggregated metrics to disk.
   Feeds into analysis scripts later.

---

## Architecture Changes

The current architecture is already well-factored. The simulation engine
slots in between `quoting_cycle.py` and `market_action.py` with minimal
changes:

```
Current:
  quoting_cycle ──place_order()──▶ market_action ──▶ CLOB API (real)
  quoting_cycle ◀─get_bids()───── market_info    ◀── CLOB API (real)

After:
  quoting_cycle ──place_order()──▶ simulation_engine  ──▶ (no-op)
  quoting_cycle ◀─get_bids()───── market_info         ◀── CLOB API (real)

  simulation_engine:
    - Tracks hypothetical order book
    - Matches quotes against live market bids
    - Records fills, computes PnL, updates inventory
    - Feeds state back to TUI renderer
```

Key insight: **`market_info.py` still fetches live bids from Polymarket.**
Only order *placement* is intercepted. This means the simulation runs
against real market conditions in real time — the strategy sees the actual
bid/ask landscape and reacts to it. We just don't send the orders.

### New Files

```
market-making-pm/
├── simulation/
│   ├── engine.py       # Hypothetical order book, matching, PnL tracking
│   ├── tui.py           # FTXUI-inspired TUI (or Rich Live for simplicity)
│   └── metrics.py       # Sharpe, fill rate, spread capture, PnL decomposition
├── main.py              # Updated: --mode tui | headless | live
├── (existing files...)  # Unchanged or lightly modified
└── PLANNING.md          # This file
```

### simulation/engine.py

```python
class SimulationEngine:
    """
    Tracks hypothetical positions and fills without sending orders.

    For each market:
      - Records the two quotes placed (YES bid, NO bid)
      - Checks: did either quote cross the live best bid?
          YES quote fills when: quote_price >= live_yes_bid
          NO  quote fills when: quote_price >= live_no_bid
      - Updates paper inventory and paper PnL
      - Tracks: total fill events, spread captured per fill, position sizing
    """

    def place_order(self, run, market, token, price, size, side, is_FOK):
        """Intercepts place_order() calls. Records the quote intent.
        Returns a simulated order ID so the caller can 'cancel' it later.
        """
        ...

    def get_current_bids(self, token_pair):
        """Returns live market bids via market_info.py — unchanged."""
        ...

    def get_inventory(self, market):
        """Returns paper inventory (not real wallet balance)."""
        ...
```

### Fill Simulation Logic

The minimum viable fill model is simple but sufficient:

```python
def check_fills(self, market, quotes_placed, live_bids):
    """
    A quote fills if the quoted price meets or beats the best market bid.

    YES quote at 0.44  +  live YES bid = 0.45  →  NO FILL (bid too high)
    YES quote at 0.44  +  live YES bid = 0.43  →  FILL (taker got 0.44, I bought at 0.44)
    YES quote at 0.44  +  live YES bid = 0.44  →  FILL (at the wall)

    NO quote at 0.56  +  live NO bid = 0.55  →  NO FILL
    NO quote at 0.56  +  live NO bid = 0.57  →  FILL
    """
```

This gives you a lower-bound fill rate (real fills would be slightly
higher because your quote could also get taken by a market order between
poll cycles). That's conservative and defensible.

### PnL Decomposition

The metrics module should slice results by regime so you can answer
interesting questions:

| Dimension | Buckets | Question Answered |
|---|---|---|
| Time to resolution | [>24h, 12-24h, 6-12h, <6h] | Does the strategy perform better close to resolution? |
| Volume regime | High/Med/Low tercile | Are high-volume markets more profitable? |
| Spread width | [<0.02, 0.02-0.06, >0.06] | Does wider spread = more PnL or fewer fills? |
| Inventory skew | [flat, small, large] | Does the skew parameter actually reduce exposure? |

Output: a CSV that can be loaded into a pandas notebook for analysis,
plus summary stats printed in the TUI.

---

## TUI Implementation Options

### Option A: FTXUI (C++) — High Effort, High Polish

Same tech as the LOB project. Pipe data from Python to C++ via a temp file
or FIFO (matching the strat-optimizer IPC pattern). Gives you a
professional-looking terminal UI but adds a compile step and IPC
complexity.

### Option B: Rich Live (Python) — Low Effort, Good Enough

Use `rich.live.Live` to render a continuously updating dashboard. Same
Python process, no IPC, trivial to add metrics panels. Less visually
impressive than FTXUI but gets you to a working simulation in a day.

**Recommendation: Start with Rich Live.** Ship the simulation engine first,
then upgrade the TUI to FTXUI if you want the extra polish. The engine
is the hard part; the renderer is swappable.

---

## Implementation Order

### Phase 1: Simulation Engine (2-3 sessions)

- [ ] Create `simulation/engine.py` — intercept order placement, track paper
  positions, compute fills against live bids
- [ ] Create `simulation/metrics.py` — PnL tracking, Sharpe calculation,
  fill rate, spread capture
- [ ] Wire into `quoting_cycle.py` with a `--mode paper` flag
- [ ] Run headless for 1-2 days, dump metrics to JSON/CSV
- [ ] Verify: does the paper PnL make sense? Are fill rates reasonable?

### Phase 2: TUI Dashboard (2-3 sessions)

- [ ] Build a Rich-based live dashboard showing per-market state and
  aggregate metrics
- [ ] Interactive controls: start/stop individual markets, adjust spread
  or skew on the fly
- [ ] Equity curve visualization (inline sparkline or separate panel)

### Phase 3: Analysis (1-2 sessions)

- [ ] Run headless for 7-14 days across 30+ markets
- [ ] Slice PnL by resolution time, volume, spread regime
- [ ] Identify: which config parameters maximize risk-adjusted return?
- [ ] Write up findings in README — these numbers are the portfolio piece

### Phase 4 (Optional): Back to Live

- [ ] Implement splitting/merging (redeem YES+NO for USDC)
- [ ] Deploy with at least $500, monitor with the same TUI
- [ ] Compare simulation predictions vs. actual live PnL

---

## Config Changes for Simulation Mode

Add simulation-specific parameters to `config.json`:

```json
{
  "simulation": {
    "enabled": true,
    "metrics_interval_s": 300,
    "log_path": "logs/simulation.csv",
    "equity_snapshot_interval_s": 60
  }
}
```

When `simulation.enabled` is true, `market_action.place_order()` is
replaced by `simulation.engine.place_order()`. The rest of the code is
unchanged.

---

## Notes

- The strategy itself (Avellaneda-Stoikov inventory skew, two-sided quoting
  via YES+NO BUY orders) doesn't change. We're changing only the execution
  layer.
- The real-time bid fetching from `market_info.py` stays live. The simulation
  exercises the strategy against actual market conditions.
- If this runs long enough (2+ weeks), the aggregate metrics become a
  credible signal that the strategy has (or doesn't have) an edge — far
  more useful than a single week of $100 live trading.

---

*Last updated: 2026-07-05*
