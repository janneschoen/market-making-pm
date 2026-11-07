"""
Forecast scoring: Brier, log-loss, reliability.

The historical gate (ROADMAP §9/Phase 1) is decided by these numbers:
does `P(bucket)` beat the market-implied probability *after costs*?  A
proper scoring rule is the only honest way to answer that.

All functions accept sequences of probabilities and 0/1 (or bool) outcomes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence

EPS = 1e-15


def _pairs(probabilities: Sequence[float],
           outcomes: Sequence[float]) -> List[tuple[float, float]]:
    if len(probabilities) != len(outcomes):
        raise ValueError(
            f"length mismatch: {len(probabilities)} probabilities vs "
            f"{len(outcomes)} outcomes"
        )
    return [(float(p), float(o)) for p, o in zip(probabilities, outcomes)]


def brier_score(probabilities: Sequence[float],
                outcomes: Sequence[float]) -> float:
    """Mean squared error of probabilistic forecasts (lower is better)."""
    pairs = _pairs(probabilities, outcomes)
    if not pairs:
        raise ValueError("brier_score needs at least one forecast")
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def log_loss(probabilities: Sequence[float],
             outcomes: Sequence[float]) -> float:
    """Mean negative log-likelihood (lower is better)."""
    pairs = _pairs(probabilities, outcomes)
    if not pairs:
        raise ValueError("log_loss needs at least one forecast")
    total = 0.0
    for p, o in pairs:
        p = min(max(p, EPS), 1.0 - EPS)
        total -= o * math.log(p) + (1.0 - o) * math.log(1.0 - p)
    return total / len(pairs)


def brier_skill_score(model_probabilities: Sequence[float],
                      reference_probabilities: Sequence[float],
                      outcomes: Sequence[float]) -> float:
    """
    Skill vs a reference forecast (e.g. the market-implied probability).

    BSS = 1 − Brier(model) / Brier(reference).  Positive means the model
    beats the reference; ≤ 0 means it does not, and the historical gate
    should stop the project (ROADMAP §9).
    """
    reference = brier_score(reference_probabilities, outcomes)
    if reference == 0:
        return 0.0
    return 1.0 - brier_score(model_probabilities, outcomes) / reference


@dataclass(frozen=True)
class ReliabilityBin:
    """One row of a reliability diagram."""

    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_frequency: float


def reliability_bins(probabilities: Sequence[float],
                     outcomes: Sequence[float],
                     n_bins: int = 10) -> List[ReliabilityBin]:
    """
    Bin forecasts by predicted probability and report the hit rate.

    A calibrated model has `mean_predicted ≈ observed_frequency` in every
    populated bin.
    """
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    pairs = _pairs(probabilities, outcomes)
    if not pairs:
        return []

    buckets: List[List[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for p, o in pairs:
        idx = min(int(p * n_bins), n_bins - 1)
        if idx < 0:
            idx = 0
        buckets[idx].append((p, o))

    bins: List[ReliabilityBin] = []
    for idx, bucket in enumerate(buckets):
        if not bucket:
            continue
        bins.append(ReliabilityBin(
            lower=idx / n_bins,
            upper=(idx + 1) / n_bins,
            count=len(bucket),
            mean_predicted=sum(p for p, _ in bucket) / len(bucket),
            observed_frequency=sum(o for _, o in bucket) / len(bucket),
        ))
    return bins


def expected_calibration_error(probabilities: Sequence[float],
                               outcomes: Sequence[float],
                               n_bins: int = 10) -> float:
    """Count-weighted mean |predicted − observed| across reliability bins."""
    bins = reliability_bins(probabilities, outcomes, n_bins=n_bins)
    total = sum(b.count for b in bins)
    if total == 0:
        return 0.0
    return sum(
        b.count / total * abs(b.mean_predicted - b.observed_frequency)
        for b in bins
    )
