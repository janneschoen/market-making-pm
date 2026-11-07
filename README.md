# Market Making on Polymarket

An automated trading stack for **[Polymarket](https://polymarket.com)** binary-outcome markets, currently targeting weather markets (e.g. *"Highest temperature in London on June 12?"*). It scans dozens of markets across global cities, scores them, and runs parallel quoting cycles on the best opportunities.

The repository is organised as an **execution engine** (`engine/`) plus **strategies** (`strategies/`): the engine decides *how* to trade, the strategy decides *what* to trade. Today the only strategy is inventory-skewed market making; the roadmap adds a forecast-driven taker strategy and a measurement layer that makes the PnL attributable.

> **Roadmap:** the next phase turns this execution stack into a forecast-driven trader — ensemble forecasts → calibrated `P(bucket)` → taker trades when the market is mispriced, with maker quoting added later as an execution optimization. See **[ROADMAP.md](ROADMAP.md)** for the design and sequencing. Phase 0 (restructure, bug fixes, measurement skeleton) is implemented.

---

## Table of Contents

- [Polymarket Primer — YES & NO Tokens](#polymarket-primer--yes--no-tokens)
- [Architecture](#architecture)
- [Market Selection](#market-selection)
- [Quoting Strategy](#quoting-strategy)
- [Inventory Risk Management](#inventory-risk-management)
- [Engine, Strategies, Forecast & Measurement](#engine-strategies-forecast--measurement)
- [Project Structure](#project-structure)
- [Setup](#setup)
- [Configuration Reference](#configuration-reference)

---

## Polymarket Primer — YES & NO Tokens

Polymarket is a prediction market where every binary question mints **two outcome tokens**: `YES` and `NO`. Exactly one resolves to \$1 and the other to \$0 once the event settles. Before resolution, both trade freely on a central-limit order book (CLOB).

The critical identity:

**price(YES) + price(NO) = 1**

This holds because the tokens are complementary payoffs: holding one of each is a guaranteed \$1 at settlement. Polymarket lets you redeem YES+NO pairs for \$1 in a single transaction (splitting/merging), which enforces the no-arbitrage relationship.

### Why buy YES *and* NO instead of buying and selling one asset?

A standard market maker quotes a **bid** and an **ask** on a single asset. To sell, you must already hold inventory. On Polymarket, you can get the same economic exposure without pre-existing inventory:

| Traditional MM | This bot |
|---|---|
| Buy YES at **p − s/2** | **Buy YES** at **p − s/2** |
| Sell YES at **p + s/2** (requires holding YES) | **Buy NO** at **1 − (p + s/2)** |

Buying NO at price **q** is **economically equivalent** to selling YES at **1 − q**. So two BUY orders — one on YES, one on NO — create a complete two-sided market with **zero starting inventory**:

```
Buy YES @ $0.48   ≡   bid YES
Buy NO  @ $0.50   ≡   ask YES (1 − 0.50 = $0.50)
```

When both orders fill, you hold 1 YES + 1 NO — a risk-free pair redeemable for \$1. You earned the spread without ever needing to short or borrow tokens. This is the core design choice of the bot.

---

## Architecture

```
┌──────────┐     ┌─────────────────────┐     ┌──────────────────────┐
│ config   │────▶│   Market Discovery  │────▶│   Market Scoring &   │
│ .json    │     │ (engine/markets.py) │     │   Selection          │
└──────────┘     │                     │     │  (volume × uncert.)  │
                 │ Fetch weather       │     └──────────┬───────────┘
                 │ markets per city    │                │ top-N
                 │ Filter by trading   │                │ markets
                 │ window              │                ▼
                 └─────────────────────┘     ┌──────────────────────┐
                                             │   Parallel Quoting   │
                                             │   (asyncio.gather)   │
                                             └──────────┬───────────┘
                                                        │
                          ┌─────────────────────────────┼─────────────────────────────┐
                          │                             │                             │
                          ▼                             ▼                             ▼
               ┌──────────────────┐          ┌──────────────────┐          ┌──────────────────┐
               │   Market 1       │          │   Market 2       │          │   Market N       │
               │                  │          │                  │          │                  │
               │ ① Neutralise     │          │ ① Neutralise     │          │ ① Neutralise     │
               │ ② Quote loop     │          │ ② Quote loop     │          │ ② Quote loop     │
               │ ③ Neutralise     │          │ ③ Neutralise     │          │ ③ Neutralise     │
               └──────────────────┘          └──────────────────┘          └──────────────────┘
```

### Per-market lifecycle

Each market goes through three stages:

| Stage | What happens |
|---|---|
| **① Pre-cycle neutralisation** | Cancel all open orders. Sell excess tokens at market bid (FOK) until YES ≈ NO. Guarantees a clean start. |
| **② Quoting cycle** | Continuous loop: fetch bids → compute skewed midpoint → post two BUY orders (YES + NO) → cancel stale orders → sleep `refresh_rate` seconds. Runs until `hours_to_resolution` drops below the lower bound of `trading_window`. |
| **③ Post-cycle neutralisation** | Same as ① — liquidates whatever inventory accumulated during quoting. Forces exit at market-bid (accepts slippage for certainty). |

---

## Market Selection

The bot scans **15 global cities** (configurable) and fetches their daily temperature markets. Each market is then scored by:

**score = volume × uncertainty**

Where:

| Factor | Formula | Rationale |
|---|---|---|
| **Volume** | Raw USDC volume from Gamma API | Higher volume → tighter spreads, higher fill probability, more PnL opportunity |
| **Uncertainty** | **1 − 2·\|0.5 − p\|** | Peaks at 1.0 when the market is a 50/50 coin flip (maximum disagreement = maximum trading). Decays linearly to 0 at fully-resolved markets (0% or 100%) |

Markets are sorted descending by score and the **top N are selected** (`number_of_markets` in config). The bot also filters by `trading_window` — only markets resolving within `[max_hours, min_hours]` are considered. This avoids quoting on markets that are too far from resolution (low information flow) or too close (resolution risk, wide spreads).

---

## Quoting Strategy

The core algorithm is **inventory-skewed market making** — a lightweight approximation of the Avellaneda-Stoikov framework adapted for binary-outcome markets.

### Per tick

1. **Fetch market state** — best bid for YES and NO from the CLOB order book.
2. **Imply YES ask** — from the NO bid via **yesAsk = 1 − noBid**.
3. **Compute market mid & spread** — **mid = (yesBid + yesAsk) / 2**.
4. **Measure inventory** — **exposure = YES_held − NO_held**.
5. **Skew midpoint**:

**ownMid = marketMid − (exposure × skewIntensity)**

6. **Post two BUY orders** (post-only, GTC):

**quote_YES = ownMid − spread/2**

**quote_NO = 1 − (ownMid + spread/2)**

7. **Cancel previous orders** (after posting new ones — no liquidity gap).
8. **Sleep** `refresh_rate` seconds, repeat.

### Intuition for the skew

| Condition | ownMid vs marketMid | Effect |
|---|---|---|
| Long YES (YES > NO) | ownMid **lower** | YES bid drops → discourages more YES buys |
| Short YES (NO > YES) | ownMid **higher** | YES bid rises → attracts YES sells |
| Flat (YES ≈ NO) | ownMid ≈ marketMid | symmetric quotes |

The `skew_intensity` parameter controls how aggressively the bot leans against its inventory. Higher values mean faster mean-reversion but risk quoting too far from the market and not getting filled.

### Example

Config: `spread = 0.04`, `skew_intensity = 0.01`.  
Market mid = 0.52. Bot holds 10 YES and 4 NO (exposure = +6).

**ownMid = 0.52 − (6 × 0.01) = 0.46**

**quote_YES = 0.46 − 0.02 = 0.44**

**quote_NO = 1 − (0.46 + 0.02) = 0.52**

Without skew the quotes would be YES @ 0.50, NO @ 0.54. The long-YES exposure pushes both quotes down by 6¢, making it cheaper for others to buy from the bot (reducing its YES inventory).

---

## Inventory Risk Management

Two guardrails prevent runaway directional exposure:

### 1. Quoting skew (continuous)

The midpoint skew described above acts as a **soft** inventory control — it continuously tilts quotes to encourage mean-reversion while still quoting. Inventory drifts slowly back to zero without stopping trading.

### 2. Neutralisation (hard exit)

Before and after each quoting cycle, the bot **force-liquidates** any imbalance:

1. Cancel all open orders.
2. If YES > NO → sell excess YES at the current market bid (FOK).
3. If NO > YES → sell excess NO at the current market bid (FOK).
4. Repeat every 5 seconds until **\|YES − NO\| <** `minimal_order_size`.

This is a **market-order exit** — it accepts slippage in exchange for guaranteed flat exposure. It's intentionally aggressive because the alternative (holding through resolution) is a binary gamble the bot has no edge on.

> ⚠ **Note:** Redeeming equal YES+NO pairs for USDC (Polymarket splitting/merging) is not yet implemented. When both sides have filled, the bot currently holds the paired tokens rather than recycling the collateral. This is a planned improvement.

---

## Engine, Strategies, Forecast & Measurement

The codebase is split along the one boundary that matters (ROADMAP §3): **alpha and execution are separate layers**.

| Package | Responsibility |
|---|---|
| `engine/` | CLOB auth (`client`), order lifecycle (`orders`), cached discovery + books + UTC clock (`markets`), balances/neutralisation (`positions`), hard limits (`risk`) |
| `strategies/` | Trading decisions. `inventory_mm` is the current spread-capture strategy |
| `forecast/` | Contract station/bucket parity (`station`, `buckets`) and point-in-time GEFS cycle selection (`ensemble`) — the parity-critical parts that must be correct before any probability is priced |
| `measure/` | Append-only JSONL event log (`logs`), per-trade attribution, mark-outs, Brier/log-loss/reliability |

### Event log

Every quote, order, fill, neutralisation step and risk rejection is appended to
`logs/events.jsonl` as one JSON object per line, with a UTC timestamp:

```json
{"ts":"2026-06-12T03:00:00+00:00","event":"quote","mode":"paper","condition_id":"0x…","yes_bid":0.48,"no_bid":0.50,"market_mid":0.49,"own_mid":0.49,"yes_quote":0.47,"no_quote":0.49,"inventory_yes":0.0,"inventory_no":0.0,"exposure":0.0}
```

This is the raw evidence behind every later claim about PnL — no dashboards until
the numbers exist.

### Correctness preconditions

- **Point-in-time forecasts.** `forecast.ensemble.latest_available_cycle(as_of)` returns the latest GEFS cycle whose ~4 h publication lag had elapsed at `as_of`; using `datetime.now().replace(hour=0)` silently looks ahead and invalidates any historical test.
- **Bucket/station parity.** `forecast.buckets` makes the rounding convention, unit and bucket edges explicit and validates that a contract's buckets partition the outcome space (no gaps, overlaps, or missing tails). `forecast.contracts` parses each market's own resolution description into a spec — station, unit, rounding, source family — and `forecast.station.resolve_station` raises rather than substituting a nearby gridpoint.
- **UTC everywhere.** `engine.clock` is injectable; tests run against a fixed clock.

### Historical dataset (Phase 1)

```bash
python -m eval.historical --out logs/history/resolved_events.jsonl
```

Harvests every closed weather contract per city series, parses the contract's
own resolution description into a spec (station/ICAO, unit, rounding, source
family), validates the bucket partition, extracts the settled bucket and token
IDs, and writes one JSONL row per contract. Gamma responses are cached under
`logs/cache/gamma_events/`, so re-runs are cheap.

Current run: **4,779 contracts**, 15 cities, 2025-01-22 → 2026-11-07, across
**8 contract families** (`wunderground|C`, `wunderground|F`, `noaa|C`,
`noaa|F`, `hko|C`, `metar|C`, `metar|F`, `cwa|C`), 0 ambiguous settlements,
1 malformed contract rejected and 7 rows with an unverified station flagged
(Taipei CWA).

```bash
python -m eval.prices --out logs/history/market_prices.jsonl
```

Joins each contract to its YES-token price history at two decision times
anchored to the station's **local** midnight (T-24h, T-12h) — never after the
settlement day has started — forward-fills the last quote with a 6 h staleness
cap, and records missing buckets explicitly. One request per token, cached
under `logs/cache/prices/`; 48k tokens take ~8 minutes at 16 workers.

Current run: **48,122 token histories**, 0 failed requests, **86.7%** of
contracts fully quoted at T-12h (**84.1%** at T-24h), median implied bucket
sum 1.04–1.06 (market overround), and the winning bucket trades at a mean
price of **0.33** vs ≈0.11 under a uniform bucket prior. Point-in-time
forecasts are the remaining input before the Brier/log-loss gate.

---

## Project Structure

```
market-making-pm/
├── main.py                    # Entry point — discovery, scoring, parallel dispatch
├── config.py / config.json    # Config dataclass + trading parameters
│
├── engine/                    # Reusable execution layer (no price view)
│   ├── client.py              # CLOB client init / auth
│   ├── orders.py              # place / cancel / open orders, balances, order verification
│   ├── markets.py             # cached Gamma discovery, order books, UTC resolution clocks
│   ├── positions.py           # balances + force-neutralisation
│   ├── risk.py                # notional / position / daily-loss limits, kill switch
│   └── clock.py               # injectable UTC clock
├── strategies/
│   └── inventory_mm.py        # inventory-skewed two-sided MM
├── forecast/                  # contract semantics + point-in-time ensembles
│   ├── contracts.py           # contract → station/unit/rounding/source spec
│   ├── station.py             # resolution stations (per contract, not per city)
│   ├── buckets.py             # bucket labels/ranges, rounding, partition checks
│   └── ensemble.py            # point-in-time GEFS cycle selection
├── eval/
│   ├── historical.py          # Phase 1: resolved-contract dataset + parity probe
│   └── prices.py              # Phase 1: market-implied P(bucket) at decision times
├── measure/                   # the part that makes results credible
│   ├── logs.py                # append-only JSONL event log
│   ├── attribution.py         # per-trade edge, PnL decomposition
│   ├── markout.py             # adverse-selection mark-outs (+1m / +5m / +1h)
│   └── scoring.py             # Brier, log-loss, reliability
├── simulation/                # paper-trading engine, metrics, TUI
├── tests/                     # pytest suite (no network)
└── requirements.txt
```

---

## Setup

### Prerequisites

- Python 3.10+
- A Polymarket account with API credentials

### Installation

```bash
git clone https://github.com/janneschoen/market-making-pm.git
cd market-making-pm
pip install -r requirements.txt
```

### API credentials

Create a `.env` file in the project root:

```env
POLYMARKET_KEY=your_private_key_hex
POLYMARKET_FUNDER=0xYourFunderAddress
```

These can be generated from the [Polymarket API settings](https://polymarket.com/settings/api).

### Running

```bash
# With default config (config.json)
python main.py

# With a custom config
python main.py path/to/custom_config.json

# Paper-trading dashboard instead of live
python main.py --mode tui
```

### Tests

```bash
pytest -q
```

The suite covers slug rollover, UTC clock handling, discovery caching, bucket
parity, no-lookahead cycle selection, scoring, risk limits and the quoting
strategy's order-verification path. It never touches the network.

---

## Configuration Reference

All parameters live in `config.json`:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `trading_window` | `[int, int]` | `[36, 12]` | Hours-before-resolution range. Markets outside `[36h, 12h]` are skipped. |
| `number_of_markets` | `int` | `10` | How many top-scored markets to quote simultaneously. |
| `minimal_order_size` | `int` | `5` | Minimum order size in USDC. Orders ≤ this are skipped (dust prevention). |
| `standard_order_size` | `int` | `20` | Size of each quote in USDC. |
| `spread` | `float` | `0.04` | Total spread width around own-mid. YES bid = ownMid − spread/2, effective ask = ownMid + spread/2. |
| `skew_intensity` | `float` | `0.01` | Per-unit-inventory midpoint shift. Higher = more aggressive inventory mean-reversion. |
| `refresh_rate` | `int` | `5` | Seconds between quote updates (cancel + repost). |
| `events_log_path` | `str` | `logs/events.jsonl` | Append-only JSONL event log (quotes, orders, fills, risk blocks). |
| `cache_dir` | `str` | `logs/cache/gamma` | On-disk Gamma discovery cache. |
| `discovery_cache_ttl_s` | `float` | `300` | Seconds before a cached market-discovery response expires. |
| `risk.max_notional` | `float \| null` | `null` | Max USDC committed per UTC day (`null` disables). |
| `risk.max_position_per_market` | `float \| null` | `null` | Max absolute shares held per market. |
| `risk.max_daily_loss` | `float \| null` | `null` | Daily loss that latches the kill switch. |
| `locations` | `[str]` | 15 cities | Cities to scan for weather markets. Markets follow the slug `highest-temperature-in-{city}-on-{month}-{day}-{year}` (UTC date, month/day/year all shifted together). |
