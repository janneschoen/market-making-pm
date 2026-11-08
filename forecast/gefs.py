"""
Point-in-time GEFS ensemble fetch from the NOAA archive.

Open-Meteo's ensemble archive is not usable for a historical gate (the
`past_days` axis exists, but values are null beyond the last few days), so
forecasts come from the raw NOAA GEFS archive on S3:

    s3://noaa-gefs-pds/gefs.YYYYMMDD/HH/atmos/pgrb2sp25/<member>.tHHz.pgrb2s.0p25.fNNN

Each member/lead-time file is a few hundred MB of GRIB2, so we read the
`.idx` sidecar, find the `TMP:2 m above ground` message, and fetch only that
byte range. A single message is decoded with pygrib and reduced to the
nearest gridpoint of each requested station.

The run used is always ``latest_available_cycle(as_of)`` — the newest cycle
whose ~4 h publication lag had elapsed at the decision time — never the
latest cycle that exists today (ROADMAP §5).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pygrib

from forecast.ensemble import latest_available_cycle, publication_time
from forecast.station import Station, resolve_station
from forecast.buckets import Bucket, bucket_probabilities

S3_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
GFS_S3_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
GFS_MAX_LEAD_HOURS = 120

# product key → (S3 directory, file stem, grid spacing in degrees)
PRODUCTS: Dict[str, Tuple[str, str, float]] = {
    "0p25": ("pgrb2sp25", "pgrb2s.0p25", 0.25),
    "0p50": ("pgrb2ap5", "pgrb2a.0p50", 0.50),
}
DEFAULT_PRODUCT = "0p25"
CONTROL_MEMBER = "gec00"
PERTURBED_MEMBERS = tuple(f"gep{index:02d}" for index in range(1, 31))
DEFAULT_MEMBERS = (CONTROL_MEMBER,) + PERTURBED_MEMBERS

DEFAULT_CACHE_DIR = os.path.join("logs", "cache", "gefs")
STEP_HOURS = 3          # GEFS 0.25°/0.5° TMP:2m is 3-hourly
MAX_LEAD_HOURS = 240
REQUEST_TIMEOUT_S = 60
TMP2M_KEY = "TMP:2 m above ground"


@dataclass(frozen=True)
class Point:
    """A station's coordinates, used as a cache key."""

    latitude: float
    longitude: float

    @property
    def key(self) -> str:
        return f"{self.latitude:.3f},{self.longitude:.3f}"


def points_for(stations: Iterable[Station]) -> List[Point]:
    return [Point(s.latitude, s.longitude) for s in stations
            if s.latitude is not None and s.longitude is not None]


# ── Lead times ───────────────────────────────────────────────────────────────

def forecast_hours(cycle: datetime, local_start: datetime,
                   local_end: datetime) -> List[int]:
    """
    GEFS lead hours covering a station-local settlement day.

    3-hourly sampling means the true daily maximum can sit between samples;
    the resulting cold bias is a documented limitation, corrected only by
    calibration (ROADMAP §5), never by ad-hoc fudging here.
    """
    first = max(0, math.ceil((local_start - cycle).total_seconds() / 3600))
    last = math.floor((local_end - cycle).total_seconds() / 3600)
    first = math.ceil(first / STEP_HOURS) * STEP_HOURS
    last = math.floor(last / STEP_HOURS) * STEP_HOURS
    hours = list(range(first, last + 1, STEP_HOURS))
    hours = [h for h in hours if 0 <= h <= MAX_LEAD_HOURS]
    if not hours:
        raise ValueError(
            f"no GEFS lead times for cycle {cycle.isoformat()} covering "
            f"{local_start.isoformat()}..{local_end.isoformat()}"
        )
    return hours


def local_day_window(day: date, timezone_name: str) -> Tuple[datetime, datetime]:
    """UTC window of the station's local settlement day."""
    tz = ZoneInfo(timezone_name)
    start = datetime.combine(day, dt_time(0, 0), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), dt_time(0, 0), tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def decision_time(day: date, timezone_name: str, offset_hours: int) -> datetime:
    """`offset_hours` before the station's local midnight (ROADMAP decision)."""
    start, _ = local_day_window(day, timezone_name)
    return start - timedelta(hours=offset_hours)


