"""
Calibration: from ensemble member maxima to contract probabilities.

The raw empirical frequency of 15–30 members cannot resolve bucket tails: if
no member exceeds 22.7 °C, the contract bucket at 23 °C gets probability zero
and one bad tail destroys log-loss.  Members also estimate a *gridpoint*
daily max, while the contract settles on a *station* observation, so a small
amount of additional uncertainty is physically justified.

`kernel_bucket_probabilities` treats each member maximum as a noisy estimate
with Gaussian error `sigma` (in °C) and integrates that density over each
bucket's settlement interval.  `sigma` is a calibration parameter: fix it from
a training window, never from the evaluation window (ROADMAP §5/§9).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, median
from typing import Dict, Iterable, Optional, Sequence

from forecast.buckets import Bucket, convert

DEFAULT_SIGMA_C = 0.75
DEFAULT_SCALE_GRID = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
DEFAULT_SIGMA_GRID = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)


@dataclass(frozen=True)
class Calibration:
    """Per-station correction from ensemble member maxima to settlement."""

    bias: float = 0.0
    scale: float = 1.0
    sigma: float = DEFAULT_SIGMA_C
    samples: int = 0

    def corrected(self, members: Sequence[float],
                  centre: Optional[float] = None) -> list:
        return corrected_maxima(members, centre, self.bias, self.scale)


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _bucket_interval(bucket: Bucket) -> tuple:
    """Continuous settlement interval (in the bucket's unit) for a bucket."""
    lower = float("-inf") if bucket.lower is None else bucket.lower - 0.5
    upper = float("inf") if bucket.upper is None else bucket.upper + 0.5
    return lower, upper


def kernel_bucket_probabilities(member_temps_c: Iterable[float],
                                buckets: Sequence[Bucket],
                                sigma_c: float = DEFAULT_SIGMA_C) -> dict:
    """
    `P(bucket)` as the mean over members of a Gaussian density mass.

    Each member contributes N(member_max, sigma²) in °C, converted to the
    bucket's unit; the bucket probability is the density mass over
    `[lower − 0.5, upper + 0.5]` (the integers that round into the bucket).
    """
    members = [float(value) for value in member_temps_c]
    if not members:
        raise ValueError("need at least one member")
    if sigma_c <= 0:
        raise ValueError("sigma_c must be positive")

    total = {bucket.label: 0.0 for bucket in buckets}
    for temp in members:
        for bucket in buckets:
            lower, upper = _bucket_interval(bucket)
            mean = convert(temp, "C", bucket.unit)
            total[bucket.label] += _phi((upper - mean) / sigma_c) \
                - _phi((lower - mean) / sigma_c)
    return {label: mass / len(members) for label, mass in total.items()}


# ── Bias / spread calibration ───────────────────────────────────────────────

def winner_value(label: str) -> float:
    """Representative settlement value of a bucket (its rounded integer)."""
    bucket = Bucket.parse(label)
    if bucket.lower is None:
        return float(bucket.upper)
    if bucket.upper is None:
        return float(bucket.lower)
    return (float(bucket.lower) + float(bucket.upper)) / 2.0


def corrected_maxima(members: Sequence[float],
                     centre: Optional[float] = None,
                     bias: float = 0.0,
                     scale: float = 1.0) -> list:
    """
    Re-centre member maxima on `centre` and scale their spread.

    The member anomalies (member − ensemble mean) carry the forecast
    uncertainty; the centre carries the level.  `centre` is the hourly GFS
    daily max when available — replacing the 3-hourly GEFS max removes most
    of its cold sampling bias — otherwise the ensemble mean.
    """
    values = [float(value) for value in members]
    if not values:
        return []
    ensemble_mean = mean(values)
    base = ensemble_mean if centre is None else float(centre)
    return [base + (value - ensemble_mean) * scale + bias
            for value in values]


def _brier(probabilities: Dict[str, float], buckets: Sequence[Bucket],
           winner: str) -> float:
    total = 0.0
    for bucket in buckets:
        outcome = 1.0 if bucket.label == winner else 0.0
        total += (probabilities[bucket.label] - outcome) ** 2
    return total / len(buckets)


def fit_calibration(samples: Sequence[dict],
                    scale_grid: Sequence[float] = DEFAULT_SCALE_GRID,
                    sigma_grid: Sequence[float] = DEFAULT_SIGMA_GRID) -> Calibration:
    """
    Fit `bias`, `scale` and `sigma` on training samples.

    `bias` is the additive correction `median(settled − centre)` (a cold
    gridpoint gets a positive shift); `scale` and `sigma` are chosen by
    minimizing the training Brier score.  Samples are dicts with `members`,
    `centre` (or None), `buckets` and `winner`.
    """
    if not samples:
        return Calibration()

    diffs = []
    for sample in samples:
        centre = sample.get("centre")
        if centre is None:
            centre = mean(sample["members"])
        diffs.append(winner_value(sample["winner"]) - centre)
    bias = median(diffs)

    best = None
    for scale in scale_grid:
        for sigma in sigma_grid:
            total = 0.0
            for sample in samples:
                values = corrected_maxima(
                    sample["members"], sample.get("centre"), bias, scale
                )
                probabilities = kernel_bucket_probabilities(
                    values, sample["buckets"], sigma_c=sigma
                )
                total += _brier(probabilities, sample["buckets"],
                                sample["winner"])
            score = total / len(samples)
            if best is None or score < best[0]:
                best = (score, scale, sigma)

    _score, scale, sigma = best
    return Calibration(bias=bias, scale=scale, sigma=sigma,
                       samples=len(samples))


def apply_calibration(members: Sequence[float], centre: Optional[float],
                      buckets: Sequence[Bucket],
                      calibration: Calibration) -> Dict[str, float]:
    values = corrected_maxima(members, centre, calibration.bias,
                              calibration.scale)
    return kernel_bucket_probabilities(values, buckets,
                                       sigma_c=calibration.sigma)
