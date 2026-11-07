"""
Entry point for the Polymarket bot.

Modes:
  live     — real CLOB trading (default)
  headless — paper-trading simulation, logs to CSV/JSONL, periodic status
  tui      — paper-trading simulation with Rich-powered real-time dashboard

Workflow:
1. Discover weather markets within the trading window (cached Gamma scan)
2. Score and rank markets by (volume × uncertainty) — a proxy for expected PnL
3. Run quoting cycles on the top-N markets in parallel via asyncio
4. (Paper mode) Simulation engine intercepts orders; matches against live bids

The repository is an execution engine (`engine/`) with strategies
(`strategies/`); this file only does dispatch, discovery and scoring.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

from config import load_config
from engine.client import init_client
from engine.markets import (
    Market,
    configure_discovery_cache,
    find_markets_in_window,
)
from engine.positions import neutralise_positions
from measure.logs import get_logger
from strategies.inventory_mm import do_quoting_cycle

MAX_DAY_DELAY = 30


# ── Mode dispatch ───────────────────────────────────────────────────────────

def parse_args(argv):
    """Parse an optional `--mode` flag and an optional config path."""
    cli_mode = None
    config_arg = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--mode" and i + 1 < len(argv):
            cli_mode = argv[i + 1]
            i += 2
        elif arg.startswith("--mode="):
            cli_mode = arg.split("=", 1)[1]
            i += 1
        elif not arg.startswith("--"):
            config_arg = arg
            i += 1
        else:
            i += 1
    return cli_mode, config_arg


async def main():
    cli_mode, config_arg = parse_args(sys.argv[1:])
    run = load_config(config_arg) if config_arg else load_config()

    if cli_mode:
        run.mode = cli_mode
        run.simulation_enabled = cli_mode != "live"

    valid_modes = ("live", "headless", "tui")
    if run.mode not in valid_modes:
        print(f"Unknown mode '{run.mode}'. Valid: {', '.join(valid_modes)}")
        sys.exit(1)

    print(f"Mode: {run.mode}")
    log = get_logger(run.events_log_path)

    # ── Discover markets (same for all modes) ──
    client = init_client() if run.mode == "live" else None
    cache = configure_discovery_cache(run.cache_dir, run.discovery_cache_ttl_s)

    all_markets: list[Market] = []
    for location in run.locations:
        markets = find_markets_in_window(
            location, run.trading_window, max_day_delay=MAX_DAY_DELAY,
            cache=cache,
        )
        if not markets:
            continue
        all_markets.extend(markets)

    print(f"Scanned {len(all_markets)} markets for {len(run.locations)} locations.")

    max_hours, min_hours = run.trading_window
    filtered_markets = [
        market for market in all_markets
        if max_hours > market.hours_to_resolution() > min_hours
    ]
    all_markets = filtered_markets
    print(f"{len(all_markets)} markets matching filter.")

    sorted_markets = sorted(
        all_markets,
        key=lambda m: m.trading_volume * m.uncertainty,
        reverse=True,
    )
    trading_markets = sorted_markets[:run.number_of_markets]

    print(f"Retrieved best {len(trading_markets)} markets:")
    for market in trading_markets:
        print(
            f"- {market.question}",
            f"(vol.: {round(market.trading_volume, 2)})",
            f"(unc.: {round(market.uncertainty, 2)})",
            f"(res.: {round(market.hours_to_resolution(), 2)}h)",
        )

    log.log(
        "discovery",
        mode=run.mode,
        locations=len(run.locations),
        markets=len(all_markets),
        selected=len(trading_markets),
        condition_ids=[m.condition_id for m in trading_markets],
    )
    if run.risk_limits.enabled:
        print(f"Risk limits: {run.risk_limits}")

    # ── Route to mode-specific runner ──
    if run.mode == "live":
        await run_live(run, client, trading_markets)
    elif run.mode == "headless":
        await run_headless(run, trading_markets)
    elif run.mode == "tui":
        await run_tui(run, trading_markets)


# ── Live mode ───────────────────────────────────────────────────────────────

async def handle_market_live(run, client, market):
    error_heading = f"Error on market: '{market.question}'"
    await neutralise_positions(run, client, market, engine=None)
    print("Pre-cycle neutralisation complete.")
    print("Starting quoting cycle...")
    try:
        await do_quoting_cycle(run, client, market, engine=None)
    except Exception as e:
        print(error_heading)
        print("Failed quoting cycle:", e)
        return
    await neutralise_positions(run, client, market, engine=None)
    print("Handled market successfully:", market.question)


async def run_live(run, client, trading_markets):
    tasks = [asyncio.create_task(handle_market_live(run, client, m))
             for m in trading_markets]
    await asyncio.gather(*tasks)


# ── Paper-mode handler (shared by headless and TUI) ─────────────────────────

async def handle_market_paper(run, market, engine):
    """Run a single market in paper-trading mode."""
    error_heading = f"Error on market: '{market.question}'"
    engine.register_market(market)
    await neutralise_positions(run, client=None, market=market, engine=engine)
    try:
        await do_quoting_cycle(run, client=None, market=market, engine=engine)
    except Exception as e:
        print(error_heading)
        print("Failed quoting cycle:", e)
        return
    await neutralise_positions(run, client=None, market=market, engine=engine)
    print("Paper market complete:", market.question)


# ── Headless mode ───────────────────────────────────────────────────────────

async def run_headless(run, trading_markets):
    """Long-running paper-trading simulation with periodic CSV/console output."""
    from simulation.engine import SimulationEngine
    from simulation.metrics import MetricsEngine
    from simulation.tui import print_headless_status

    engine = SimulationEngine(run)
    metrics = MetricsEngine(engine)

    print("\nHeadless paper-trading started. Press Ctrl+C to stop.\n")

    # Launch per-market coroutines
    tasks = [asyncio.create_task(handle_market_paper(run, m, engine))
             for m in trading_markets]

    # Periodic status printing + metrics dump
    last_metrics_dump = time.time()
    last_equity_snap = time.time()

    try:
        while not all(t.done() for t in tasks):
            await asyncio.sleep(5)

            if time.time() - last_metrics_dump >= run.simulation_metrics_interval_s:
                print_headless_status(engine, metrics)
                # Also dump summary to JSON
                summary = metrics.summary_dict()
                with open('logs/metrics_summary.json', 'w') as f:
                    json.dump(summary, f, indent=2, default=str)
                last_metrics_dump = time.time()

            if time.time() - last_equity_snap >= run.simulation_equity_snapshot_interval_s:
                engine.record_equity_snapshot()
                last_equity_snap = time.time()

    except KeyboardInterrupt:
        print("\nStopping...")
        engine.running = False
        for t in tasks:
            t.cancel()

    # Final dump
    print_headless_status(engine, metrics)
    summary = metrics.summary_dict()
    with open('logs/metrics_summary.json', 'w') as f:
        json.dump(summary, f, indent=2, default=str)
    metrics.export_csv('logs/fills_export.csv')
    print(f"\nSimulation complete. Fills logged to {engine.log_path}")
    print(f"Total fills: {engine.total_fills_count}")
    print(f"Final PnL: ${engine.get_pnl():.2f}")
    print(f"Final equity: ${engine.get_equity():.2f}")


# ── TUI mode ────────────────────────────────────────────────────────────────

async def run_tui(run, trading_markets):
    """Paper-trading with Rich-powered real-time dashboard."""
    from simulation.engine import SimulationEngine
    from simulation.metrics import MetricsEngine
    from simulation.tui import SimulationTUI

    engine = SimulationEngine(run)
    metrics = MetricsEngine(engine)
    tui = SimulationTUI(engine, metrics, trading_markets)

    # Register all markets
    for m in trading_markets:
        engine.register_market(m)

    # Launch per-market coroutines
    paper_tasks = [asyncio.create_task(handle_market_paper(run, m, engine))
                   for m in trading_markets]

    # Launch periodic equity snapshots
    async def equity_recorder():
        while engine.running:
            await asyncio.sleep(run.simulation_equity_snapshot_interval_s)
            engine.record_equity_snapshot()

    snap_task = asyncio.create_task(equity_recorder())

    # Run TUI (blocks until engine.running = False)
    try:
        await tui.run()
    finally:
        engine.running = False
        for t in paper_tasks:
            if not t.done():
                t.cancel()
        snap_task.cancel()

    # Final summary
    stats = engine.get_stats()
    print("\nSimulation complete.")
    print(f"  Uptime:    {stats['uptime']/3600:.1f}h")
    print(f"  PnL:       ${stats['pnl']:.2f}")
    print(f"  Equity:    ${stats['equity']:.2f}")
    print(f"  Fills:     {stats['total_fills']}")
    print(f"  Fill rate: {stats['fill_rate']:.1%}")
    print(f"  Cycles:    {stats['quote_cycles']}")
    print(f"  Log:       {engine.log_path}")


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(main())
