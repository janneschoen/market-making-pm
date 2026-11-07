"""Contract-spec parsing against the real contract families."""

import pytest

from forecast.buckets import BucketParityError, Rounding
from forecast.contracts import (
    detect_rounding,
    detect_source,
    detect_unit,
    parse_contract_spec,
    parse_station,
)

METAR_DESCRIPTION = (
    "This market will resolve to the temperature range that contains the "
    "highest temperature reported at London City Airport (EGLC) on 5 Nov '26. "
    "The qualifying value is the highest temperature reported in the hourly "
    "temperature remark of any routine (METAR) observation for London City "
    "Airport (EGLC) on that date, in tenths of a degree Celsius as reported. "
    "Each market in this event states its range in whole degrees Celsius. The "
    "qualifying value is rounded to the nearest whole degree, with a value "
    "ending in exactly half rounded up to the next higher whole degree. For "
    "example, 13.9°C counts as 14°C; 16.5°C counts as 17°C. An observation "
    "counts if its time stamp, converted to the station's local time "
    "(including any daylight-saving shift in effect), falls on the observation "
    "date. The resolution source for this market will be the routine "
    "observations for London City Airport (EGLC) published here: "
    "https://aviationweather.gov/api/data/metar?ids=EGLC&format=raw"
)

WUNDERGROUND_DESCRIPTION = (
    "This market will resolve to the temperature range that contains the "
    "highest temperature recorded at the Chicago O'Hare Intl Airport Station in "
    "degrees Fahrenheit on 4 Dec '25. The resolution source for this market "
    "will be information from Wunderground, specifically the highest "
    "temperature recorded for all times on this day by the Forecast for the "
    "Chicago O'Hare Intl Airport Station once information is reported."
)

C_LABELS = ["5°C or below"] + [f"{v}°C" for v in range(6, 15)] + ["15°C or higher"]
F_RANGE_LABELS = [
    "24°F or below", "25–26°F", "27–28°F", "29–30°F", "31–32°F",
    "33–34°F", "35°F or higher",
]


def test_metar_contract_spec():
    spec = parse_contract_spec(
        C_LABELS, description=METAR_DESCRIPTION,
        resolution_source="https://aviationweather.gov/api/data/metar?ids=EGLC",
        city="London", series="london-daily-weather",
    )
    assert spec.source == "metar"
    assert spec.unit == "C"
    assert spec.rounding is Rounding.HALF_UP
    assert spec.icao == "EGLC"
    assert spec.station_name == "London City Airport"
    assert spec.timezone == "Europe/London"
    assert not spec.station_mismatch
    assert spec.bucket_widths[0] == "open" and spec.bucket_widths[-1] == "open"
    assert spec.signature == "metar|C|1+open|half_up"


def test_wunderground_range_contract_spec():
    spec = parse_contract_spec(
        F_RANGE_LABELS, description=WUNDERGROUND_DESCRIPTION,
        resolution_source="", city="Chicago", series="chicago-daily-weather",
    )
    assert spec.source == "wunderground"
    assert spec.unit == "F"
    assert spec.rounding is None            # contract states no rounding rule
    assert spec.icao == "KORD"              # resolved via the station registry
    assert spec.station_name.startswith("Chicago O'Hare")
    assert spec.timezone == "America/Chicago"
    assert spec.bucket_widths == (
        "open", "2", "2", "2", "2", "2", "open")
    assert spec.signature == "wunderground|F|2+open|unstated"


def test_paris_station_variants_are_distinct():
    old = parse_contract_spec(
        ["3°C or below", "4°C", "5°C or higher"],
        description=(
            "highest temperature recorded at the Charles de Gaulle Airport "
            "Station in degrees Celsius on 2 Feb '26."
        ),
        city="Paris",
    )
    new = parse_contract_spec(
        ["3°C or below", "4°C", "5°C or higher"],
        description=(
            "highest temperature reported at Paris-Le Bourget Airport (LFPB) "
            "on 2 Nov '26."
        ),
        city="Paris",
    )
    assert old.icao == "LFPG"
    assert new.icao == "LFPB"
    assert old.timezone == new.timezone == "Europe/Paris"


def test_station_mismatch_is_flagged():
    spec = parse_contract_spec(
        C_LABELS, description=METAR_DESCRIPTION,
        resolution_source="https://aviationweather.gov/api/data/metar?ids=EGLC",
        city="Tokyo",              # contract says London
    )
    assert spec.station_mismatch


def test_bad_buckets_are_rejected():
    with pytest.raises(BucketParityError):
        parse_contract_spec(
            ["5°C or below", "7°C", "8°C or higher"],
            description="not important", city="London",
        )


def test_detection_helpers():
    assert detect_unit("", ["12°F", "13°F"]) == "F"
    assert detect_unit("in degrees Celsius", ["12", "13"]) == "C"
    assert detect_source("", "https://aviationweather.gov/api/data/metar") == "metar"
    assert detect_source("information from Wunderground") == "wunderground"
    assert detect_source("nothing here") == "unknown"
    assert detect_rounding("rounded to the nearest whole degree") is Rounding.HALF_UP
    assert detect_rounding("no rounding mentioned") is None


