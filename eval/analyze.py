"""
Phase 1 gate harness: score the model against the market.

For selected resolved contracts this computes

    model  P(bucket) from point-in-time GEFS members (forecast/gefs.py)
    market P(bucket) from CLOB prices-history at the same decision time

and reports multi-category Brier score, log-loss, and the mean probability
assigned to the bucket that actually settled.  If `BSS = 1 − Brier_model /
Brier_market` is not positive after costs, the roadmap says stop (ROADMAP §9).

`--gfs-centre` replaces the 3-hourly GEFS daily max with the hourly GFS
deterministic daily max (the ensemble anomalies still carry the spread), and
`--train-start/--train-end` fit per-station bias/scale/sigma on a training
window that is scored separately from the test window.

Usage:
    python -m eval.analyze --cities London,Paris,Madrid,Munich \
        --start 2026-09-01 --end 2026-09-30 \
        --train-start 2026-08-01 --train-end 2026-08-31 \
        --gfs-centre --members 16
"""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from forecast.buckets import Bucket, validate_partition
from forecast.calibrate import (
    Calibration,
    apply_calibration,
    fit_calibration,
)
from forecast.contracts import ContractSpec
from forecast.gefs import (
    DEFAULT_CACHE_DIR,
    DEFAULT_MEMBERS,
    DEFAULT_PRODUCT,
    bucket_probabilities_for,
    daily_maxima_for_day,
    deterministic_daily_maxima_for_day,
    station_key,
)
from forecast.station import resolve_icao, resolve_station, resolve_station_name
from measure.scoring import expected_calibration_error, reliability_bins

DEFAULT_ROWS_PATH = os.path.join("logs", "history", "resolved_events.jsonl")
DEFAULT_PRICES_PATH = os.path.join("logs", "history", "market_prices.jsonl")
DEFAULT_REPORT_PATH = os.path.join("logs", "history", "gate_report.json")
PROBABILITY_FLOOR = 1e-6
DEFAULT_MIN_TRAIN_SAMPLES = 15
# Per-share edge thresholds (in probability units, i.e. cents) at which the
# after-cost taker PnL is reported.
DEFAULT_EDGE_THRESHOLDS = (0.0, 0.01, 0.02, 0.03)
DEFAULT_RELIABILITY_BINS = 10


# ── Data loading ─────────────────────────────────────────────────────────────

