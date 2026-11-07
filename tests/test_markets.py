"""Market discovery: slug arithmetic, UTC parsing and the discovery cache."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from engine.clock import use_clock
from engine.markets import (
    DiscoveryCache,
    Market,
    fetch_event,
    market_slug,
    markets_from_event,
    parse_end_date,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ── Slug arithmetic (the june-31 bug) ───────────────────────────────────────

def test_slug_rolls_over_month_boundary():
    now = utc(2026, 1, 31, 23)
    slug, target = market_slug("London", 1, now=now)
    assert slug == "highest-temperature-in-london-on-february-1-2026"
    assert target.day == 1


def test_slug_rolls_over_year_boundary():
    slug, _ = market_slug("NYC", 1, now=utc(2026, 12, 31, 12))
    assert slug == "highest-temperature-in-nyc-on-january-1-2027"


def test_slug_handles_leap_day():
    slug, _ = market_slug("Paris", 1, now=utc(2028, 2, 28, 12))
    assert slug == "highest-temperature-in-paris-on-february-29-2028"


def test_slug_uses_injected_utc_clock_not_local_time():
    with use_clock(lambda: utc(2026, 6, 12, 23, 30)):
        slug, _ = market_slug("Tokyo", 0)
    assert slug.endswith("-june-12-2026")


# ── Resolution clock ────────────────────────────────────────────────────────

def test_parse_end_date_normalises_to_utc():
    assert parse_end_date("2026-06-12T00:00:00Z") == utc(2026, 6, 12)
    assert parse_end_date("2026-06-12T00:00:00") == utc(2026, 6, 12)
    assert parse_end_date("2026-06-12T02:00:00+02:00") == utc(2026, 6, 12)


def test_market_hours_to_resolution_is_utc_aware():
    market = Market(
        question="q",
        token_pair=["yes", "no"],
        condition_id="0xabc",
        resolution=utc(2026, 6, 12, 12),
        trading_volume=100.0,
        uncertainty=0.5,
    )
    assert market.hours_to_resolution(utc(2026, 6, 12, 0)) == pytest.approx(12.0)
    # A naive reference time must not raise (it is interpreted as UTC).
    assert market.hours_to_resolution(datetime(2026, 6, 11, 12)) == pytest.approx(24.0)


# ── Event parsing ───────────────────────────────────────────────────────────

RAW_EVENT = {
    "markets": [
        {
            "question": "Highest temperature in London on June 12?",
            "clobTokenIds": json.dumps(["yes-token", "no-token"]),
            "conditionId": "0xabc",
            "endDate": "2026-06-12T00:00:00Z",
            "volume": "1234.5",
            "outcomePrices": json.dumps(["0.5", "0.5"]),
        },
        {"question": "malformed market"},
    ]
}


def test_markets_from_event_skips_malformed_entries():
    markets = markets_from_event(RAW_EVENT, slug="some-slug")
    assert len(markets) == 1
    market = markets[0]
    assert market.condition_id == "0xabc"
    assert market.token_pair == ["yes-token", "no-token"]
    assert market.trading_volume == pytest.approx(1234.5)
    assert market.uncertainty == pytest.approx(1.0)
    assert market.slug == "some-slug"


def test_markets_from_event_handles_missing_event():
    assert markets_from_event(None) == []


# ── Discovery cache + fetch ─────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        for fragment, response in self.responses.items():
            if fragment in url:
                return response
        return FakeResponse(404, None)


def test_cache_roundtrip_and_ttl(tmp_path):
    cache = DiscoveryCache(str(tmp_path), ttl_s=60)
    cache.put("some-slug", {"markets": []})

    assert cache.get("some-slug") == {"markets": []}

    # Age the on-disk entry and drop the memory tier: now it is expired.
    path = tmp_path / "some-slug.json"
    old = time.time() - 120
    os.utime(path, (old, old))
    cache.clear()
    assert cache.get("some-slug") is None


def test_fetch_event_uses_cache_before_network(tmp_path):
    cache = DiscoveryCache(str(tmp_path), ttl_s=60)
    session = FakeSession({"weather": FakeResponse(200, RAW_EVENT)})

    first = fetch_event("weather", cache=cache, session=session)
    second = fetch_event("weather", cache=cache, session=session)

    assert first == second == RAW_EVENT
    assert len(session.calls) == 1     # second call served from cache


def test_fetch_event_404_is_none_and_not_fatal(tmp_path):
    cache = DiscoveryCache(str(tmp_path), ttl_s=60)
    session = FakeSession({})
    assert fetch_event("missing", cache=cache, session=session) is None


def test_fetch_event_survives_http_errors(tmp_path):
    cache = DiscoveryCache(str(tmp_path), ttl_s=60)
    session = FakeSession({"boom": FakeResponse(500, None)})
    assert fetch_event("boom", cache=cache, session=session) is None


def test_fetch_event_retries_once_on_429(tmp_path, monkeypatch):
    cache = DiscoveryCache(str(tmp_path), ttl_s=60)

    class RateLimitedSession:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(429, None, {"Retry-After": "0"})
            return FakeResponse(200, RAW_EVENT)

    monkeypatch.setattr("engine.markets.time.sleep", lambda _seconds: None)
    session = RateLimitedSession()

    assert fetch_event("weather", cache=cache, session=session) == RAW_EVENT
    assert session.calls == 2


def test_market_slug_accepts_explicit_now():
    slug, target = market_slug("Paris", 2, now=utc(2026, 6, 12, 10))
    assert slug == "highest-temperature-in-paris-on-june-14-2026"
    assert target == (utc(2026, 6, 12) + timedelta(days=2)).date()