def test_parse_station_from_description_and_url():
    name, icao = parse_station(
        METAR_DESCRIPTION,
        "https://aviationweather.gov/api/data/metar?ids=EGLC",
    )
    assert (name, icao) == ("London City Airport", "EGLC")

    name, icao = parse_station(WUNDERGROUND_DESCRIPTION)
    assert name == "Chicago O'Hare Intl Airport"
    assert icao is None


NOAA_DESCRIPTION = (
    "This market will resolve to the temperature range that contains the "
    "highest temperature recorded by NOAA at the Incheon Intl Airport Station "
    "in degrees Celsius on 22 Oct '26. The resolution source for this market "
    "will be information from NOAA, specifically the highest reading under the "
    "\"Temp\" column, available here: "
    "https://www.weather.gov/wrh/timeseries?site=rksi"
)


def test_noaa_contract_spec():
    spec = parse_contract_spec(
        ["15°C or below", "16°C", "17°C or higher"],
        description=NOAA_DESCRIPTION,
        resolution_source="https://www.weather.gov/wrh/timeseries?site=rksi",
        city="Seoul",
    )
    assert spec.source == "noaa"
    assert spec.station_name == "Incheon Intl Airport"
    assert spec.icao == "RKSI"           # resolved via the station registry
    assert spec.timezone == "Asia/Seoul"
    assert not spec.station_mismatch


def test_station_name_matching_tolerates_contract_qualifiers():
    cases = {
        "Wellington Intl Airport": "Pacific/Auckland",
        "Toronto Pearson Intl Airport": "America/Toronto",
        "Miami Intl Airport": "America/New_York",
        "Chicago O'Hare Intl Airport": "America/Chicago",
        "London City Airport": "Europe/London",
    }
    for name, timezone in cases.items():
        spec = parse_contract_spec(
            ["15°C or below", "16°C", "17°C or higher"],
            description=(
                f"highest temperature recorded at the {name} Station in "
                f"degrees Celsius on 2 Feb '26."
            ),
        )
        assert spec.timezone == timezone, name


def test_source_detection_prefers_the_resolution_url():
    assert detect_source(NOAA_DESCRIPTION,
                         "https://www.weather.gov/wrh/timeseries?site=rksi") == "noaa"
    assert detect_source(
        "fallback to the Weather Underground table",
        "https://aviationweather.gov/api/data/metar?ids=EGLC") == "metar"
    assert detect_source(
        "information from NOAA",
        "https://www.wunderground.com/history/daily/gb/london/EGLC") == "wunderground"


HKO_DESCRIPTION = (
    "This market will resolve to the temperature range that contains the "
    "highest temperature recorded by the Hong Kong Observatory in degrees "
    "Celsius on 5 Nov '26. The resolution source for this market will be "
    "information from the Hong Kong Observatory, specifically the \"Absolute "
    "Daily Max (deg. C)\" for the specified date."
)

CWA_DESCRIPTION = (
    "This market will resolve to the temperature range that contains the "
    "highest temperature recorded by Taipei's Central Weather Administration "
    "in degrees Celsius on 22 Mar '26. The resolution source for this market "
    "will be information from the Taipei Central Weather Administration, "
    "specifically the highest reading under the \"Temperature\" column."
)

TAOYUAN_DESCRIPTION = (
    "This market will resolve to the temperature range that contains the "
    "highest temperature recorded by NOAA at the Taiwan Taoyuan International "
    "Airport in degrees Celsius on 4 Apr '26. The resolution source for this "
    "market will be information from NOAA."
)


def test_hko_contract_uses_the_observatory():
    spec = parse_contract_spec(
        ["15°C or below", "16°C", "17°C or higher"],
        description=HKO_DESCRIPTION, city="Hong Kong",
    )
    assert spec.source == "hko"
    assert spec.station_name == "Hong Kong Observatory"
    assert spec.timezone == "Asia/Hong_Kong"


def test_cwa_contract_does_not_invent_a_station():
    spec = parse_contract_spec(
        ["15°C or below", "16°C", "17°C or higher"],
        description=CWA_DESCRIPTION, city="Taipei",
    )
    assert spec.source == "cwa"
    assert spec.station_name == ""          # unverified, not guessed
    assert spec.icao is None
    assert spec.timezone == "Asia/Taipei"   # day boundary is a city property


def test_taipei_station_without_station_suffix():
    spec = parse_contract_spec(
        ["15°C or below", "16°C", "17°C or higher"],
        description=TAOYUAN_DESCRIPTION, city="Taipei",
    )
    assert spec.station_name == "Taiwan Taoyuan International Airport"
    assert spec.icao == "RCTP"
    assert spec.timezone == "Asia/Taipei"


def test_hong_kong_observatory_url_is_not_noaa():
    # weather.gov.hk contains "weather.gov" as a substring.
    assert detect_source(
        "information from the Hong Kong Observatory",
        "https://www.weather.gov.hk/en/cis/dailyExtract.htm",
    ) == "hko"