# ── S3 message access ────────────────────────────────────────────────────────

def message_url(cycle: datetime, member: str, fxx: int,
                product: str = DEFAULT_PRODUCT) -> str:
    directory, stem, _ = PRODUCTS[product]
    return (
        f"{S3_BASE}/gefs.{cycle:%Y%m%d}/{cycle:%H}/atmos/{directory}/"
        f"{member}.t{cycle:%H}z.{stem}.f{fxx:03d}"
    )


def _open(url: str, headers: Optional[dict] = None, timeout: int = REQUEST_TIMEOUT_S):
    request = urllib.request.Request(
        url, headers={"User-Agent": "market-making-pm/gefs", **(headers or {})}
    )
    return urllib.request.urlopen(request, timeout=timeout)


def message_byte_range(url: str, key: str = TMP2M_KEY) -> Optional[Tuple[int, int]]:
    """Byte range of the `key` message from the GRIB2 `.idx` sidecar."""
    try:
        with _open(url + ".idx") as response:
            lines = response.read().decode("utf-8", "replace").splitlines()
    except (urllib.error.URLError, OSError):
        return None

    for index, line in enumerate(lines):
        if key in line:
            try:
                start = int(line.split(":")[1])
            except (IndexError, ValueError):
                return None
            end = ""
            if index + 1 < len(lines):
                try:
                    end = int(lines[index + 1].split(":")[1]) - 1
                except (IndexError, ValueError):
                    end = ""
            return start, end
    return None


def download_message(url: str) -> Optional[bytes]:
    """Download only the TMP:2m message of one GEFS file."""
    byte_range = message_byte_range(url)
    if byte_range is None:
        return None
    start, end = byte_range
    value = f"bytes={start}-{end}" if end != "" else f"bytes={start}-"
    for attempt in range(3):
        try:
            with _open(url, {"Range": value}) as response:
                return response.read()
        except (urllib.error.URLError, OSError):
            if attempt == 2:
                return None
    return None


def decode_points(blob: bytes, points: Sequence[Point]) -> Dict[str, float]:
    """Nearest-gridpoint 2 m temperature (°C) for each point."""
    grib = pygrib.fromstring(blob)
    values = grib.values
    lats, lons = grib.latlons()
    lat_axis = lats[:, 0]
    lon_axis = lons[0, :]

    out: Dict[str, float] = {}
    for point in points:
        row = int(abs(lat_axis - point.latitude).argmin())
        col = int(abs(lon_axis - (point.longitude % 360.0)).argmin())
        out[point.key] = float(values[row, col]) - 273.15
    return out


# ── Cache + fetch ────────────────────────────────────────────────────────────

def cache_path(cache_dir: str, product: str, cycle: datetime,
               member: str, fxx: int) -> str:
    return os.path.join(
        cache_dir, product, f"{cycle:%Y%m%d%H}", f"{member}-f{fxx:03d}.json"
    )


def _load_cache(path: str) -> Dict[str, float]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return {str(k): float(v) for k, v in (payload.get("points") or {}).items()}
    except (OSError, ValueError, TypeError):
        return {}


def _save_cache(path: str, cycle: datetime, member: str, fxx: int,
                points: Dict[str, float]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "cycle": cycle.isoformat(),
                    "member": member,
                    "fxx": fxx,
                    "points": points,
                },
                handle,
            )
    except OSError:
        pass


