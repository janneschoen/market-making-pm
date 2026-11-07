"""
Entry point for the Polymarket market-making bot.

Modes:
  live     — real CLOB trading (default)
  headless — paper-trading simulation, logs to CSV, prints periodic status
  tui      — paper-trading simulation with Rich-powered real-time dashboard

Workflow:
1. Scan configured locations for weather markets within the trading window
2. Score and rank markets by (volume × uncertainty) — a proxy for expected PnL
3. Run quoting cycles on the top-N markets in parallel via asyncio
4. (Paper mode) Simulation engine intercepts orders; matches against live bids
"""
from inventory_management import neutralise_positions
from quoting_cycle import do_quoting_cycle
from market_info import get_locations_markets, get_hours_to_resolution
from market_action import init_client
from config import load_config, YES, NO
import time
import json
import asyncio
import sys

# ── Mode dispatch ───────────────────────────────────────────────────────────

async def main():
    # Parse optional --mode flag and config path from CLI
    cli_mode = None
    config_arg = None
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == '--mode' and i + 1 < len(sys.argv):
            cli_mode = sys.argv[i + 1]
            i += 2
        elif sys.argv[i].startswith('--mode='):
            cli_mode = sys.argv[i].split('=', 1)[1]
            i += 1
        elif not sys.argv[i].startswith('--'):
            config_arg = sys.argv[i]
            i += 1
        else:
            i += 1

    if config_arg:
        run = load_config(config_arg)
    else:
        run = load_config()

    if cli_mode:
        run.mode = cli_mode
        run.simulation_enabled = cli_mode != 'live'

    valid_modes = ('live', 'headless', 'tui')
    if run.mode not in valid_modes:
        print(f"Unknown mode '{run.mode}'. Valid: {', '.join(valid_modes)}")
        sys.exit(1)

    print(f"Mode: {run.mode}")

    # ── Discover markets (same for all modes) ──
    client = init_client() if run.mode == 'live' else None
    all_markets = []

    MAX_DAY_DELAY = 30
    for location in run.locations:
        day_delay = 0
        markets_of_location = get_locations_markets(location, day_delay)
        hours_to_resolution = await get_hours_to_resolution(markets_of_location[0])

        while (day_delay < MAX_DAY_DELAY
               and not (run.trading_window[0] > hours_to_resolution > run.trading_window[1])):
            day_delay += 1
            markets_of_location = get_locations_markets(location, day_delay)
            hours_to_resolution = await get_hours_to_resolution(markets_of_location[0])

        if day_delay == MAX_DAY_DELAY:
            continue

        for market in markets_of_location:
            all_markets.append(market)

    print(f"Scanned {len(all_markets)} markets for {len(run.locations)} locations.")

    filtered_markets = []
    for market in all_markets:
        hours_to_resolution = await get_hours_to_resolution(market)
        if run.trading_window[0] > hours_to_resolution > run.trading_window[1]:
            filtered_markets.append(market)
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
            f"(res.: {round(await get_hours_to_resolution(market), 2)}h)"
        )

    # ── Route to mode-specific runner ──
    if run.mode == 'live':
        await run_live(run, client, trading_markets)
    elif run.mode == 'headless':
        await run_headless(run, trading_markets)
    elif run.mode == 'tui':
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
    print(f"\nSimulation complete.")
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
