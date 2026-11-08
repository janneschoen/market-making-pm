# ROADMAP — From Market Maker to Forecast-Driven Trader

*Status: Phase 0 implemented. Phase 1 in progress — the dataset, market-implied prices and a point-in-time forecast pipeline exist; the first out-of-sample gate read says the market is still ahead. Last updated: 2026-11-07.*

This document is the design record for the next phase of this repository. It
supersedes the "back to live" section of `PLANNING.md`.

---

## 1. Context

The repository currently contains a working **execution stack** and a working
**market-making strategy**, but nothing that has a *view on price*:

| Component | State |
|---|---|
| CLOB client, auth, balances | Works (`market_action.py`) |
| Market discovery via Gamma API | Works, has bugs (§10) |
| Order place / cancel lifecycle | Works (`market_action.py`) |
| Paper-trading engine, fill simulation, metrics, TUI | Works (`simulation/`) |
| Inventory-skewed market making | Works (`quoting_cycle.py`) |
| Directional / value signal | **Missing** |
| Attribution (where did PnL come from?) | **Missing** |

`quoting_cycle.py` computes quotes from `market_mid` and inventory alone. It
has no opinion about whether the market price is *right* — it earns the spread
or it doesn't. Meanwhile `../pm-weather/` contains an ensemble-based
temperature probability model with no market data and no execution.

**The two halves are complementary and share no code.** This roadmap merges
them.

---

## 2. What we are building

> A Polymarket weather bot built on this repository's execution stack, whose
> strategy layer is **forecast → calibrated `P(bucket)` → taker trade when
> mispriced**, with maker quoting added later as an *execution optimization*
> and per-trade attribution + Brier scoring as the primary output.

Note the ordering: **forecast first, market making second.** The existing
market-making strategy stays in the repo as a second strategy, but it is not
the thing being optimised right now.

---

## 3. Guiding principles

1. **Alpha and execution are separate layers.** A strategy decides *what to
   trade*; the engine decides *how to trade it*. Never mix them in one file.
2. **Taker before maker.** Crossing the spread gives clean, attributable PnL
   (`PnL = Σ model_p − price − fees`). Resting quotes mix alpha with spread
   capture and inventory luck, making results uninterpretable.
3. **No live capital before historical evidence.** The historical gate in §9
   is mandatory, not a suggestion.
4. **Bucket/station parity is a correctness precondition.** If the model prices
   a different random variable than the contract settles on, every number
   downstream is noise.
5. **Publish negative results.** A clean "the ensemble does not beat the market
   after costs" is a legitimate and valuable outcome.

---

## 4. Target architecture

```
market-making-pm/
├── engine/                     # ← reusable execution layer (mostly moved from top level)
│   ├── client.py               # CLOB client init, auth        (from market_action.py)
│   ├── orders.py               # place / cancel / open orders  (from market_action.py)
│   ├── markets.py              # Gamma discovery, books, clocks (from market_info.py)
│   ├── positions.py            # balances, neutralisation, merge/redeem
│   └── risk.py                 # max notional, max position, daily loss, kill switch
├── forecast/                   # ← new: temperature probability model
│   ├── ensemble.py             # GEFS/ECMWF fetch via Open-Meteo, point-in-time
│   ├── station.py              # contract → station → resolution operator mapping
│   ├── buckets.py              # contract bucket definitions, exact parity
│   └── calibrate.py            # ensemble dispersion → calibrated P(bucket)
├── strategies/
│   ├── inventory_mm.py         # existing behaviour (from quoting_cycle.py)
│   └── forecast_taker.py       # ← new: take when model_p beats price
├── measure/                    # ← new: the part that makes results credible
│   ├── attribution.py          # per-trade edge, spread capture, PnL decomposition
│   ├── markout.py              # adverse selection at +1m / +5m / +1h
│   └── scoring.py              # Brier, log-loss, reliability diagrams
├── eval/
│   └── historical.py           # gate: model vs market-implied on resolved markets
├── simulation/                 # unchanged
├── main.py                     # mode dispatch, extended
└── config.json
```

The `engine/` split is a rename-and-move, not a rewrite. Its purpose is to make
the repo's headline honest: *an execution engine with strategies*, rather than
*a market-making bot* that happens to have extra code in it.

---

## 5. Forecast layer

### Data

