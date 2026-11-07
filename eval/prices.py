"""
Phase 1 market-implied probabilities at decision timestamps.

Joins the resolved-contract dataset to CLOB `prices-history`:

  * decision times are the **station's local** day boundary minus 24 h and
    12 h, so no decision happens after observations for the settlement day
    have started (ROADMAP §5, point-in-time correctness),
  * one `prices-history` request per YES token, cached on disk and re-usable,
  * forward-fill the last quoted price, with a staleness limit so an
    untouched old price is not mistaken for a live quote.

Output: one JSONL row per contract with the market-implied probability of
every bucket at each decision time, plus explicit missing buckets.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests

CLOB_URL = "https://clob.polymarket.com"
DEFAULT_CACHE_DIR = os.path.join("logs", "cache", "prices")
DEFAULT_OUT_PATH = os.path.join("logs", "history", "market_prices.jsonl")
DEFAULT_ROWS_PATH = os.path.join("logs", "history", "resolved_events.jsonl")
DEFAULT_FIDELITY_MIN = 60
DEFAULT_WORKERS = 12
DEFAULT_STALENESS_S = 6 * 3600
REQUEST_TIMEOUT_S = 20
DECISION_OFFSETS = (24, 12)      # hours before the local settlement day


# ── Decision times ───────────────────────────────────────────────────────────

def decision_times(row: dict) -> Dict[str, datetime]:
    """
    `{"T-24h": …, "T-12h": …}` in UTC, anchored to the station's local day.

    Anchoring to the local day (not to the 12:00Z `endDate`) is what keeps a
    decision from being made after the observation day has started in, say,
    Tokyo or Wellington.
    """
    tz_name = (row.get("spec") or {}).get("timezone")
    day_text = row.get("date")
    if not tz_name or not day_text:
        return {}

    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # unknown tz name from a malformed spec
        return {}

    day = date.fromisoformat(day_text)
    local_midnight = datetime.combine(day, dtime(0, 0), tzinfo=tz)
    return {
        f"T-{hours}h": (local_midnight - timedelta(hours=hours)).astimezone(
            timezone.utc
        )
        for hours in DECISION_OFFSETS
    }


def window_for(decisions: Dict[str, datetime]) -> Optional[Tuple[int, int]]:
    """Price-history window covering every decision time (with margin)."""
    if not decisions:
        return None
    stamps = [int(moment.timestamp()) for moment in decisions.values()]
    return min(stamps) - 12 * 3600, max(stamps) + 3600


# ── Price history ────────────────────────────────────────────────────────────

def _cache_path(cache_dir: str, token_id: str, start_ts: int,
                end_ts: int, fidelity: int) -> str:
    digest = hashlib.sha1(token_id.encode("utf-8")).hexdigest()[:20]
    return os.path.join(
        cache_dir, f"{digest}-{start_ts}-{end_ts}-{fidelity}.json"
    )


def _get_json(url: str, params: dict, timeout: float = REQUEST_TIMEOUT_S):
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, timeout=timeout)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 1))
                time.sleep(min(retry_after, 5.0))
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == 2:
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


def fetch_history(token_id: str, start_ts: int, end_ts: int,
                  fidelity: int = DEFAULT_FIDELITY_MIN,
                  cache_dir: str = DEFAULT_CACHE_DIR,
                  refresh: bool = False) -> Tuple[List[Tuple[int, float]], bool]:
    """
    Return `([(timestamp, price), …], from_cache)` for one YES token.

    Results are cached per (token, window, fidelity); `( [], False )` means
    the request failed, `([], True)` means the token genuinely has no trades.
    """
    path = _cache_path(cache_dir, token_id, start_ts, end_ts, fidelity)
    if not refresh and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            points = payload.get("history") or []
            return [(int(p["t"]), float(p["p"])) for p in points], True
        except (OSError, ValueError, KeyError, TypeError):
            pass

    payload = _get_json(
        f"{CLOB_URL}/prices-history",
        {
            "market": token_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": fidelity,
        },
    )
    if payload is None:
        return [], False

    points = []
    for point in payload.get("history") or []:
        try:
            points.append((int(point["t"]), float(point["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    points.sort()

    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "token_id": token_id,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "fidelity": fidelity,
                    "history": [{"t": t, "p": p} for t, p in points],
                },
                handle,
            )
    except OSError:
        pass

    return points, False


def price_at(history: Sequence[Tuple[int, float]], timestamp: int,
             max_staleness_s: int = DEFAULT_STALENESS_S) -> Optional[float]:
    """Last quoted price at or before `timestamp`, if fresh enough."""
    if not history:
        return None
    index = bisect.bisect_right(history, (timestamp, float("inf"))) - 1
    if index < 0:
        return None
    stamp, price = history[index]
    if timestamp - stamp > max_staleness_s:
        return None
    return price


# ── Assembly ─────────────────────────────────────────────────────────────────

def assemble(row: dict, histories: Dict[str, Sequence[Tuple[int, float]]],
             staleness_s: int = DEFAULT_STALENESS_S) -> dict:
    """Market-implied bucket probabilities for one contract."""
    decisions = decision_times(row)
    created = row.get("created_at")
    created_at = None
    if created:
        try:
            created_at = datetime.fromisoformat(
                str(created).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except ValueError:
            created_at = None

    out = {
        "slug": row.get("slug"),
        "city": row.get("city"),
        "date": row.get("date"),
        "winner": row.get("winner"),
        "decisions": {},
    }

    for name, moment in decisions.items():
        if created_at is not None and created_at > moment:
            out["decisions"][name] = {
                "ts": int(moment.timestamp()),
                "available": False,
                "reason": "not_listed",
            }
            continue

        prices: Dict[str, float] = {}
        missing: List[str] = []
        for market in row.get("markets") or []:
            tokens = market.get("token_ids") or []
            label = market.get("label", "")
            if not tokens:
                missing.append(label)
                continue
            price = price_at(
                histories.get(tokens[0], []),
                int(moment.timestamp()),
                staleness_s,
            )
            if price is None:
                missing.append(label)
            else:
                prices[label] = price

        out["decisions"][name] = {
            "ts": int(moment.timestamp()),
            "available": bool(prices),
            "prices": prices,
            "missing": missing,
            "complete": not missing,
            "n_quoted": len(prices),
            "sum": sum(prices.values()) if prices else None,
        }
    return out


# ── Harvest ──────────────────────────────────────────────────────────────────

def harvest_prices(rows: Sequence[dict], cache_dir: str = DEFAULT_CACHE_DIR,
                   fidelity: int = DEFAULT_FIDELITY_MIN,
                   workers: int = DEFAULT_WORKERS,
                   refresh: bool = False,
                   staleness_s: int = DEFAULT_STALENESS_S,
                   out_path: Optional[str] = None) -> dict:
    """Fetch every token's history once, then assemble per-contract rows."""
    tasks: Dict[str, Tuple[int, int]] = {}
    for row in rows:
        window = window_for(decision_times(row))
        if not window:
            continue
        start, end = window
        for market in row.get("markets") or []:
            tokens = market.get("token_ids") or []
            if tokens:
                tasks.setdefault(tokens[0], (start, end))

    print(f"tokens to fetch: {len(tasks)} (cache: {cache_dir})", flush=True)
    histories: Dict[str, List[Tuple[int, float]]] = {}
    cached_hits = empty_histories = failures = 0
    done = 0
    started = time.time()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(
                fetch_history, token_id, start, end, fidelity, cache_dir, refresh
            ): token_id
            for token_id, (start, end) in tasks.items()
        }
        for future in as_completed(futures):
            token_id = futures[future]
            done += 1
            try:
                points, from_cache = future.result()
            except Exception:  # noqa: BLE001 - one bad token must not stop the run
                failures += 1
                histories[token_id] = []
                continue
            cached_hits += int(from_cache)
            if not points:
                empty_histories += 1
            histories[token_id] = points
            if done % 1000 == 0:
                rate = done / max(time.time() - started, 1e-9)
                print(f"  {done}/{len(tasks)} tokens ({rate:.0f}/s)", flush=True)

    joined = [assemble(row, histories, staleness_s) for row in rows]
    summary = summarize(joined, tasks, cached_hits, empty_histories, failures)
    if out_path:
        write_rows(joined, out_path)
        summary["out_path"] = out_path
    return summary


