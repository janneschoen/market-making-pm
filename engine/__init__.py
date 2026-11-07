"""
Reusable execution layer for Polymarket.

Modules
-------
clock      — injectable UTC clock (no local-time drift, testable)
client     — CLOB client init / auth
orders     — place / cancel / open orders, balances
markets    — Gamma discovery (cached), order books, resolution clocks
positions  — balances and neutralisation
risk       — notional / position / daily-loss limits and kill switch

Nothing in this package may hold a view on price.  Strategies live in
`strategies/`; see ROADMAP.md §3.
"""
