"""
Forecast layer: calibrated P(bucket) for weather contracts.

Modules
-------
station   — contract city → resolution station, unit and timezone
buckets   — contract bucket definitions with exact parity to settlements
ensemble  — point-in-time GEFS cycle selection (no lookahead)

Phase 0 delivers the parity-critical, dependency-free parts so they can be
tested before any forecast data is fetched.  Network access (Open-Meteo
ensembles, METAR/ERA5 truth) arrives in Phase 1 behind these interfaces.
"""