def summarize(joined: Sequence[dict], tasks: dict, cached_hits: int,
              empty_histories: int, failures: int) -> dict:
    summary: dict = {
        "contracts": len(joined),
        "tokens": len(tasks),
        "cache_hits": cached_hits,
        "empty_histories": empty_histories,
        "failed_requests": failures,
        "decisions": {},
    }

    names = sorted({name for row in joined for name in row["decisions"]})
    for name in names:
        entries = [
            row["decisions"][name] for row in joined
            if name in row["decisions"]
        ]
        available = [e for e in entries if e.get("available")]
        complete = [e for e in available if e.get("complete")]
        sums = [e["sum"] for e in complete if e.get("sum") is not None]
        quoted = [e["n_quoted"] for e in available]
        summary["decisions"][name] = {
            "contracts": len(entries),
            "available": len(available),
            "complete": len(complete),
            "coverage": (len(complete) / len(entries)) if entries else 0.0,
            "median_sum": statistics.median(sums) if sums else None,
            "mean_sum": (sum(sums) / len(sums)) if sums else None,
            "median_quoted": statistics.median(quoted) if quoted else None,
        }
    return summary


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


def load_rows(path: str) -> List[dict]:
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


def print_summary(summary: dict) -> None:
    print(f"\nContracts: {summary['contracts']} | tokens: {summary['tokens']} "
          f"| cache hits: {summary['cache_hits']} "
          f"| empty: {summary['empty_histories']} "
          f"| failed: {summary['failed_requests']}")
    for name, stats in summary["decisions"].items():
        print(f"  {name:<7} available={stats['available']:>5} "
              f"complete={stats['complete']:>5} "
              f"coverage={stats['coverage']:.1%} "
              f"median_sum={stats['median_sum']} "
              f"median_quoted={stats['median_quoted']}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Join resolved contracts to market price history."
    )
    parser.add_argument("--rows", default=DEFAULT_ROWS_PATH)
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--fidelity", type=int, default=DEFAULT_FIDELITY_MIN)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N contracts (for smoke tests)")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)

    rows = load_rows(args.rows)
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        parser.error(f"no rows in {args.rows}; run `python -m eval.historical`")

    summary = harvest_prices(
        rows,
        cache_dir=args.cache_dir,
        fidelity=args.fidelity,
        workers=args.workers,
        refresh=args.refresh,
        out_path=args.out,
    )
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