| Input | Source | Notes |
|---|---|---|
| Ensemble forecasts | Open-Meteo ensemble API (GEFS ~30 members; ECMWF optional) | Free, JSON, no key. Replaces the current per-(member, lead-time) GRIB downloads in `pm-weather/`. |
| Station truth | METAR / airport observations; ERA5 for backfill | Must match the station named in the contract's resolution rules. |
| Market prices | Gamma / CLOB (`prices-history`) | Point-in-time, timestamped. |

### Point-in-time correctness (blocking bug in current code)

GEFS cycles at 00/06/12/18Z with a ~3.5–4 h publication lag. `pm-weather`
currently does:

```python
modelInit = datetime.now().replace(hour=0, ...)   # ← WRONG
```

It must instead select **the latest cycle whose data was actually available at
the decision timestamp**, and record that timestamp alongside the probability.
Otherwise the historical test is contaminated by lookahead.

### Bucket/station parity

For each contract, resolve and store explicitly:

- the **resolution station** (not a 0.25° gridpoint),
- the **operator** (`>=`, `<`, open-ended, integer rounding),
- the **unit and rounding convention**,
- the **timezone of the day boundary**.

The current `pm-weather` bucketing (`int(temp + 0.5)`) is **not** universally
wrong: the METAR-era contracts do round to the nearest whole degree with
half rounded up. It is wrong in the ways that matter — it bins a 0.25°
gridpoint rather than the station's METAR observation, ignores the station's
local day boundary, and cannot express the two-degree Fahrenheit range family
(`25–26°F`) used by the Wunderground/NOAA contracts. Bucketing must therefore
come from a parsed per-contract spec, not a single formula.

Verified against 4 779 live resolved contracts (Phase 1 dataset, `eval/historical.py`):

| Family (`source\|unit\|width`) | Contracts | Example |
|---|---|---|
| `wunderground\|C\|1` | 2 205 | `13°C` |
| `wunderground\|F\|2` | 1 320 | `25–26°F` |
| `noaa\|C\|1` | 614 | `13°C` |
| `hko\|C\|1` | 234 | `13°C` (Hong Kong Observatory) |
| `noaa\|F\|2` | 183 | `25–26°F` |
| `metar\|C\|1` | 171 | `13°C`, half-up, station ICAO |
| `metar\|F\|2` | 45 | `40-41°F`, half-up, station ICAO |
| `cwa\|C\|1` | 7 | Taipei CWA, no station named |

Stations and units change over time **within a city** (Paris moves LFPG → LFPB;
Taipei moves RCTP → RCSS; London and NYC are °F in winter 2025 and °C later),
so a per-city station table is insufficient.

### Contract

```python
p_bucket(city: str, date: date, bucket: Bucket, as_of: datetime) -> float
```

Pure function, no I/O, fully testable. Calibration (isotonic or logistic on
ensemble dispersion → empirical hit rate) lives behind this interface.

---

## 6. Strategy layer

### 6.1 Forecast taker — phase 1, the alpha proof

```
edge = p_bucket - ask - slippage(size, book) - fees
if edge > threshold:  buy
```

Requirements:

- **Depth-aware**: size against the actual book, not the top of book. Log the
  full book snapshot at decision time.
- **Thin-book guard**: a "mispricing" on an illiquid book is often just the
  price of illiquidity. If the edge only exists for a handful of shares, it is
  not an edge.
- **One-sided**: place a single order. The existing YES+NO pairing cannot
  express a directional view and must not be used here.
- **Every trade logged** with `model_p`, `price`, `edge`, `book_depth`,
  `fees`, `timestamp`.

### 6.2 Market making around fair value — phase 2, only after 6.1 shows an edge

Two changes to `inventory_mm.py`:

1. **Shrinkage, not replacement.**

   ```
   fair = w * model_p + (1 - w) * market_mid        # w ≈ 0.2–0.4
   ```

   The market knows things the model does not (live station observations,
   other forecasters, order flow). Quoting purely around the model means the
   only counterparties who fill you are the better-informed ones — textbook
   adverse selection. Start at low `w` and raise it only if mark-outs support it.

2. **Width from ensemble dispersion, not a constant.**

   `spread: 0.04` is a spread-harvesting parameter. Once quotes depend on a
   model, width must reflect *model uncertainty*: wide disagreement → wide or
   no quote; tight ensemble → tighten. A fixed width quotes tightest exactly
   when the model is least reliable — the worst possible failure mode.

