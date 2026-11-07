"""
Phase 1 historical gate (ROADMAP §9).

This module builds the resolved-contract dataset the gate is scored on:

  1. harvest closed weather events per city series from Gamma,
  2. parse each contract's own resolution spec (station, unit, rounding,
     source family) and validate its buckets as a partition,
  3. extract the settled bucket and per-bucket token IDs.

It deliberately stops there: price history and point-in-time forecasts are the
next layer, and must not be mixed into the same pass (alpha vs execution vs
data, ROADMAP §3).

Usage:
    python -m eval.historical                        # probe, no writes
    python -m eval.historical --out logs/history/resolved_events.jsonl
    python -m eval.historical --cities London,NYC --limit-per-city 5
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from forecast.buckets import BucketParityError
from forecast.contracts import ContractSpec, parse_contract_spec

GAMMA_URL = "https://gamma-api.polymarket.com"
DEFAULT_CACHE_DIR = os.path.join("logs", "cache", "gamma_events")
DEFAULT_OUT_PATH = os.path.join("logs", "history", "resolved_events.jsonl")
DEFAULT_SINCE = "2025-01-01"
DEFAULT_CONFIG = "config.json"
PAGE_SIZE = 100          # Gamma caps list responses at 100
REQUEST_TIMEOUT_S = 25
REQUEST_SLEEP_S = 0.15
SETTLED_THRESHOLD = 0.5

_MONTHS = {
    name: number
    for number, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}

_DESC_DATE_RE = re.compile(r"on (\d{1,2}) ([A-Za-z]{3,9}) '?(\d{2,4})")
_SLUG_DATE_RE = re.compile(
    r"-on-([a-z]+)-(\d{1,2})(?:-(\d{4}))?$", re.IGNORECASE
)


# ── City / series mapping ────────────────────────────────────────────────────

def series_for(city: str) -> str:
    """Gamma series slug for a city (`NYC` → `nyc-daily-weather`)."""
    return f"{city.strip().lower().replace(' ', '-')}-daily-weather"


def default_cities(config_path: str = DEFAULT_CONFIG) -> List[str]:
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            return list(json.load(handle).get("locations") or [])
    except (OSError, ValueError):
        return []


# ── HTTP + cache ─────────────────────────────────────────────────────────────

def _cache_path(cache_dir: str, series: str, offset: int) -> str:
    return os.path.join(cache_dir, f"{series}-{offset}.json")


def fetch_page(series: str, offset: int, cache_dir: str,
               refresh: bool = False) -> List[dict]:
    """One Gamma events page, cached on disk (discovery is request-hungry)."""
    path = _cache_path(cache_dir, series, offset)
    if not refresh and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                return payload
        except (OSError, ValueError):
            pass

    params = {
        "series_slug": series,
        "closed": "true",
        "limit": PAGE_SIZE,
        "offset": offset,
        "order": "endDate",
        "ascending": "false",
    }
    url = f"{GAMMA_URL}/events?{urllib.parse.urlencode(params)}"

    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "market-making-pm/eval"}
            )
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_S
            ) as response:
                page = json.load(response)
            break
        except Exception:  # noqa: BLE001 - network layer, retry then give up
            if attempt == 2:
                return []
            time.sleep(1.5 * (attempt + 1))
    else:  # pragma: no cover - loop always breaks or returns
        return []

    if not isinstance(page, list):
        return []

    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(page, handle)
    except OSError:
        pass
    return page


def iter_series_events(series: str, since: Optional[date] = None,
                       cache_dir: str = DEFAULT_CACHE_DIR,
                       refresh: bool = False,
                       limit: Optional[int] = None) -> Iterator[dict]:
    """Yield closed events for `series`, newest first, down to `since`."""
    since = since or date.fromisoformat(DEFAULT_SINCE)
    offset = 0
    yielded = 0

    while True:
        page = fetch_page(series, offset, cache_dir, refresh=refresh)
        if not page:
            return

        reached_cutoff = False
        for event in page:
            observed = observation_date(event)
            if observed is None:
                continue
            if observed < since:
                reached_cutoff = True
                continue
            yield event
            yielded += 1
            if limit is not None and yielded >= limit:
                return

        if reached_cutoff or len(page) < PAGE_SIZE:
            return
        offset += len(page)
        time.sleep(REQUEST_SLEEP_S)


# ── Event parsing ────────────────────────────────────────────────────────────

def _description(event: dict) -> str:
    markets = event.get("markets") or []
    return (markets[0].get("description") or "") if markets else ""


def observation_date(event: dict) -> Optional[date]:
    """
    The local observation date the contract settles on.

    Taken from the resolution description ("on 4 Dec '25"), validated against
    the event's own slug, and only then from `endDate` (12:00Z on that date).
    """
    description = _description(event)

    match = _DESC_DATE_RE.search(description)
    if match:
        day, month_name, year = match.groups()
        month = _MONTHS.get(month_name[:3].lower())
        if month:
            parsed_year = int(year)
            if parsed_year < 100:
                parsed_year += 2000
            try:
                return date(parsed_year, month, int(day))
            except ValueError:
                pass

    match = _SLUG_DATE_RE.search(event.get("slug", ""))
    if match:
        month_name, day, year = match.groups()
        month = _MONTHS.get(month_name[:3].lower())
        if month:
            if year:
                parsed_year = int(year)
            else:
                end = event.get("endDate") or ""
                try:
                    parsed_year = datetime.fromisoformat(
                        end.replace("Z", "+00:00")
                    ).year
                except ValueError:
                    return None
            try:
                return date(parsed_year, month, int(day))
            except ValueError:
                return None

    end = event.get("endDate") or ""
    try:
        return datetime.fromisoformat(
            end.replace("Z", "+00:00")
        ).astimezone(timezone.utc).date()
    except ValueError:
        return None


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value or [])


def settled_bucket(event: dict) -> Tuple[str, int]:
    """
    Return `(label, index)` of the single bucket that resolved YES.

    Raises `ValueError` for anything other than exactly one settled outcome —
    trading a contract whose settlement is ambiguous is a parity failure.
    """
    winners: List[Tuple[str, int]] = []
    for index, market in enumerate(event.get("markets") or []):
        prices = _json_list(market.get("outcomePrices"))
        try:
            yes_price = float(prices[0])
        except (IndexError, TypeError, ValueError):
            continue
        if yes_price > SETTLED_THRESHOLD:
            winners.append((market.get("groupItemTitle", ""), index))

    if len(winners) != 1:
        raise ValueError(f"expected 1 settled bucket, found {len(winners)}")
    return winners[0]


def spec_for(event: dict, city: str) -> ContractSpec:
    """Parse and validate the contract specification from the event itself."""
    markets = event.get("markets") or []
    labels = [market.get("groupItemTitle", "") for market in markets]
    description = _description(event)
    resolution_source = (
        event.get("resolutionSource")
        or (markets[0].get("resolutionSource") if markets else "")
        or ""
    )
    return parse_contract_spec(
        labels,
        description=description,
        resolution_source=resolution_source,
        city=city,
        series=event.get("seriesSlug") or "",
    )


def dataset_row(event: dict, city: str, spec: ContractSpec,
                winner: str, winner_index: int) -> dict:
    """One JSONL row: enough to join prices and forecasts later."""
    markets = []
    for market in event.get("markets") or []:
        markets.append({
            "label": market.get("groupItemTitle", ""),
            "condition_id": market.get("conditionId"),
            "token_ids": _json_list(market.get("clobTokenIds")),
            "settled": _json_list(market.get("outcomePrices")),
        })

    observed = observation_date(event)
    return {
        "slug": event.get("slug"),
        "city": city,
        "series": spec.series,
        "date": observed.isoformat() if observed else None,
        "volume": float(event.get("volume") or 0.0),
        "winner": winner,
        "winner_index": winner_index,
        "n_buckets": len(markets),
        "spec": {
            "station": spec.station_name,
            "icao": spec.icao,
            "timezone": spec.timezone,
            "unit": spec.unit,
            "rounding": spec.rounding.value if spec.rounding else None,
            "source": spec.source,
            "signature": spec.signature,
            "labels": list(spec.labels),
            "widths": list(spec.bucket_widths),
            "station_mismatch": spec.station_mismatch,
        },
        "markets": markets,
    }


# ── Harvest ──────────────────────────────────────────────────────────────────

def harvest(cities: Sequence[str], since: Optional[date] = None,
            cache_dir: str = DEFAULT_CACHE_DIR,
            limit_per_city: Optional[int] = None,
            refresh: bool = False) -> Tuple[List[dict], dict]:
    """Return `(rows, summary)` for the resolved-contract dataset."""
    since = since or date.fromisoformat(DEFAULT_SINCE)
    rows: List[dict] = []
    summary: Dict[str, Any] = {
        "since": since.isoformat(),
        "cities": {},
        "signatures": Counter(),
        "parity_failures": [],
        "settlement_failures": [],
        "missing_timezone": Counter(),
        "missing_station": Counter(),
        "station_mismatches": [],
    }

    for city in cities:
        series = series_for(city)
        stats = {
            "series": series, "events": 0, "resolved": 0,
            "parity_failures": 0, "settlement_failures": 0,
            "first": None, "last": None,
        }
        for event in iter_series_events(
            series, since=since, cache_dir=cache_dir, refresh=refresh,
            limit=limit_per_city,
        ):
            stats["events"] += 1
            observed = observation_date(event)
            if observed:
                stats["first"] = min(stats["first"] or observed, observed)
                stats["last"] = max(stats["last"] or observed, observed)

            try:
                spec = spec_for(event, city)
            except (BucketParityError, ValueError) as error:
                stats["parity_failures"] += 1
                if len(summary["parity_failures"]) < 50:
                    summary["parity_failures"].append(
                        {"slug": event.get("slug"), "error": str(error)[:200]}
                    )
                continue

            try:
                winner, winner_index = settled_bucket(event)
            except ValueError as error:
                stats["settlement_failures"] += 1
                if len(summary["settlement_failures"]) < 50:
                    summary["settlement_failures"].append(
                        {"slug": event.get("slug"), "error": str(error)}
                    )
                continue

            rows.append(dataset_row(event, city, spec, winner, winner_index))
            stats["resolved"] += 1
            summary["signatures"][spec.signature] += 1
            if spec.timezone is None:
                summary["missing_timezone"][spec.icao or spec.station_name] += 1
            if not spec.station_name:
                summary["missing_station"][spec.icao or "unknown"] += 1
            if spec.station_mismatch:
                summary["station_mismatches"].append({
                    "slug": event.get("slug"),
                    "city": city,
                    "station": spec.station_name,
                })

        summary["cities"][city] = stats
        print(
            f"{city:<10} events={stats['events']:>4} resolved={stats['resolved']:>4} "
            f"parity_bad={stats['parity_failures']:>2} "
            f"settle_bad={stats['settlement_failures']:>2} "
            f"range={stats['first']}..{stats['last']}",
            flush=True,
        )

    return rows, summary


def write_rows(rows: Iterable[dict], path: str) -> int:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            count += 1
    return count


def print_summary(summary: dict) -> None:
    print("\nContract families (signature = source|unit|widths|rounding):")
    for signature, count in summary["signatures"].most_common():
        print(f"  {count:>5}  {signature}")

    if summary["parity_failures"]:
        print(f"\nParity failures: {len(summary['parity_failures'])} (showing 5)")
        for row in summary["parity_failures"][:5]:
            print("  ", row)

    if summary["settlement_failures"]:
        print(f"\nSettlement failures: {len(summary['settlement_failures'])}")
        for row in summary["settlement_failures"][:5]:
            print("  ", row)

    if summary["missing_timezone"]:
        print("\nMissing timezone:", dict(summary["missing_timezone"]))

    if summary["missing_station"]:
        print("\nUnverified station (excluded from parity):",
              dict(summary["missing_station"]))

    if summary["station_mismatches"]:
        print(f"\nStation/city mismatches: {len(summary['station_mismatches'])}")
        for row in summary["station_mismatches"][:5]:
            print("  ", row)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Harvest resolved weather contracts (Phase 1 dataset)."
    )
    parser.add_argument("--cities", default=None,
                        help="comma-separated; default: config.json locations")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help=f"earliest observation date (default {DEFAULT_SINCE})")
    parser.add_argument("--out", default=None,
                        help="write the dataset JSONL to this path")
    parser.add_argument("--limit-per-city", type=int, default=None)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--refresh-cache", action="store_true")
    args = parser.parse_args(argv)

    cities = (
        [c.strip() for c in args.cities.split(",") if c.strip()]
        if args.cities
        else default_cities()
    )
    if not cities:
        parser.error("no cities given and config.json has no locations")

    rows, summary = harvest(
        cities,
        since=date.fromisoformat(args.since),
        cache_dir=args.cache_dir,
        limit_per_city=args.limit_per_city,
        refresh=args.refresh_cache,
    )
    print(f"\nResolved contracts: {len(rows)}")
    print_summary(summary)

    if args.out:
        written = write_rows(rows, args.out)
        print(f"\nWrote {written} rows to {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
