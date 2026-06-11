# Market Making on Polymarket

An automated market-making bot for **[Polymarket](https://polymarket.com)** binary-outcome markets, currently targeting weather markets (e.g. *"Highest temperature in London on June 12?"*). It scans dozens of markets across global cities, scores them, and runs parallel, inventory-skewed quoting cycles on the best opportunities.

---

## Table of Contents

- [Polymarket Primer — YES & NO Tokens](#polymarket-primer--yes--no-tokens)
- [Architecture](#architecture)
- [Market Selection](#market-selection)
- [Quoting Strategy](#quoting-strategy)
- [Inventory Risk Management](#inventory-risk-management)
- [Project Structure](#project-structure)
- [Setup](#setup)
- [Configuration Reference](#configuration-reference)

---

## Polymarket Primer — YES & NO Tokens

Polymarket is a prediction market where every binary question mints **two outcome tokens**: `YES` and `NO`. Exactly one resolves to \$1 and the other to \$0 once the event settles. Before resolution, both trade freely on a central-limit order book (CLOB).

The critical identity:

$$\text{price(YES)} + \text{price(NO)} = 1$$

This holds because the tokens are complementary payoffs: holding one of each is a guaranteed \$1 at settlement. Polymarket lets you redeem YES+NO pairs for \$1 in a single transaction (splitting/merging), which enforces the no-arbitrage relationship.

### Why buy YES *and* NO instead of buying and selling one asset?

A standard market maker quotes a **bid** and an **ask** on a single asset. To sell, you must already hold inventory. On Polymarket, you can get the same economic exposure without pre-existing inventory:

| Traditional MM | This bot |
|---|---|
| Buy YES at $p - s/2$ | **Buy YES** at $p - s/2$ |
| Sell YES at $p + s/2$ (requires holding YES) | **Buy NO** at $1 - (p + s/2)$ |

Buying NO at price $q$ is **economically equivalent** to selling YES at $1 - q$. So two BUY orders — one on YES, one on NO — create a complete two-sided market with **zero starting inventory**:

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
│ .json    │     │  (market_info.py)   │     │   Selection          │
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

$$\text{score} = \text{volume} \times \text{uncertainty}$$

Where:

| Factor | Formula | Rationale |
|---|---|---|
| **Volume** | Raw USDC volume from Gamma API | Higher volume → tighter spreads, higher fill probability, more PnL opportunity |
| **Uncertainty** | $1 - 2 \lvert 0.5 - p \rvert$ | Peaks at 1.0 when the market is a 50/50 coin flip (maximum disagreement = maximum trading). Decays linearly to 0 at fully-resolved markets (0% or 100%) |

Markets are sorted descending by score and the **top $N$ are selected** (`number_of_markets` in config). The bot also filters by `trading_window` — only markets resolving within `[max_hours, min_hours]` are considered. This avoids quoting on markets that are too far from resolution (low information flow) or too close (resolution risk, wide spreads).

---

## Quoting Strategy

The core algorithm is **inventory-skewed market making** — a lightweight approximation of the Avellaneda-Stoikov framework adapted for binary-outcome markets.

### Per tick

1. **Fetch market state** — best bid for YES and NO from the CLOB order book.
2. **Imply YES ask** — from the NO bid via $\text{yesAsk} = 1 - \text{noBid}$.
3. **Compute market mid & spread** — $\text{mid} = (\text{yesBid} + \text{yesAsk}) / 2$.
4. **Measure inventory** — $\text{exposure} = \text{YES}_\text{held} - \text{NO}_\text{held}$.
5. **Skew midpoint**:

$$\text{ownMid} = \text{marketMid} - (\text{exposure} \times \text{skewIntensity})$$

6. **Post two BUY orders** (post-only, GTC):

$$\text{quote}_\text{YES} = \text{ownMid} - \frac{\text{spread}}{2}$$

$$\text{quote}_\text{NO} = 1 - \left(\text{ownMid} + \frac{\text{spread}}{2}\right)$$

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

$$\text{ownMid} = 0.52 - (6 \times 0.01) = 0.46$$

$$\text{quote}_\text{YES} = 0.46 - 0.02 = 0.44$$

$$\text{quote}_\text{NO} = 1 - (0.46 + 0.02) = 0.52$$

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
4. Repeat every 5 seconds until $|\text{YES} - \text{NO}| <$ `minimal_order_size`.

This is a **market-order exit** — it accepts slippage in exchange for guaranteed flat exposure. It's intentionally aggressive because the alternative (holding through resolution) is a binary gamble the bot has no edge on.

> ⚠ **Note:** Redeeming equal YES+NO pairs for USDC (Polymarket splitting/merging) is not yet implemented. When both sides have filled, the bot currently holds the paired tokens rather than recycling the collateral. This is a planned improvement.

---

## Project Structure

```
market-making-pm/
├── main.py                    # Entry point — market discovery, scoring, parallel dispatch
├── quoting_cycle.py           # Core quoting loop — inventory-skewed two-sided MM
├── market_action.py           # CLOB client wrapper — auth, balances, orders
├── market_info.py             # Market data — order-book bids, resolution time, market scraping
├── inventory_management.py    # Position neutralisation — force-flatten inventory
├── config.py                  # Config dataclass + loader
├── config.json                # Trading parameters
├── requirements.txt           # Python dependencies
└── README.md
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
```

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
| `exit_buffer` | `float` | `0.005` | *(Reserved — intended for early-exit margin before resolution.)* |
| `locations` | `[str]` | 15 cities | Cities to scan for weather markets. Markets follow the slug `highest-temperature-in-{city}-on-{month}-{day}-{year}`. |