Inventory skew stays on top of `fair` as the risk control.

### 6.3 Liquidity-reward quoting — orthogonal, optional

Resting size for Polymarket's per-market liquidity rewards is a *separate*
income stream that does not depend on directional accuracy (see
`booksignal`'s reward-farming analysis). If implemented, it must be tracked in
its own PnL bucket and never pooled with forecast alpha.

---

## 7. Measurement layer

This is the actual deliverable. Without it, live trading produces a number
nobody — including us — can explain.

| Metric | Definition | Why |
|---|---|---|
| **Brier / log-loss** | `P(bucket)` vs settlement, vs market-implied at same timestamp | Is the model better than the market? |
| **Per-trade edge** | `model_p − price − fees`, in cents | Is each trade individually justified? |
| **Realized spread capture** | fill price vs mid at fill time | For maker fills. |
| **Mark-out** | mid at +1m / +5m / +1h vs fill price | Adverse selection. Negative mark-out means we are being picked off. |
| **Inventory MTM** | position × mid, marked continuously | Separates alpha from inventory drift. |
| **PnL attribution** | spread + alpha + inventory + fees | Which component is working? |
| **Fill rate** | fills / quotes rested | Maker viability. |

Output: append-only JSONL trade log + a `reports/weekly.csv` + one regeneration
script. No dashboards until the numbers exist.

---

## 8. Pre-live blockers

Not "nice to have" — each of these leaks money or corrupts results on day one.

| Blocker | Current state | Action |
|---|---|---|
| **YES+NO merge/redeem** | Not implemented; `inventory_management.py` neutralises by *selling at the market bid, FOK* | Implement splitting/merging. Selling a redeemable pair at the bid gives back the spread the strategy just earned. |
| **Risk limits** | None | Max notional, max position per market, daily loss limit, kill switch. |
| **Order state machine** | Place-then-cancel, no verification | Idempotent placement; verify orders rested; recover cleanly after a crash with orders live. |
| **Clock / timezone** | `datetime.now()` local vs UTC `endDate` | UTC everywhere; inject a clock for tests. |
| **Secrets** | `.env` in repo root | Verify `.gitignore`; document env-only keys. |
| **API rate limits** | Up to 30 requests × 15 locations per startup | Cache discovery; back off on 429. |

---

## 9. Sequencing

### Phase 0 — Restructure and fix (no behaviour change) — **done**

- [x] Split `engine/` from `strategies/`
- [x] Fix slug month/day rollover and local-vs-UTC clock (`engine/markets.py`, `engine/clock.py`)
- [x] Cache market discovery (`DiscoveryCache`, TTL, memory + disk)
- [x] Add `measure/` skeleton and JSONL logging of every quote and fill
- [x] Add tests: bucket parity (`forecast/buckets.py`), no-lookahead on ensemble
      cycle selection (`forecast/ensemble.py`), plus slug/cache/risk/scoring
- [x] Update README to describe the repo as an engine + strategies

Behaviour changes limited to the documented bug fixes: the `trading_window`
upper bound is now enforced during quoting, new quotes are verified to have
rested before the old set is cancelled, and risk limits (disabled by default)
gate every order.

### Phase 1 — Historical gate (€0, no server)

- [x] Pull ~12 months of **resolved** weather markets and their price history
- [x] Reconstruct the ensemble forecast available at each decision timestamp
- [ ] Score `P(bucket)` vs market-implied: Brier, log-loss, reliability
- [ ] Compute edge **after** spread crossing and fees
- [ ] **Decision point** — see below

> **Progress (2026-11-07):**
>
> * Resolved-contract dataset (`python -m eval.historical`): 4 779 contracts,
>   15 cities, 2025-01-22 → 2026-11-07, 8 contract families, 0 ambiguous
>   settlements, 1 malformed contract rejected, 7 rows with an unverified
>   station (Taipei CWA) flagged rather than guessed.
> * Market-implied prices (`python -m eval.prices`): 48 122 YES-token
>   histories, 0 failed requests; full bucket coverage 86.7% at T-12h,
>   84.1% at T-24h; median implied bucket sum 1.04–1.06.
> * **Ensemble source:** Open-Meteo's ensemble archive is unusable (values
>   null beyond ~4 days; previous-runs deterministic only). Forecasts come
>   from NOAA GEFS on S3 (`forecast/gefs.py`) with `.idx` + HTTP Range
>   subsetting, plus the hourly GFS deterministic archive for the centre.
> * **Gate harness** (`eval/analyze.py`) scores multi-category Brier and
>   log-loss against the market and fits per-station bias/scale/σ on a
>   separate training window (`forecast/calibrate.py`).
> * **First walk-forward read** (train Aug 2026, test Sep 2026, 4 European
>   cities, 113 contracts, T-24h, 16 members):
>
>   | Forecast | Brier | BSS | mean P(winner) |
>   |---|---|---|---|
>   | Raw GEFS 3-hourly | 0.0928 | −0.53 | 0.154 |
>   | GFS hourly centre | 0.0780 | −0.29 | 0.185 |
>   | + Aug-fitted per-station calibration | 0.0750 | −0.24 | 0.249 |
>   | Test-tuned upper bound (diagnostic) | 0.0709 | −0.17 | 0.212 |
>   | Market | **0.0606** | — | **0.334** |
>
>   This is **not** yet a verdict: the market is still ahead on
>   a one-month, four-city sample. Per-station biases fitted on August do
>   not fully transfer to September, so the honest next requirement is a
>   6–12-month walk-forward across all 15 cities before declaring the gate
>   failed or passed.
>
> **Gate:** if the model does not beat the market-implied probability after
> costs, stop here and publish the negative result. Live trading cannot create
> an edge that is absent from the forecast.