def fetch_ensemble(cycle: datetime, hours: Sequence[int], points: Sequence[Point],
                   members: Sequence[str] = DEFAULT_MEMBERS,
                   product: str = DEFAULT_PRODUCT,
                   cache_dir: str = DEFAULT_CACHE_DIR,
                   workers: int = 12,
                   progress: bool = False) -> Dict[Tuple[str, int], Dict[str, float]]:
    """
    `{(member, fxx): {point_key: temp_c}}`, using the on-disk cache.

    Messages are global, so one download serves every requested station; the
    cache stores per-point scalars and is merged when new points appear.
    """
    plan: List[Tuple[str, int, str, Dict[str, float]]] = []
    for member in members:
        for fxx in hours:
            path = cache_path(cache_dir, product, cycle, member, fxx)
            cached = _load_cache(path)
            if all(point.key in cached for point in points):
                plan.append((member, fxx, path, cached))
            else:
                plan.append((member, fxx, path, {}))

    results: Dict[Tuple[str, int], Dict[str, float]] = {}
    missing = [entry for entry in plan if not entry[3]]
    for member, fxx, _path, cached in plan:
        if cached:
            results[(member, fxx)] = cached

    def work(member: str, fxx: int, path: str) -> Tuple[str, int, str, Dict[str, float]]:
        blob = download_message(message_url(cycle, member, fxx, product))
        if blob is None:
            return member, fxx, path, {}
        try:
            values = decode_points(blob, points)
        except Exception:  # noqa: BLE001 - one corrupt message must not stop the run
            return member, fxx, path, {}
        return member, fxx, path, values

    if missing:
        started = time.time()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [
                pool.submit(work, member, fxx, path)
                for member, fxx, path, _cached in missing
            ]
            for index, future in enumerate(as_completed(futures), start=1):
                member, fxx, path, values = future.result()
                if not values:
                    results[(member, fxx)] = {}
                    continue
                merged = _load_cache(path)
                merged.update(values)
                _save_cache(path, cycle, member, fxx, merged)
                results[(member, fxx)] = merged
                if progress and index % 200 == 0:
                    rate = index / max(time.time() - started, 1e-9)
                    print(f"  gefs {cycle:%Y-%m-%d %H}z: {index}/{len(missing)} "
                          f"messages ({rate:.1f}/s)", flush=True)

    return results


# ── Daily maxima / probabilities ────────────────────────────────────────────

def member_daily_maxima(day: date, timezone_name: str,
                        stations: Sequence[Station],
                        offset_hours: int = 24,
                        members: Sequence[str] = DEFAULT_MEMBERS,
                        product: str = DEFAULT_PRODUCT,
                        cache_dir: str = DEFAULT_CACHE_DIR,
                        workers: int = 12) -> Dict:
    """
    Per-member daily maxima for each station and the cycle that was available
    `offset_hours` before the local settlement day began.

    Returns `{"cycle", "as_of", "hours", "maxima": {member: {point: temp}}}`
    where `maxima` keys are `Point.key` strings.
    """
    decision = decision_time(day, timezone_name, offset_hours)
    cycle = latest_available_cycle(decision)
    start, end = local_day_window(day, timezone_name)
    hours = forecast_hours(cycle, start, end)
    points = points_for(stations)
    values = fetch_ensemble(cycle, hours, points, members=members,
                           product=product, cache_dir=cache_dir, workers=workers)

    maxima: Dict[str, Dict[str, float]] = {}
    for member in members:
        per_point: Dict[str, List[float]] = {point.key: [] for point in points}
        for fxx in hours:
            for key, temp in (values.get((member, fxx)) or {}).items():
                if key in per_point:
                    per_point[key].append(temp)
        maxima[member] = {
            key: max(temps) for key, temps in per_point.items() if temps
        }

    return {
        "cycle": cycle,
        "as_of": decision,
        "published_at": publication_time(cycle),
        "hours": hours,
        "maxima": maxima,
    }


def bucket_probabilities_for(spec_buckets: Sequence[Bucket],
                             member_maxima_c: Sequence[float]) -> Dict[str, float]:
    """Empirical P(bucket) from member maxima (°C) using contract buckets."""
    return bucket_probabilities(member_maxima_c, spec_buckets)


def station_key(station: Station) -> str:
    return station.icao or station.name or station.city


# ── GFS deterministic archive (hourly centre) ───────────────────────────────

def gfs_message_url(cycle: datetime, fxx: int) -> str:
    return (
        f"{GFS_S3_BASE}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/"
        f"gfs.t{cycle:%H}z.pgrb2.0p25.f{fxx:03d}"
    )


