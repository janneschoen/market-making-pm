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

DEFAULT_ROWS_PATH = os.path.join("logs", "history", "resolved_events.jsonl")
DEFAULT_PRICES_PATH = os.path.join("logs", "history", "market_prices.jsonl")
DEFAULT_REPORT_PATH = os.path.join("logs", "history", "gate_report.json")
PROBABILITY_FLOOR = 1e-6
DEFAULT_MIN_TRAIN_SAMPLES = 15


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
             kernel_sigma: float = 0.0) -> dict:
    print("collecting test samples", flush=True)
    test_samples = collect_samples(
        test_rows, prices, offset_hours=offset_hours, members=members,
        product=product, cache_dir=cache_dir, workers=workers,
        use_gfs=use_gfs, progress=progress,
    )

    report: dict = {
        "offset_hours": offset_hours,
        "members": len(members),
        "product": product,
        "gfs_centre": use_gfs,
        "decisions": len(test_samples),
        "score": (score_samples(test_samples, kernel_sigma=kernel_sigma)
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
            calibrated_model = [
                model_probabilities(sample,
                                    calibration_for(calibration, sample.station))
                for sample in test_samples
            ]
            market = [sample.market for sample in test_samples]
            winners = [sample.winner for sample in test_samples]
            report["score_calibrated"] = skill_score(
                calibrated_model, market, winners
            )
            report["edge_calibrated"] = edge_summary(calibrated_model, market)

    if test_samples and use_gfs:
        # Fair intermediate baseline: hourly GFS centre, no fitted bias.
        centre_cal = Calibration(bias=0.0, scale=1.0,
                                 sigma=kernel_sigma or 0.75)
        centre_model = [
            apply_calibration(sample.members, sample.gfs_max,
                              sample.spec.buckets, centre_cal)
            for sample in test_samples
        ]
        report["score_gfs_centre"] = skill_score(
            centre_model,
            [sample.market for sample in test_samples],
            [sample.winner for sample in test_samples],
        )

    if test_samples:
        raw_model = [model_probabilities(sample, kernel_sigma=kernel_sigma)
                     for sample in test_samples]
        report["edge"] = edge_summary(raw_model,
                                      [sample.market for sample in test_samples])
        report["contracts"] = [
            {"slug": sample.slug, "date": sample.day.isoformat(),
             "station": sample.station, "cycle": sample.cycle,
             "gfs_max": sample.gfs_max}
            for sample in test_samples
        ]
    return report


def print_report(report: dict) -> None:
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
    parser.add_argument("--out", default=DEFAULT_REPORT_PATH)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    if bool(args.train_start) != bool(args.train_end):
        parser.error("--train-start and --train-end must be given together")

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
