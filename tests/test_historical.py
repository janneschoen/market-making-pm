"""Historical dataset builders: dates, settlements, specs, pagination."""
import json
from datetime import date

import pytest

from eval import historical as H

METAR_DESCRIPTION = (
    "This market will resolve to the temperature range that contains the "
    "highest temperature reported at London City Airport (EGLC) on 5 Nov '26. "
    "The qualifying value is rounded to the nearest whole degree, with a value "
    "ending in exactly half rounded up to the next higher whole degree."
)

WUNDERGROUND_DESCRIPTION = (
    "This market will resolve to the temperature range that contains the "
    "highest temperature recorded at the Chicago O'Hare Intl Airport Station "
    "in degrees Fahrenheit on 4 Dec '25. The resolution source for this market "
    "will be information from Wunderground."
)

C_LABELS = ["5°C or below"] + [f"{v}°C" for v in range(6, 15)] + ["15°C or higher"]
F_LABELS = ["24°F or below", "25–26°F", "27–28°F", "29–30°F",
            "31–32°F", "33–34°F", "35°F or higher"]


def make_event(labels, description, *, slug, end_date, resolution_source="",
               winner_index=0, series="london-daily-weather", volume="123.5"):
    markets = []
    for index, label in enumerate(labels):
        markets.append({
            "groupItemTitle": label,
            "conditionId": f"0x{index}",
            "clobTokenIds": json.dumps([f"yes-{index}", f"no-{index}"]),
            "outcomePrices": json.dumps(
                ["1", "0"] if index == winner_index else ["0", "1"]
            ),
            "description": description,
            "resolutionSource": resolution_source,
        })
    return {
        "slug": slug,
        "seriesSlug": series,
        "endDate": end_date,
        "resolutionSource": resolution_source,
        "volume": volume,
        "markets": markets,
    }


def london_event(winner_index=5):
    return make_event(
        C_LABELS, METAR_DESCRIPTION,
        slug="highest-temperature-in-london-on-november-5-2026",
        end_date="2026-11-05T12:00:00Z",
        resolution_source="https://aviationweather.gov/api/data/metar?ids=EGLC",
        winner_index=winner_index,
    )


def chicago_event(winner_index=3):
    return make_event(
        F_LABELS, WUNDERGROUND_DESCRIPTION,
        slug="highest-temperature-in-chicago-on-december-4",
        end_date="2025-12-04T12:00:00Z",
        series="chicago-daily-weather",
        winner_index=winner_index,
    )


# ── Dates ────────────────────────────────────────────────────────────────────

def test_observation_date_from_description():
    assert H.observation_date(london_event()) == date(2026, 11, 5)


def test_observation_date_from_slug_with_year():
    event = london_event()
    event["markets"][0]["description"] = "no date here"
    assert H.observation_date(event) == date(2026, 11, 5)


def test_observation_date_from_slug_without_year():
    event = london_event()
    event["slug"] = "highest-temperature-in-london-on-jan-22"
    event["endDate"] = "2025-01-22T12:00:00Z"
    event["markets"][0]["description"] = "no date here"
    assert H.observation_date(event) == date(2025, 1, 22)


def test_observation_date_falls_back_to_end_date():
    event = london_event()
    event["slug"] = "unparseable-slug"
    event["markets"][0]["description"] = "no date here"
    assert H.observation_date(event) == date(2026, 11, 5)


# ── Settlement ───────────────────────────────────────────────────────────────

def test_settled_bucket_finds_the_single_winner():
    event = london_event(winner_index=5)
    assert H.settled_bucket(event) == ("10°C", 5)


def test_settled_bucket_rejects_ambiguous_settlements():
    event = london_event()
    for market in event["markets"]:
        market["outcomePrices"] = json.dumps(["0", "1"])   # nobody won
    with pytest.raises(ValueError):
        H.settled_bucket(event)

    event = london_event(winner_index=2)
    event["markets"][3]["outcomePrices"] = json.dumps(["1", "0"])
    with pytest.raises(ValueError):
        H.settled_bucket(event)


# ── Specs and rows ───────────────────────────────────────────────────────────

def test_spec_for_metar_event():
    spec = H.spec_for(london_event(), "London")
    assert spec.source == "metar"
    assert spec.unit == "C"
    assert spec.icao == "EGLC"
    assert spec.timezone == "Europe/London"


def test_spec_for_wunderground_range_event():
    spec = H.spec_for(chicago_event(), "Chicago")
    assert spec.source == "wunderground"
    assert spec.unit == "F"
    assert spec.timezone == "America/Chicago"
    assert spec.bucket_widths[1] == "2"


def test_dataset_row_carries_ids_and_settlement():
    event = chicago_event(winner_index=3)
    spec = H.spec_for(event, "Chicago")
    winner, index = H.settled_bucket(event)
    row = H.dataset_row(event, "Chicago", spec, winner, index)

    assert row["date"] == "2025-12-04"
    assert row["winner"] == winner == "29–30°F"
    assert row["spec"]["unit"] == "F"
    assert row["spec"]["signature"].startswith("wunderground|F|2+open")
    assert len(row["markets"]) == len(F_LABELS)
    assert row["markets"][0]["token_ids"] == ["yes-0", "no-0"]
    assert row["markets"][3]["settled"] == ["1", "0"]


def test_series_for_city_names():
    assert H.series_for("NYC") == "nyc-daily-weather"
    assert H.series_for("Hong-Kong") == "hong-kong-daily-weather"
    assert H.series_for("London") == "london-daily-weather"


# ── Pagination / cut-off ─────────────────────────────────────────────────────

def test_iter_series_events_stops_at_since(monkeypatch):
    recent = london_event()
    older = make_event(
        C_LABELS, METAR_DESCRIPTION,
        slug="highest-temperature-in-london-on-december-20-2024",
        end_date="2024-12-20T12:00:00Z",
    )
    older["markets"][0]["description"] = "no date here"   # use the slug date
    pages = {0: [recent] * 100, 100: [older]}

    monkeypatch.setattr(
        H, "fetch_page",
        lambda series, offset, cache_dir, refresh=False: pages.get(offset, []),
    )
    events = list(H.iter_series_events(
        "london-daily-weather", since=date(2025, 1, 1),
        cache_dir="/tmp", refresh=False,
    ))

    # The 2024 event is older than `since` and must not be yielded.
    assert len(events) == 100
    assert all(e is recent for e in events)


def test_iter_series_events_honours_limit(monkeypatch):
    monkeypatch.setattr(
        H, "fetch_page",
        lambda series, offset, cache_dir, refresh=False: [london_event()] * 100,
    )
    events = list(H.iter_series_events(
        "london-daily-weather", since=date(2025, 1, 1),
        cache_dir="/tmp", limit=7,
    ))
    assert len(events) == 7