def load_jsonl(path: str) -> List[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def market_prices_by_slug(rows: Sequence[dict]) -> Dict[str, dict]:
    return {row["slug"]: row for row in rows if row.get("slug")}


def station_for_row(row: dict, spec: ContractSpec):
    """Registry station for a contract row (ICAO first, then name, then city)."""
    if spec.icao:
        station = resolve_icao(spec.icao)
        if station:
            return station
    if spec.station_name:
        station = resolve_station_name(spec.station_name)
        if station:
            return station
    try:
        return resolve_station(row["city"])
    except Exception:
        return None


def spec_from_row(row: dict) -> ContractSpec:
    spec = row["spec"]
    unit = spec.get("unit", "C")
    return ContractSpec(
        city=row.get("city", ""),
        series=row.get("series", ""),
        station_name=spec.get("station", ""),
        icao=spec.get("icao"),
        timezone=spec.get("timezone"),
        unit=unit,
        rounding=None,
        source=spec.get("source", ""),
        labels=tuple(spec.get("labels") or ()),
        buckets=tuple(Bucket.parse(label, unit=unit)
                      for label in (spec.get("labels") or ())),
    )


# ── Samples ──────────────────────────────────────────────────────────────────

@dataclass
class Sample:
    """One scored contract at one decision time."""

    slug: str
    city: str
    station: str
    day: date
    spec: ContractSpec
    winner: str
    market: Dict[str, float]
    members: List[float] = field(default_factory=list)
    gfs_max: Optional[float] = None
    cycle: Optional[str] = None


def collect_samples(rows: Sequence[dict], prices: Dict[str, dict],
                    offset_hours: int = 24,
                    members: Sequence[str] = DEFAULT_MEMBERS,
                    product: str = DEFAULT_PRODUCT,
                    cache_dir: str = DEFAULT_CACHE_DIR,
                    workers: int = 12,
                    use_gfs: bool = False,
                    progress: bool = True) -> List[Sample]:
    """Build model/market samples for the selected contracts."""
    by_day: Dict[date, List[dict]] = defaultdict(list)
    for row in rows:
        try:
            by_day[date.fromisoformat(row["date"])].append(row)
        except (KeyError, TypeError, ValueError):
            continue

    samples: List[Sample] = []
    for day in sorted(by_day):
        specs: Dict[str, Tuple[object, ContractSpec, dict]] = {}
        entries: Dict[Tuple[str, str], Tuple[object, str]] = {}
        for row in by_day[day]:
            spec = spec_from_row(row)
            station = station_for_row(row, spec)
            if station is None or not spec.timezone:
                continue
            try:
                validate_partition(spec.buckets)
            except Exception:
                continue
            specs[row["slug"]] = (station, spec, row)
            entries[(station_key(station), spec.timezone)] = (station, spec.timezone)

        if not entries:
            continue
        station_entries = list(entries.values())
        maxima_by_station = daily_maxima_for_day(
            day, station_entries, offset_hours=offset_hours, members=members,
            product=product, cache_dir=cache_dir, workers=workers,
            progress=progress,
        )
        gfs_by_station = deterministic_daily_maxima_for_day(
            day, station_entries, offset_hours=offset_hours,
            cache_dir=cache_dir, workers=workers, progress=progress,
        ) if use_gfs else {}

        kept = 0
        for slug, (station, spec, row) in specs.items():
            price_row = prices.get(slug) or {}
            decision = (price_row.get("decisions") or {}).get(
                f"T-{offset_hours}h") or {}
            if not decision.get("complete"):
                continue
            key = station_key(station)
            entry = maxima_by_station.get(key) or {}
            maxima = entry.get("maxima") or {}
            if not maxima:
                continue
            market = {label: decision["prices"][label]
                      for label in spec.labels
                      if label in decision["prices"]}
            if len(market) != len(spec.labels):
                continue
            samples.append(Sample(
                slug=slug,
                city=row.get("city", ""),
                station=key,
                day=day,
                spec=spec,
                winner=row["winner"],
                market=market,
                members=list(maxima.values()),
                gfs_max=(gfs_by_station.get(key) or {}).get("max"),
                cycle=entry.get("cycle").isoformat() if entry.get("cycle") else None,
            ))
            kept += 1

        print(f"  {day}: {kept} samples", flush=True)

    return samples


# ── Scoring ──────────────────────────────────────────────────────────────────

def multiclass_brier(probabilities: Sequence[Dict[str, float]],
                     winners: Sequence[str]) -> float:
    """Mean squared error over every (contract, bucket) pair."""
    total = 0.0
    count = 0
    for probs, winner in zip(probabilities, winners):
        for label, probability in probs.items():
            outcome = 1.0 if label == winner else 0.0
            total += (probability - outcome) ** 2
            count += 1
    if count == 0:
        raise ValueError("no forecasts to score")
    return total / count


def winner_log_loss(probabilities: Sequence[Dict[str, float]],
                    winners: Sequence[str]) -> float:
    """Mean −log P(winner), clipped to stay finite."""
    total = 0.0
    count = 0
    for probs, winner in zip(probabilities, winners):
        probability = probs.get(winner)
        if probability is None:
            continue
        total -= math.log(max(probability, PROBABILITY_FLOOR))
        count += 1
    if count == 0:
        raise ValueError("no winner probabilities to score")
    return total / count


def mean_winner_probability(probabilities: Sequence[Dict[str, float]],
                            winners: Sequence[str]) -> float:
    values = [probs[winner] for probs, winner in zip(probabilities, winners)
              if winner in probs]
    return sum(values) / len(values) if values else float("nan")


def skill_score(model: Sequence[Dict[str, float]],
                market: Sequence[Dict[str, float]],
                winners: Sequence[str]) -> dict:
    brier_model = multiclass_brier(model, winners)
    brier_market = multiclass_brier(market, winners)
    return {
        "contracts": len(winners),
        "brier_model": brier_model,
        "brier_market": brier_market,
        "brier_skill_score": (1.0 - brier_model / brier_market
                              if brier_market else 0.0),
        "log_loss_model": winner_log_loss(model, winners),
        "log_loss_market": winner_log_loss(market, winners),
        "mean_p_winner_model": mean_winner_probability(model, winners),
        "mean_p_winner_market": mean_winner_probability(market, winners),
    }


def edge_summary(model: Sequence[Dict[str, float]],
                 market: Sequence[Dict[str, float]],
                 threshold: float = 0.02) -> dict:
    """How often (and how far) the model disagrees with the market."""
    edges = []
    over = 0
    for model_probs, market_probs in zip(model, market):
        for label, probability in model_probs.items():
            market_probability = market_probs.get(label)
            if market_probability is None:
                continue
            edge = probability - market_probability
            edges.append(edge)
            if edge > threshold:
                over += 1
    if not edges:
        return {"observations": 0}
    return {
        "observations": len(edges),
        "mean_edge": sum(edges) / len(edges),
        "mean_abs_edge": sum(abs(edge) for edge in edges) / len(edges),
        "share_edge_gt_threshold": over / len(edges),
    }


# ── Reliability ─────────────────────────────────────────────────────────────

def flatten_outcomes(probabilities: Sequence[Dict[str, float]],
                     winners: Sequence[str]) -> Tuple[List[float], List[float]]:
    """Flatten multi-category forecasts into (probability, outcome) pairs."""
    ps: List[float] = []
    outcomes: List[float] = []
    for probs, winner in zip(probabilities, winners):
        for label, probability in probs.items():
            ps.append(probability)
            outcomes.append(1.0 if label == winner else 0.0)
    return ps, outcomes


def reliability_summary(probabilities: Sequence[Dict[str, float]],
                        winners: Sequence[str],
                        n_bins: int = DEFAULT_RELIABILITY_BINS) -> dict:
    """Reliability diagram + ECE over every (contract, bucket) probability."""
    ps, outcomes = flatten_outcomes(probabilities, winners)
    if not ps:
        return {}
    bins = reliability_bins(ps, outcomes, n_bins=n_bins)
    return {
        "observations": len(ps),
        "n_bins": n_bins,
        "ece": expected_calibration_error(ps, outcomes, n_bins=n_bins),
        "bins": [
            {
                "lower": b.lower,
                "upper": b.upper,
                "count": b.count,
                "mean_predicted": b.mean_predicted,
                "observed_frequency": b.observed_frequency,
            }
            for b in bins
        ],
    }


# ── After-cost taker edge ───────────────────────────────────────────────────

@dataclass(frozen=True)
class CostModel:
    """Taker costs beyond the quoted history price, per share.

    `prices-history` returns a single traded price per token, not a book, so
    the ask is approximated as `mid + half_spread`.

    Fees follow Polymarket's published taker schedule
    (docs.polymarket.com/trading/fees, read 2026-11-08):

        fee_per_share = fee_rate × p × (1 − p)

    with `fee_rate = 0.05` for weather markets and no maker fee.  `fee` is an
    additional flat per-share cost, kept for sensitivity analysis.  All
    values are in probability units (0.01 = 1¢).
    """

    half_spread: float = 0.0
    fee_rate: float = 0.0
    fee: float = 0.0

    def fee_at(self, price: float) -> float:
        """Taker fee per share when buying at `price`."""
        return self.fee + self.fee_rate * price * (1.0 - price)

    @property
    def per_share(self) -> float:
        """Fixed per-share cost; the fee rate is price-dependent."""
        return self.half_spread + self.fee


@dataclass(frozen=True)
class TakerTrade:
    """One simulated taker buy of a bucket at the cost-adjusted ask."""

    slug: str
    city: str
    label: str
    model_p: float
    mid: float
    price: float
    fee: float
    won: bool

    @property
    def edge(self) -> float:
        return self.model_p - self.price - self.fee

    @property
    def pnl(self) -> float:
        return (1.0 if self.won else 0.0) - self.price - self.fee


def taker_trades(samples: Sequence[Sample],
                 model: Sequence[Dict[str, float]],
                 costs: CostModel,
                 threshold: float = 0.0) -> List[TakerTrade]:
    """Every bucket whose model edge survives spread crossing and fees."""
    trades: List[TakerTrade] = []
    for sample, model_probs in zip(samples, model):
        for label, model_p in model_probs.items():
            mid = sample.market.get(label)
            if mid is None:
                continue
            price = mid + costs.half_spread
            fee = costs.fee_at(price)
            edge = model_p - price - fee
            if edge <= threshold:
                continue
            trades.append(TakerTrade(
                slug=sample.slug,
                city=sample.city,
                label=label,
                model_p=model_p,
                mid=mid,
                price=price,
                fee=fee,
                won=label == sample.winner,
            ))
    return trades


def after_cost_summary(trades: Sequence[TakerTrade]) -> dict:
    """Aggregate one share per trade: edge, hit rate, PnL and ROI."""
    if not trades:
        return {
            "trades": 0,
            "contracts": 0,
            "staked": 0.0,
            "pnl": 0.0,
            "roi": 0.0,
            "mean_edge": 0.0,
            "mean_price": 0.0,
            "hit_rate": 0.0,
        }
    staked = sum(trade.price + trade.fee for trade in trades)
    pnl = sum(trade.pnl for trade in trades)
    winners = sum(1 for trade in trades if trade.won)
    return {
        "trades": len(trades),
        "contracts": len({trade.slug for trade in trades}),
        "staked": staked,
        "pnl": pnl,
        "roi": pnl / staked if staked else 0.0,
        "mean_edge": sum(trade.edge for trade in trades) / len(trades),
        "mean_price": sum(trade.price for trade in trades) / len(trades),
        "hit_rate": winners / len(trades),
    }


def after_cost_report(samples: Sequence[Sample],
                      model: Sequence[Dict[str, float]],
                      costs: CostModel,
                      threshold: float = 0.0,
                      thresholds: Sequence[float] = DEFAULT_EDGE_THRESHOLDS,
                      ) -> dict:
    """After-cost taker PnL at `threshold`, plus sensitivity and by city."""
    trades = taker_trades(samples, model, costs, threshold=threshold)
    summary = after_cost_summary(trades)
    by_city: Dict[str, List[TakerTrade]] = defaultdict(list)
    for trade in trades:
        by_city[trade.city].append(trade)
    return {
        "threshold": threshold,
        "contract_share": (summary["contracts"] / len(samples)
                           if samples else 0.0),
        "costs": {
            "half_spread": costs.half_spread,
            "fee_rate": costs.fee_rate,
            "fee": costs.fee,
            "fixed_per_share": costs.per_share,
        },
        "summary": summary,
        "by_city": {city: after_cost_summary(group)
                    for city, group in sorted(by_city.items())},
        "thresholds": [
            {"threshold": level,
             "summary": after_cost_summary(
                 taker_trades(samples, model, costs, threshold=level))}
            for level in thresholds
        ],
    }


def model_probabilities(sample: Sample,
                        calibration: Optional[Calibration] = None,
                        kernel_sigma: float = 0.0) -> Dict[str, float]:
    if calibration is not None:
        return apply_calibration(sample.members, sample.gfs_max,
                                 sample.spec.buckets, calibration)
    if kernel_sigma > 0:
        from forecast.calibrate import kernel_bucket_probabilities
        return kernel_bucket_probabilities(sample.members, sample.spec.buckets,
                                           sigma_c=kernel_sigma)
    return bucket_probabilities_for(sample.spec.buckets, sample.members)


def score_samples(samples: Sequence[Sample],
                  calibration: Optional[Calibration] = None,
                  kernel_sigma: float = 0.0) -> dict:
    model: List[Dict[str, float]] = []
    market: List[Dict[str, float]] = []
    winners: List[str] = []
    for sample in samples:
        model.append(model_probabilities(sample, calibration, kernel_sigma))
        market.append(sample.market)
        winners.append(sample.winner)
    return skill_score(model, market, winners)


def fit_per_station(samples: Sequence[Sample],
                    min_samples: int = DEFAULT_MIN_TRAIN_SAMPLES) -> Dict[str, Calibration]:
    """Fit calibration per station, falling back to a pooled fit."""
    if not samples:
        raise ValueError("no training samples")
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for sample in samples:
        grouped[sample.station].append({
            "members": sample.members,
            "centre": sample.gfs_max,
            "buckets": sample.spec.buckets,
            "winner": sample.winner,
        })

    global_fit = fit_calibration(
        [entry for group in grouped.values() for entry in group]
    )
    calibration: Dict[str, Calibration] = {"__global__": global_fit}
    for station, entries in grouped.items():
        if len(entries) >= min_samples:
            calibration[station] = fit_calibration(entries)
    return calibration


def calibration_for(calibration: Dict[str, Calibration],
                    station: str) -> Calibration:
    return calibration.get(station) or calibration["__global__"]


# ── Runner ───────────────────────────────────────────────────────────────────

def select_rows(rows: Sequence[dict], cities: Sequence[str],
                start: date, end: date) -> List[dict]:
    wanted = {city.lower() for city in cities}
    selected = []
    for row in rows:
        if row.get("city", "").lower() not in wanted:
            continue
        try:
            observed = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= observed <= end:
            selected.append(row)
    return selected


def run_gate(test_rows: Sequence[dict], prices: Dict[str, dict],
             train_rows: Optional[Sequence[dict]] = None,
             offset_hours: int = 24,
             members: Sequence[str] = DEFAULT_MEMBERS,
             product: str = DEFAULT_PRODUCT,
             cache_dir: str = DEFAULT_CACHE_DIR,
             workers: int = 12,
             use_gfs: bool = False,
             progress: bool = True,
             min_train_samples: int = DEFAULT_MIN_TRAIN_SAMPLES,
             kernel_sigma: float = 0.0,
             half_spread: float = 0.0,
             fee_rate: float = 0.05,
             fee: float = 0.0,
             edge_threshold: float = 0.0,
             edge_thresholds: Sequence[float] = DEFAULT_EDGE_THRESHOLDS,
             reliability_bins_n: int = DEFAULT_RELIABILITY_BINS) -> dict:
    print("collecting test samples", flush=True)
    test_samples = collect_samples(
        test_rows, prices, offset_hours=offset_hours, members=members,
        product=product, cache_dir=cache_dir, workers=workers,
        use_gfs=use_gfs, progress=progress,
    )

    market = [sample.market for sample in test_samples]
    winners = [sample.winner for sample in test_samples]
    variants: Dict[str, List[Dict[str, float]]] = {}
    if test_samples:
        variants["raw"] = [
            model_probabilities(sample, kernel_sigma=kernel_sigma)
            for sample in test_samples
        ]

    report: dict = {
        "offset_hours": offset_hours,
        "members": len(members),
        "product": product,
        "gfs_centre": use_gfs,
        "decisions": len(test_samples),
        "score": (skill_score(variants["raw"], market, winners)
                  if test_samples else {}),
    }

    if train_rows:
        print("collecting training samples", flush=True)
        train_samples = collect_samples(
            train_rows, prices, offset_hours=offset_hours, members=members,
            product=product, cache_dir=cache_dir, workers=workers,
            use_gfs=use_gfs, progress=progress,
        )
        if train_samples:
            calibration = fit_per_station(train_samples,
                                          min_samples=min_train_samples)
            report["training_samples"] = len(train_samples)
            report["calibration"] = {
                station: {
                    "bias": fit.bias,
                    "scale": fit.scale,
                    "sigma": fit.sigma,
                    "samples": fit.samples,
                }
                for station, fit in calibration.items()
            }
            variants["calibrated"] = [
                model_probabilities(sample,
                                    calibration_for(calibration, sample.station))
                for sample in test_samples
            ]
            report["score_calibrated"] = skill_score(
                variants["calibrated"], market, winners
            )
            report["edge_calibrated"] = edge_summary(variants["calibrated"],
                                                     market)

    if test_samples and use_gfs:
        # Fair intermediate baseline: hourly GFS centre, no fitted bias.
        centre_cal = Calibration(bias=0.0, scale=1.0,
                                 sigma=kernel_sigma or 0.75)
        variants["gfs_centre"] = [
            apply_calibration(sample.members, sample.gfs_max,
                              sample.spec.buckets, centre_cal)
            for sample in test_samples
        ]
        report["score_gfs_centre"] = skill_score(
            variants["gfs_centre"], market, winners
        )

    if test_samples:
        report["edge"] = edge_summary(variants["raw"], market)
        report["contracts"] = [
            {"slug": sample.slug, "date": sample.day.isoformat(),
             "station": sample.station, "cycle": sample.cycle,
             "gfs_max": sample.gfs_max}
            for sample in test_samples
        ]

        costs = CostModel(half_spread=half_spread, fee_rate=fee_rate, fee=fee)
        report["costs"] = {
            "half_spread": costs.half_spread,
            "fee_rate": costs.fee_rate,
            "fee": costs.fee,
            "fixed_per_share": costs.per_share,
        }
        report["reliability"] = {
            name: reliability_summary(probs, winners,
                                      n_bins=reliability_bins_n)
            for name, probs in variants.items()
        }
        report["reliability"]["market"] = reliability_summary(
            market, winners, n_bins=reliability_bins_n
        )
        report["after_cost"] = {
            name: after_cost_report(
                test_samples, probs, costs,
                threshold=edge_threshold,
                thresholds=edge_thresholds,
            )
            for name, probs in variants.items()
        }
    return report


def print_report(report: dict) -> None:
    def money(value: float) -> str:
        # Avoid printing "-0.00" for a trivially small loss.
        return f"{value:+.2f}" if abs(value) >= 0.005 else "0.00"

    def show(label: str, score: dict) -> None:
        if not score:
            return
        print(f"\n{label} (n={score['contracts']}):")
        print(f"  Brier model={score['brier_model']:.4f} "
              f"market={score['brier_market']:.4f} "
              f"BSS={score['brier_skill_score']:+.3f}")
        print(f"  log-loss model={score['log_loss_model']:.4f} "
              f"market={score['log_loss_market']:.4f}")
        print(f"  mean P(winner): model={score['mean_p_winner_model']:.3f} "
              f"market={score['mean_p_winner_market']:.3f}")

    print(f"\nDecision T-{report['offset_hours']}h | "
          f"{report['members']} members | {report['product']} | "
          f"gfs_centre={report.get('gfs_centre')}")
    show("Raw GEFS ensemble", report.get("score") or {})
    show("GFS hourly centre (no fitted bias)",
         report.get("score_gfs_centre") or {})
    if report.get("score_calibrated"):
        show("Calibrated (train → test)", report["score_calibrated"])
    if report.get("calibration"):
        print("\n  calibration:", json.dumps(report["calibration"], indent=2))
    for key in ("edge", "edge_calibrated"):
        edge = report.get(key) or {}
        if edge.get("observations"):
            print(f"  {key}: mean={edge['mean_edge']:+.3f} "
                  f"mean|edge|={edge['mean_abs_edge']:.3f} "
                  f"share>2¢={edge['share_edge_gt_threshold']:.1%}")

    reliability = report.get("reliability") or {}
    if reliability:
        print("\n  Reliability over all bucket probabilities (lower ECE better):")
        for name in ("raw", "gfs_centre", "calibrated", "market"):
            summary = reliability.get(name) or {}
            if not summary:
                continue
            print(f"    {name:<12} ECE={summary['ece']:.4f} "
                  f"(n={summary['observations']}, "
                  f"{summary['n_bins']} bins)")
        for name in ("calibrated", "market"):
            bins = (reliability.get(name) or {}).get("bins") or []
            if not bins:
                continue
            print(f"    {name} diagram (predicted → observed):")
            for row in bins:
                print(f"      [{row['lower']:.1f}, {row['upper']:.1f}) "
                      f"n={row['count']:>5} "
                      f"pred={row['mean_predicted']:.3f} "
                      f"obs={row['observed_frequency']:.3f}")

    after_cost = report.get("after_cost") or {}
    if after_cost:
        costs = report.get("costs") or {}
        print("\n  After-cost taker PnL "
              f"(half-spread={costs.get('half_spread', 0.0) * 100:.2f}¢, "
              f"fee={costs.get('fee_rate', 0.0):.3f}·p(1-p) + "
              f"{costs.get('fee', 0.0) * 100:.2f}¢/share):")
        for name in ("raw", "gfs_centre", "calibrated"):
            entry = after_cost.get(name) or {}
            summary = entry.get("summary") or {}
            if not summary:
                continue
            print(f"    {name:<12} trades={summary['trades']:>5} "
                  f"({entry.get('contract_share', 0.0):.0%} of contracts) "
                  f"hit={summary['hit_rate']:.1%} "
                  f"mean edge={summary['mean_edge'] * 100:+.2f}¢ "
                  f"PnL={money(summary['pnl'])} "
                  f"ROI={summary['roi']:+.1%}")
            levels = ", ".join(
                f">{row['threshold'] * 100:.0f}¢: "
                f"{row['summary']['trades']}t/"
                f"{money(row['summary']['pnl'])}"
                for row in entry.get("thresholds") or []
            )
            print(f"      threshold sensitivity: {levels}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1 gate: model vs market.")
    parser.add_argument("--rows", default=DEFAULT_ROWS_PATH)
    parser.add_argument("--prices", default=DEFAULT_PRICES_PATH)
    parser.add_argument("--cities", default="London")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--train-start", default=None)
    parser.add_argument("--train-end", default=None)
    parser.add_argument("--offset-hours", type=int, default=24)
    parser.add_argument("--members", type=int, default=len(DEFAULT_MEMBERS))
    parser.add_argument("--product", default=DEFAULT_PRODUCT,
                        choices=("0p25", "0p50"))
    parser.add_argument("--gfs-centre", action="store_true",
                        help="use hourly GFS daily max as the ensemble centre")
    parser.add_argument("--kernel-sigma", type=float, default=0.0,
                        help="kernel smoothing for the uncalibrated score")
    parser.add_argument("--min-train-samples", type=int,
                        default=DEFAULT_MIN_TRAIN_SAMPLES)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--half-spread", type=float, default=0.0,
                        help="cost of crossing the spread, per share in "
                             "probability units (0.01 = 1¢); the ask is "
                             "approximated as mid + half_spread")
    parser.add_argument("--fee-rate", type=float, default=0.05,
                        help="Polymarket taker fee rate; per-share fee is "
                             "fee_rate * p * (1-p). 0.05 for weather "
                             "markets per docs.polymarket.com/trading/fees "
                             "(read 2026-11-08); set 0 to disable")
    parser.add_argument("--fee", type=float, default=0.0,
                        help="extra flat per-share cost in probability "
                             "units (0.002 = 0.2¢)")
    parser.add_argument("--edge-threshold", type=float, default=0.0,
                        help="minimum after-cost edge to take a trade")
    parser.add_argument("--edge-thresholds", default="0,0.01,0.02,0.03",
                        help="comma-separated thresholds for the PnL "
                             "sensitivity table")
    parser.add_argument("--reliability-bins", type=int,
                        default=DEFAULT_RELIABILITY_BINS)
    parser.add_argument("--out", default=DEFAULT_REPORT_PATH)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    if bool(args.train_start) != bool(args.train_end):
        parser.error("--train-start and --train-end must be given together")
    if args.half_spread < 0 or args.fee < 0 or args.fee_rate < 0:
        parser.error("--half-spread, --fee and --fee-rate must be non-negative")
    if args.reliability_bins < 1:
        parser.error("--reliability-bins must be >= 1")
    try:
        thresholds = tuple(float(value.strip())
                           for value in args.edge_thresholds.split(",")
                           if value.strip())
    except ValueError:
        parser.error("--edge-thresholds must be comma-separated numbers")

    rows = load_jsonl(args.rows)
    prices = market_prices_by_slug(load_jsonl(args.prices))
    cities = [city.strip() for city in args.cities.split(",") if city.strip()]

    test_rows = select_rows(rows, cities, date.fromisoformat(args.start),
                            date.fromisoformat(args.end))
    train_rows = None
    if args.train_start:
        train_rows = select_rows(rows, cities,
                                 date.fromisoformat(args.train_start),
                                 date.fromisoformat(args.train_end))
        # Never train on the test window.
        test_slugs = {row["slug"] for row in test_rows}
        train_rows = [row for row in train_rows
                      if row["slug"] not in test_slugs]
    if args.limit:
        test_rows = test_rows[:args.limit]
    if not test_rows:
        parser.error("no contracts matched the selection")

    report = run_gate(
        test_rows, prices, train_rows=train_rows,
        offset_hours=args.offset_hours,
        members=DEFAULT_MEMBERS[:max(1, args.members)],
        product=args.product,
        cache_dir=args.cache_dir,
        workers=args.workers,
        use_gfs=args.gfs_centre,
        min_train_samples=args.min_train_samples,
        kernel_sigma=args.kernel_sigma,
        half_spread=args.half_spread,
        fee_rate=args.fee_rate,
        fee=args.fee,
        edge_threshold=args.edge_threshold,
        edge_thresholds=thresholds,
        reliability_bins_n=args.reliability_bins,
    )
    print_report(report)
    directory = os.path.dirname(args.out)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=str)
    print(f"\nReport written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
