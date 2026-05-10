# market-making-pm

A Python market-making bot for **Polymarket** focused on weather markets.


## Project Structure
- `main.py` — Entry point starting threads
- `quotingCycle.py` — Core logic for generating and updating quotes
- `marketAction.py` — Market actions running via CLOB client
- `marketInfo.py` — Fetch public market info
- `exitLoop.py` — Handling of market exit
- `invMan.py` — Polymarket Splitting / Merging (DOES NOT WORK YET)
- `config.py` + `config.json` — Bot logic configuration

## Features
- Fully configurable via `config.json`
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