def hourly_forecast_hours(cycle: datetime, local_start: datetime,
                          local_end: datetime) -> List[int]:
    """Hourly GFS leads covering the station-local settlement day."""
    first = max(1, math.ceil((local_start - cycle).total_seconds() / 3600))
    last = math.floor((local_end - cycle).total_seconds() / 3600)
    hours = [hour for hour in range(first, last + 1)
             if hour <= GFS_MAX_LEAD_HOURS]
    if not hours:
        raise ValueError(
            f"no GFS lead times for cycle {cycle.isoformat()} covering "
            f"{local_start.isoformat()}..{local_end.isoformat()}"
        )
    return hours


def gfs_cache_path(cache_dir: str, cycle: datetime, fxx: int) -> str:
    return os.path.join(cache_dir, "gfs", f"{cycle:%Y%m%d%H}", f"f{fxx:03d}.json")


def fetch_gfs_points(cycle: datetime, hours: Sequence[int],
                     points: Sequence[Point],
                     cache_dir: str = DEFAULT_CACHE_DIR,
                     workers: int = 12,
                     progress: bool = False) -> Dict[int, Dict[str, float]]:
    """Deterministic GFS 2 m temperatures, keyed by lead hour."""
    results: Dict[int, Dict[str, float]] = {}
    plan: List[Tuple[int, str, Dict[str, float]]] = []
    for fxx in hours:
        path = gfs_cache_path(cache_dir, cycle, fxx)
        cached = _load_cache(path)
        plan.append((fxx, path, cached if all(p.key in cached for p in points)
                     else {}))
    for fxx, _path, cached in plan:
        if cached:
            results[fxx] = cached
    missing = [entry for entry in plan if not entry[2]]

    def work(fxx: int, path: str) -> Tuple[int, str, Dict[str, float]]:
        blob = download_message(gfs_message_url(cycle, fxx))
        if blob is None:
            return fxx, path, {}
        try:
            return fxx, path, decode_points(blob, points)
        except Exception:  # noqa: BLE001
            return fxx, path, {}

    if missing:
        started = time.time()
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(work, fxx, path) for fxx, path, _c in missing]
            for index, future in enumerate(as_completed(futures), start=1):
                fxx, path, values = future.result()
                if not values:
                    results[fxx] = {}
                    continue
                merged = _load_cache(path)
                merged.update(values)
                _save_cache(path, cycle, "gfs", fxx, merged)
                results[fxx] = merged
                if progress and index % 200 == 0:
                    rate = index / max(time.time() - started, 1e-9)
                    print(f"  gfs {cycle:%Y-%m-%d %H}z: {index}/{len(missing)} "
                          f"messages ({rate:.1f}/s)", flush=True)
    return results


def deterministic_daily_maxima_for_day(
    day: date, entries: Sequence[Tuple[Station, str]], offset_hours: int = 24,
    cache_dir: str = DEFAULT_CACHE_DIR, workers: int = 12,
    progress: bool = False,
) -> Dict[str, dict]:
    """Hourly GFS daily maxima per station (one cycle per timezone group)."""
    groups: Dict[datetime, List[Tuple[Station, datetime, List[int]]]] = {}
    for station, timezone_name in entries:
        decision = decision_time(day, timezone_name, offset_hours)
        cycle = latest_available_cycle(decision)
        start, end = local_day_window(day, timezone_name)
        hours = hourly_forecast_hours(cycle, start, end)
        groups.setdefault(cycle, []).append((station, decision, hours))

    out: Dict[str, dict] = {}
    for cycle, group in groups.items():
        all_hours = sorted({hour for _s, _d, hours in group for hour in hours})
        stations = [station for station, _d, _h in group]
        points = points_for(stations)
        values = fetch_gfs_points(cycle, all_hours, points,
                                  cache_dir=cache_dir, workers=workers,
                                  progress=progress)
        for station, decision, hours in group:
            point_key = Point(station.latitude, station.longitude).key
            temps = [
                (values.get(hour) or {}).get(point_key) for hour in hours
            ]
            temps = [temp for temp in temps if temp is not None]
            out[station_key(station)] = {
                "cycle": cycle,
                "as_of": decision,
                "published_at": publication_time(cycle),
                "hours": hours,
                "max": max(temps) if temps else None,
            }
    return out


