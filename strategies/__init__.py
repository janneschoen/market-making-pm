"""
Strategies: each module decides *what* to trade; `engine/` decides *how*.

Current
-------
inventory_mm        — inventory-skewed two-sided market making (spread capture)

Planned (ROADMAP §6)
--------------------
forecast_taker      — take when calibrated P(bucket) beats the ask (Phase 1/2)
inventory_mm-v2     — quote around `w · model_p + (1−w) · mid` (Phase 4)
"""
