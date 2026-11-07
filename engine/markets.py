"""
Polymarket market data: discovery, order books, resolution clocks.

Phase 0 fixes from ROADMAP §10:

  * slugs are built from a single UTC date, so month/year rollover works
    (`june-31` can no longer be produced)
  * `endDate` is parsed into a timezone-aware UTC datetime (it used to be
    annotated `str` and compared against local `datetime.now()`)
  * Gamma discovery responses are cached in memory and on disk with a TTL,
    instead of issuing up to 30 requests × 15 locations on every startup
  * a missing/404 event yields an empty list instead of raising
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol, Sequence

import requests

from engine import clock

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"
SLUG_TEMPLATE = "highest-temperature-in-{location}-on-{month}-{day}-{year}"

DEFAULT_CACHE_DIR = os.path.join("logs", "cache", "gamma")
DEFAULT_CACHE_TTL_S = 300.0
DEFAULT_TIMEOUT_S = 10.0


# ── Time helpers ─────────────────────────────────────────────────────────────

def as_utc(value: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC (naive input is assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_end_date(value: Any) -> datetime:
    """Parse a Gamma `endDate` (ISO-8601, usually with a trailing `Z`)."""
    if isinstance(value, datetime):
        return as_utc(value)
    text = str(value).strip()
    if not text:
        raise ValueError("empty endDate")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return as_utc(datetime.fromisoformat(text))


# ── Market ───────────────────────────────────────────────────────────────────

@dataclass
class Market:
    """Normalised representation of a single Polymarket binary market."""

    question: str
    token_pair: List[str]          # [YES_token_id, NO_token_id]
    condition_id: str              # Polymarket condition ID
    resolution: datetime           # UTC, timezone-aware
    trading_volume: float          # total volume in USDC
    uncertainty: float             # 1 − 2|0.5 − p|  → 1.0 at 50%, 0.0 at 0/100
    slug: str = ""                 # event slug the market was discovered under

    def __post_init__(self) -> None:
        self.resolution = as_utc(self.resolution)

    def hours_to_resolution(self, now: Optional[datetime] = None) -> float:
        """Hours remaining until resolution, measured from `now` (UTC)."""
        reference = as_utc(now) if now is not None else clock.now()
        return (self.resolution - reference).total_seconds() / 3600


# ── Slugs ────────────────────────────────────────────────────────────────────

def market_slug(location: str, day_delay: int = 0,
                now: Optional[datetime] = None) -> tuple[str, date]:
    """
    Build the Gamma event slug for `location` on (today + `day_delay`) UTC.

    The month, day and year all come from the same shifted date.  The old
    implementation took the month from `now` and shifted only the day, which
    produced slugs like `june-31` and silently 404'd at month boundaries.

    Returns `(slug, target_date)` so callers can log or assert the date.
    """
    reference = as_utc(now) if now is not None else clock.now()
    target = reference.date() + timedelta(days=day_delay)
    slug = SLUG_TEMPLATE.format(
        location=location.lower(),
        month=target.strftime("%B").lower(),
        day=target.day,
        year=target.year,
    )
    return slug, target


# ── Discovery cache ──────────────────────────────────────────────────────────

class _Response(Protocol):
    status_code: int

    def json(self) -> Any: ...

    def raise_for_status(self) -> None: ...


class _Session(Protocol):
    def get(self, url: str, **kwargs: Any) -> _Response: ...


class DiscoveryCache:
    """
    Two-level (memory + disk) TTL cache for Gamma event payloads.

    Discovery is by far the most request-hungry part of a startup.  Caching
    raw event JSON keeps repeat scans (and restarts within the TTL) cheap.
    Entries are keyed by slug and expire after `ttl_s` seconds.
    """

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR,
                 ttl_s: float = DEFAULT_CACHE_TTL_S):
        self.cache_dir = cache_dir
        self.ttl_s = float(ttl_s)
        self._memory: Dict[str, tuple[float, Any]] = {}

    # -- paths ------------------------------------------------------------
    def _path(self, slug: str) -> str:
        safe = slug.replace(os.sep, "_").replace("/", "_")
        return os.path.join(self.cache_dir, f"{safe}.json")

    def _ensure_dir(self) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)

    # -- operations -------------------------------------------------------
    def get(self, slug: str) -> Optional[Any]:
        """Return a cached payload, or None if absent/expired."""
        cached = self._memory.get(slug)
        if cached is not None and (time.time() - cached[0]) <= self.ttl_s:
            return cached[1]

        path = self._path(slug)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return None
        if (time.time() - mtime) > self.ttl_s:
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                entry = json.load(handle)
            payload = entry["payload"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

        self._memory[slug] = (mtime, payload)
        return payload

    def put(self, slug: str, payload: Any) -> None:
        """Store a payload (best-effort: cache failures are never fatal)."""
        now = time.time()
        self._memory[slug] = (now, payload)
        try:
            self._ensure_dir()
            path = self._path(slug)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "fetched_at": datetime.fromtimestamp(
                            now, tz=timezone.utc
                        ).isoformat(),
                        "slug": slug,
                        "payload": payload,
                    },
                    handle,
                )
        except OSError:
            pass

    def clear(self) -> None:
        """Drop the in-memory tier (disk entries are left to expire)."""
        self._memory.clear()


_default_cache = DiscoveryCache()


def get_discovery_cache() -> DiscoveryCache:
    """Process-wide default discovery cache."""
    return _default_cache


def configure_discovery_cache(cache_dir: Optional[str] = None,
                              ttl_s: Optional[float] = None) -> DiscoveryCache:
    """Rebuild the default cache from configuration at startup."""
    global _default_cache
    _default_cache = DiscoveryCache(
        cache_dir=cache_dir or _default_cache.cache_dir,
        ttl_s=_default_cache.ttl_s if ttl_s is None else ttl_s,
    )
    return _default_cache


# ── Discovery ────────────────────────────────────────────────────────────────

def fetch_event(slug: str, cache: Optional[DiscoveryCache] = None,
                session: Optional[_Session] = None,
                timeout: float = DEFAULT_TIMEOUT_S,
                max_retries: int = 1) -> Optional[dict]:
    """Fetch (and cache) one Gamma event by slug; None on 404/error.

    A 429 is retried once, honouring `Retry-After` up to a small cap, so a
    discovery scan does not stampede the API (ROADMAP §8).
    """
    cache = cache if cache is not None else get_discovery_cache()
    cached = cache.get(slug)
    if cached is not None:
        return cached

    url = f"{GAMMA_URL}/events/slug/{slug}"
    http = session if session is not None else requests
    response = None
    for attempt in range(max_retries + 1):
        try:
            response = http.get(url, timeout=timeout)
        except Exception:  # pragma: no cover - defensive against odd clients
            return None
        if response.status_code == 429 and attempt < max_retries:
            time.sleep(min(_retry_after(response), 5.0))
            continue
        break

    try:
        if response is None or response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return None
    except Exception:  # pragma: no cover - defensive against odd HTTP clients
        return None

    if isinstance(payload, dict):
        cache.put(slug, payload)
    return payload if isinstance(payload, dict) else None


def _retry_after(response: Any, default: float = 1.0) -> float:
    """Seconds to wait from a `Retry-After` header (seconds form only)."""
    headers = getattr(response, "headers", None) or {}
    try:
        value = float(headers.get("Retry-After", default))
    except (TypeError, ValueError):
        return default
    return max(0.0, min(value, 5.0))


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _uncertainty(outcome_prices: Any) -> float:
    """1 − 2|0.5 − P(YES)|: 1.0 at 50/50, 0.0 at a resolved market."""
    try:
        prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) \
            else list(outcome_prices or [])
        yes_price = float(prices[0])
    except (TypeError, ValueError, IndexError):
        return 0.0
    return max(0.0, 1.0 - 2.0 * abs(0.5 - yes_price))


def parse_market(raw: dict, slug: str = "") -> Market:
    """Normalise one raw Gamma market into a `Market`."""
    return Market(
        question=raw["question"],
        token_pair=json.loads(raw["clobTokenIds"]),
        condition_id=raw["conditionId"],
        resolution=parse_end_date(raw["endDate"]),
        trading_volume=_float(raw.get("volume")),
        uncertainty=_uncertainty(raw.get("outcomePrices")),
        slug=slug,
    )


def markets_from_event(event: Optional[dict], slug: str = "") -> List[Market]:
    """Extract `Market`s from a Gamma event payload (empty on none)."""
    if not event:
        return []
    markets: List[Market] = []
    for raw in event.get("markets") or []:
        try:
            markets.append(parse_market(raw, slug=slug))
        except (KeyError, TypeError, ValueError):
            # One malformed market must not kill the whole scan.
            continue
    return markets


def get_locations_markets(location: str, day_delay: int = 0,
                          now: Optional[datetime] = None,
                          cache: Optional[DiscoveryCache] = None,
                          session: Optional[_Session] = None) -> List[Market]:
    """All weather markets for `location` on (today + `day_delay`) UTC."""
    slug, _target = market_slug(location, day_delay, now=now)
    event = fetch_event(slug, cache=cache, session=session)
    return markets_from_event(event, slug=slug)


def find_markets_in_window(location: str, trading_window: Sequence[float],
                           max_day_delay: int = 30,
                           now: Optional[datetime] = None,
                           cache: Optional[DiscoveryCache] = None,
                           session: Optional[_Session] = None) -> List[Market]:
    """
    Scan forward from today until a day's markets fall inside the window.

    `trading_window` is `[max_hours, min_hours]`; a market is tradeable when
    `max_hours > hours_to_resolution > min_hours`.  Returns the first such
    day's markets, or `[]` if nothing is found within `max_day_delay` days.
    """
    max_hours, min_hours = float(trading_window[0]), float(trading_window[1])
    reference = as_utc(now) if now is not None else clock.now()

    for day_delay in range(max_day_delay + 1):
        markets = get_locations_markets(
            location, day_delay, now=reference, cache=cache, session=session
        )
        if not markets:
            continue
        if any(max_hours > m.hours_to_resolution(reference) > min_hours
               for m in markets):
            return markets
    return []


# ── Live data ────────────────────────────────────────────────────────────────

async def get_price(token_id: str) -> float:
    """Best BUY price for a single token ID."""
    response = requests.get(
        f"{CLOB_URL}/price", params={"token_id": token_id, "side": "BUY"},
        timeout=DEFAULT_TIMEOUT_S,
    )
    return float(response.json()["price"])


async def get_market_bids(token_pair: Sequence[str]) -> List[float]:
    """
    Return `[best_yes_bid, best_no_bid]` for a CLOB token pair.

    Uses `/book`; falls back to 0.01 when a side has no bids.  Note that the
    fallback is a placeholder, not a tradeable price — callers that *sell*
    should treat it as "no market".
    """
    bids: List[float] = []
    for token in token_pair:
        response = requests.get(
            f"{CLOB_URL}/book", params={"token_id": token}, timeout=DEFAULT_TIMEOUT_S
        )
        data = response.json()
        book_bids = data.get("bids", [])
        bids.append(float(book_bids[-1]["price"]) if len(book_bids) else 0.01)
    return bids


async def get_hours_to_resolution(market: Market,
                                  now: Optional[datetime] = None) -> float:
    """Hours remaining until the market resolves (UTC)."""
    return market.hours_to_resolution(now)