def daily_maxima_for_day(day: date,
                         entries: Sequence[Tuple[Station, str]],
                         offset_hours: int = 24,
                         members: Sequence[str] = DEFAULT_MEMBERS,
                         product: str = DEFAULT_PRODUCT,
                         cache_dir: str = DEFAULT_CACHE_DIR,
                         workers: int = 12,
                         progress: bool = False) -> Dict[str, dict]:
    """
    Per-member daily maxima for several stations on one day.

    Entries are `(station, timezone_name)`; stations resolving to the same
    forecast cycle share their downloads (one global message serves all).

    Returns `{station_key: {"cycle", "as_of", "hours", "maxima": {member: t}}}`.
    """
    groups: Dict[datetime, List[Tuple[Station, datetime, List[int]]]] = {}
    for station, timezone_name in entries:
        decision = decision_time(day, timezone_name, offset_hours)
        cycle = latest_available_cycle(decision)
        start, end = local_day_window(day, timezone_name)
        hours = forecast_hours(cycle, start, end)
        groups.setdefault(cycle, []).append((station, decision, hours))

    out: Dict[str, dict] = {}
    for cycle, group in groups.items():
        all_hours = sorted({hour for _s, _d, hours in group for hour in hours})
        stations = [station for station, _d, _h in group]
        points = points_for(stations)
        values = fetch_ensemble(
            cycle, all_hours, points, members=members, product=product,
            cache_dir=cache_dir, workers=workers, progress=progress,
        )
        for station, decision, hours in group:
            point_key = Point(station.latitude, station.longitude).key
            maxima: Dict[str, float] = {}
            for member in members:
                temps = [
                    (values.get((member, hour)) or {}).get(point_key)
                    for hour in hours
                ]
                temps = [temp for temp in temps if temp is not None]
                if temps:
                    maxima[member] = max(temps)
            out[station_key(station)] = {
                "cycle": cycle,
                "as_of": decision,
                "published_at": publication_time(cycle),
                "hours": hours,
                "maxima": maxima,
            }
    return out


# ── CLI probe ────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe one station-day of the GEFS archive."
    )
    parser.add_argument("--city", default="London")
    parser.add_argument("--date", required=True, help="observation date (UTC)")
    parser.add_argument("--offset-hours", type=int, default=24,
                        help="hours before local midnight (default 24)")
    parser.add_argument("--product", default=DEFAULT_PRODUCT,
                        choices=sorted(PRODUCTS))
    parser.add_argument("--members", type=int, default=len(DEFAULT_MEMBERS))
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--labels", default=None,
                        help="comma-separated bucket labels to price")
    args = parser.parse_args(argv)

    station = resolve_station(args.city)
    day = date.fromisoformat(args.date)
    members = DEFAULT_MEMBERS[:max(1, args.members)]

    result = member_daily_maxima(
        day, station.timezone, [station],
        offset_hours=args.offset_hours, members=members,
        product=args.product, cache_dir=args.cache_dir, workers=args.workers,
    )
    maxima = sorted(
        next(iter(per.values()))
        for per in result["maxima"].values() if per
    )
    print(f"cycle {result['cycle'].isoformat()} (published "
          f"{result['published_at'].isoformat()}), as_of "
          f"{result['as_of'].isoformat()}, leads {result['hours']}")
    print(f"members: {len(maxima)} | min={min(maxima):.1f} "
          f"median={maxima[len(maxima)//2]:.1f} max={max(maxima):.1f} °C")

    if args.labels:
        buckets = [Bucket.parse(label, unit="C")
                   for label in args.labels.split(",")]
        from forecast.buckets import validate_partition
        validate_partition(buckets)
        probs = bucket_probabilities_for(buckets, maxima)
        for label, probability in probs.items():
            print(f"  {label:<16} {probability:6.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
