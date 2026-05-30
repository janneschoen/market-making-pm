# Market Making on Polymarket

A Python market-making bot for **Polymarket** focused on weather markets.

It gathers all open weather market bets from the locations set in the configuration, calculates which are most suitable for market making by giving each a score (uncertainty (closeness to 50%) * trading volume), and picks the n markets with the highest scores. The bot then market makes on these n markets in parallel using Asyncio.

The market making algorithm quotes around market midpoint with a set spread, and skews quotes by moving the midpoint with the formula `midpoint - inventory * skew_intensiy`, where `skew_intensity` is a constant from the configuration.


## Project Structure
- `main.py` — Entry point starting threads
- `quoting_cycle.py` — Core logic for generating and updating quotes
- `market_action.py` — Market actions running via CLOB client
- `market_info.py` — Fetch public market info
- `inventory_management.py` — Checking for / creating neutral positions
- `config.py` + `config.json` — Bot logic configuration

## Features
- Fully configurable via `config.json`
- Dynamic fetching of different markets + evaluation of each
- Inventory risk management to limit directional exposure
- Real-time interaction with Polymarket API

## Setup
1. Clone the repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Adjust config.json based on preferences
4. Add Polymarket API keys to .env file
5. Run the bot:
   ```bash
   python main.py
   ```