### Phase 2 — Paper trading (1 VPS, ~€5/month)

- [ ] Deploy `forecast_taker` in paper mode against live books
- [ ] 2+ weeks of logged forecasts vs settlements
- [ ] Verify simulated fills are plausible (compare with observed book activity)
- [ ] Review mark-outs before committing capital

### Phase 3 — Small live

- [ ] Implement merge/redeem and risk limits first (§8)
- [ ] €200–500 of own capital
- [ ] Weekly PnL table, published; every claim backed by the log

### Phase 4 — Execution optimization (only if Phase 3 is positive)

- [ ] Add maker quoting around `fair` with dispersion-scaled width (§6.2)
- [ ] Compare taker vs maker fill quality on the same signal
- [ ] Optionally add liquidity-reward quoting (§6.3)

---

## 10. Known bugs (fix in Phase 0)

> Fixed in Phase 0: `market_info.py` (now `engine/markets.py`) and
> `quoting_cycle.py` (now `strategies/inventory_mm.py`) items below. The
> `forecast.py` items stay open until the model is ported in Phase 1.

`market_info.py`

- `day = now.day + day_delay` → produces invalid dates at month/year rollover
  (e.g. `june-31`), and the slug month comes from `now` while the day is
  shifted. Silently returns the wrong market or 404s.
- `datetime.now()` (local) compared against a UTC `endDate`.
- Discovery loop issues up to `30 requests × 15 locations` on every startup.

`quoting_cycle.py`

- `while hours_to_resolution > run.trading_window[1]` — the upper bound (36 h)
  is enforced at discovery only, not during quoting.
- New orders are posted *before* old ones are cancelled (correct, to avoid a
  liquidity gap) but there is no check that the new orders actually rested. A
  rejected post leaves the book empty while the old set is cancelled.
- `Market.resolution` is annotated `str` but holds a `datetime`.

`forecast.py` (in `pm-weather/`, to be ported)

- `modelInit = datetime.now().replace(hour=0, ...)` ignores publication lag.
- `if memberTemps:` is always truthy; should test for any non-empty member.
- `dailyMax = []` inside the loop is dead assignment; `max()` on an empty
  member raises.
- Only 6 of ~30 ensemble members are used.

---

## 11. Open questions

- Which cities/stations have enough resolved history to test on? (need months,
  not weeks)
- Shrinkage weight `w` — fit it, don't guess.
- Taker threshold above fees — fixed or volatility-scaled?
- Current Polymarket fee schedule and reward program rules — verify before
  any size decision.
- Jurisdiction / ToS for automated trading and reward farming.
- Does the ensemble's dispersion actually predict the *sign* of its error, or
  only its magnitude? This determines whether 6.2 can be trusted.

---

## 12. Definition of done

Phase 3 is complete when this repository's README can state, truthfully:

> *"Ran live with €X for N weeks: M trades, Brier 0.1XX vs market-implied 0.1YY,
> realized edge +Z¢/trade after fees, mark-out −W¢ at 5m, net +€V."*

Everything before that is preparation for that sentence.
