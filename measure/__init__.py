"""
Measurement layer: the part that makes results credible.

Modules
-------
logs         — append-only JSONL event log (quotes, orders, fills, decisions)
attribution  — per-trade edge and PnL decomposition
markout      — adverse-selection mark-outs at +1m / +5m / +1h
scoring      — Brier, log-loss, reliability bins

Everything here is pure and side-effect free except `logs`, which is the one
place that writes output.  See ROADMAP §7.
"""
