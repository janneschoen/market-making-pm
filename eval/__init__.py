"""
Phase 1 evaluation: the historical gate.

`historical` builds the resolved-contract dataset and the spec/parity checks
that any forecast score has to stand on.  Scoring (Brier/log-loss vs the
market, after costs) lands here once the dataset and point-in-time forecasts
exist; see ROADMAP §9.
"""
