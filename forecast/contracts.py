"""
Contract specifications: what a market actually settles on.

Polymarket weather contracts come in several families, e.g.

  * METAR family  — station ICAO in `resolutionSource`, tenths-of-a-degree
    observation, "rounded to the nearest whole degree, half rounded up",
    integer °C buckets.
  * Wunderground family — whole degrees from Weather Underground, integer or
    two-degree buckets, in °C or °F depending on the city/era.

The spec is parsed from each market's own resolution description, never
inferred from the city name, and the bucket set is validated as a partition.
This is the guard that stops the model pricing one random variable while the
contract settles another (ROADMAP §3, §5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from forecast.buckets import Bucket, BucketParityError, Rounding, validate_partition
from forecast.station import (
    Station,
    StationNotFound,
    normalise_city,
    resolve_icao,
    resolve_station,
    resolve_station_name,
)

# ── Detection helpers ────────────────────────────────────────────────────────

_LABEL_UNIT_RE = re.compile(r"°\s*([CF])", re.IGNORECASE)
_ICAO_IN_URL_RE = re.compile(r"ids=([A-Z]{4})")
_ICAO_IN_TEXT_RE = re.compile(r"\(([A-Z]{4})\)")

_STATION_PATTERNS = (
    re.compile(r"reported at (?P<name>.+?) \((?P<icao>[A-Z]{4})\)"),
    re.compile(
        r"(?:reported|recorded|observed)\s+(?:by\s+[A-Za-z ]+?\s+)?at\s+"
        r"(?:the\s+)?(?P<name>[A-Z][^.;]*?)\s+Station"
    ),
    # e.g. "…recorded by NOAA at the Taiwan Taoyuan International Airport in
    # degrees Celsius" — no "Station" suffix.
    re.compile(
        r"(?:reported|recorded|observed)\s+(?:by\s+[A-Za-z ]+?\s+)?at\s+"
        r"(?:the\s+)?(?P<name>[A-Z][^.;]*?)\s+in degrees"
    ),
    # `\bat` (not `at` inside `that`) and an uppercase start stop us from
    # capturing "contains the highest temperature recorded by NOAA at …".
    re.compile(r"\bat\s+(?:the\s+)?(?P<name>[A-Z][^.;]*?)\s+Station"),
)

_ROUNDING_RE = re.compile(
    r"rounded to the nearest whole degree", re.IGNORECASE
)
# `weather.gov` must not match Hong Kong's `weather.gov.hk`.
_WEATHER_GOV_RE = re.compile(r"(?:^|//|\.)weather\.gov(?:/|$|\?)")
_HALF_UP_RE = re.compile(r"half rounded up", re.IGNORECASE)


def detect_unit(description: str, labels: Sequence[str]) -> str:
    """Settlement unit from the outcome labels, then the description."""
    for label in labels:
        match = _LABEL_UNIT_RE.search(label)
        if match:
            return match.group(1).upper()

    text = description.lower()
    if "degrees fahrenheit" in text:
        return "F"
    if "degrees celsius" in text:
        return "C"
    return "C"


def detect_source(description: str, resolution_source: str = "") -> str:
    """
    Resolution source family.

    `metar` (aviationweather), `wunderground`, `noaa` (weather.gov),
    `hko` (Hong Kong Observatory), `cwa` (Taipei Central Weather
    Administration) or `unknown`.
    """
    url = (resolution_source or "").lower()
    if "aviationweather.gov" in url:
        return "metar"
    if "wunderground.com" in url:
        return "wunderground"
    if _WEATHER_GOV_RE.search(url):
        return "noaa"

    text = f"{resolution_source}\n{description}".lower()
    if "aviationweather.gov" in text or "metar" in text:
        return "metar"
    if "weather underground" in text or "wunderground" in text:
        return "wunderground"
    if "hong kong observatory" in text:
        return "hko"
    if "central weather administration" in text:
        return "cwa"
    if _WEATHER_GOV_RE.search(text) or "noaa" in text:
        return "noaa"
    return "unknown"


def detect_rounding(description: str) -> Optional[Rounding]:
    """Declared rounding rule, or None when the contract does not state one."""
    if _ROUNDING_RE.search(description):
        return Rounding.HALF_UP
    if _HALF_UP_RE.search(description):
        return Rounding.HALF_UP
    return None


def parse_station(description: str,
                  resolution_source: str = "") -> Tuple[str, Optional[str]]:
    """Extract `(station_name, icao)` from a resolution description/URL."""
    icao = None
    url_match = _ICAO_IN_URL_RE.search(resolution_source or "")
    if url_match:
        icao = url_match.group(1).upper()
    else:
        text_match = _ICAO_IN_TEXT_RE.search(description or "")
        if text_match:
            icao = text_match.group(1).upper()

    name = ""
    for pattern in _STATION_PATTERNS:
        match = pattern.search(description or "")
        if match:
            name = match.group("name").strip()
            if not icao and "icao" in match.groupdict() and match.group("icao"):
                icao = match.group("icao").upper()
            break

    name = re.sub(r"^(?:the)\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*\bStation$", "", name)
    name = re.sub(r"\s+", " ", name).strip(" ,.;")
    return name, icao


# ── Spec ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContractSpec:
    """Everything needed to price and settle one contract family."""

    city: str
    series: str
    station_name: str
    icao: Optional[str]
    timezone: Optional[str]
    unit: str
    rounding: Optional[Rounding]
    source: str
    labels: Tuple[str, ...]
    buckets: Tuple[Bucket, ...]
    station_mismatch: bool = False

    @property
    def bucket_widths(self) -> Tuple[str, ...]:
        """Width of each bounded bucket (`open` for tails)."""
        widths = []
        for bucket in self.buckets:
            if bucket.lower is None or bucket.upper is None:
                widths.append("open")
            else:
                widths.append(str(int(bucket.upper - bucket.lower + 1)))
        return tuple(widths)

    @property
    def signature(self) -> str:
        """Fingerprint of the contract family (for grouping the dataset)."""
        widths = "+".join(sorted(set(self.bucket_widths)))
        rounding = self.rounding.value if self.rounding else "unstated"
        return f"{self.source}|{self.unit}|{widths}|{rounding}"

    def station(self) -> Optional[Station]:
        return resolve_icao(self.icao) if self.icao else resolve_station_name(
            self.station_name
        )


def parse_contract_spec(labels: Sequence[str], description: str = "",
                        resolution_source: str = "", city: str = "",
                        series: str = "") -> ContractSpec:
    """
    Parse a contract specification and validate its buckets.

    Raises `BucketParityError` if the outcome labels do not form a clean
    partition, so a malformed contract can never enter the dataset.
    """
    if not labels:
        raise BucketParityError("contract has no outcome labels")

    unit = detect_unit(description, labels)
    buckets = tuple(Bucket.parse(label, unit=unit) for label in labels)
    validate_partition(buckets)

    source = detect_source(description, resolution_source)
    station_name, icao = parse_station(description, resolution_source)
    station = resolve_icao(icao) if icao else resolve_station_name(station_name)

    # Hong Kong contracts settle on the Observatory itself and name no
    # station; the city-level station is the only possible reading there.
    if station is None and source == "hko" and city:
        try:
            station = resolve_station(city)
        except StationNotFound:
            station = None

    if station and not icao:
        icao = station.icao
    if station and not station_name:
        station_name = station.name

    mismatch = bool(
        station and city and station.city != normalise_city(city)
    )

    timezone = station.timezone if station else None
    if timezone is None and city:
        # The day boundary is a city property; using it does not claim the
        # station identity, which stays empty for unverified sources (CWA).
        try:
            timezone = resolve_station(city).timezone
        except StationNotFound:
            timezone = None

    return ContractSpec(
        city=city,
        series=series,
        station_name=station_name,
        icao=icao,
        timezone=timezone,
        unit=unit,
        rounding=detect_rounding(description),
        source=source,
        labels=tuple(labels),
        buckets=buckets,
        station_mismatch=mismatch,
    )
