"""
Resolution stations — the other half of bucket parity.

A forecast gridpoint is not a station, and a contract settles on the value
observed at one specific station, in one specific unit, on one specific local
day.  If the model prices a different random variable than the contract, every
number downstream is noise (ROADMAP §5).

Station identity is *per contract*, not per city: older Paris contracts settle
at Charles de Gaulle (LFPG), newer ones at Paris–Le Bourget (LFPB).  The
registry therefore also indexes by ICAO, and `forecast.contracts` resolves the
station from each market's own resolution description.

`resolve_station` deliberately raises for cities it does not know instead of
silently substituting a nearby gridpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


class StationNotFound(KeyError):
    """Raised when a city has no explicitly verified resolution station."""


@dataclass(frozen=True)
class Station:
    """Everything needed to compare a forecast with a settlement."""

    city: str               # normalised city key used by the discovery layer
    name: str               # human-readable station name
    timezone: str           # IANA name; defines the day boundary
    unit: str = "C"         # default settlement unit, "C" or "F"
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    icao: Optional[str] = None
    source: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        unit = self.unit.upper()
        if unit not in {"C", "F"}:
            raise ValueError(f"unsupported station unit: {self.unit!r}")
        object.__setattr__(self, "unit", unit)
        if self.icao:
            object.__setattr__(self, "icao", self.icao.upper())


# Airport reference points.  Station identity and timezone are what parity
# depends on; coordinates are only used to pick the ensemble gridpoint later.
# Sources: contract resolution descriptions + airport reference points.
STATIONS: Dict[str, Station] = {
    "london": Station(
        city="london", name="London City Airport", timezone="Europe/London",
        latitude=51.505, longitude=0.055, icao="EGLC",
        source="contract description",
    ),
    "paris": Station(
        city="paris", name="Paris-Le Bourget Airport", timezone="Europe/Paris",
        latitude=48.969, longitude=2.441, icao="LFPB",
        source="contract description",
        note="contracts before ~Feb 2026 settle at Charles de Gaulle (LFPG)",
    ),
    "amsterdam": Station(
        city="amsterdam", name="Amsterdam Schiphol", timezone="Europe/Amsterdam",
        latitude=52.309, longitude=4.762, icao="EHAM",
        source="contract description",
    ),
    "munich": Station(
        city="munich", name="Munich Airport", timezone="Europe/Berlin",
        latitude=48.354, longitude=11.786, icao="EDDM",
        source="contract description",
    ),
    "madrid": Station(
        city="madrid", name="Adolfo Suárez Madrid-Barajas Airport",
        timezone="Europe/Madrid", latitude=40.472, longitude=-3.561,
        icao="LEMD", source="contract description",
    ),
    "seoul": Station(
        city="seoul", name="Incheon Intl Airport", timezone="Asia/Seoul",
        latitude=37.469, longitude=126.451, icao="RKSI",
        source="contract description",
        note="contract settles at Incheon, not Seoul city",
    ),
    "tokyo": Station(
        city="tokyo", name="Tokyo Haneda Airport", timezone="Asia/Tokyo",
        latitude=35.553, longitude=139.781, icao="RJTT",
        source="contract description",
    ),
    "shanghai": Station(
        city="shanghai", name="Shanghai Pudong International Airport",
        timezone="Asia/Shanghai", latitude=31.143, longitude=121.805,
        icao="ZSPD", source="contract description",
    ),
    "beijing": Station(
        city="beijing", name="Beijing Capital International Airport",
        timezone="Asia/Shanghai", latitude=40.080, longitude=116.585,
        icao="ZBAA", source="contract description",
    ),
    "taipei": Station(
        city="taipei", name="Taipei Songshan Airport", timezone="Asia/Taipei",
        latitude=25.069, longitude=121.552, icao="RCSS",
        source="contract description",
    ),
    "hong kong": Station(
        city="hong kong", name="Hong Kong Observatory",
        timezone="Asia/Hong_Kong", latitude=22.302, longitude=114.174,
        source="contract description",
        note="contracts settle on the Observatory's Absolute Daily Max (deg. C)",
    ),
    "wellington": Station(
        city="wellington", name="Wellington Airport", timezone="Pacific/Auckland",
        latitude=-41.327, longitude=174.805, icao="NZWN",
        source="contract description",
    ),
    "nyc": Station(
        city="nyc", name="LaGuardia Airport", timezone="America/New_York",
        latitude=40.777, longitude=-73.873, icao="KLGA",
        source="contract description",
    ),
    "chicago": Station(
        city="chicago", name="Chicago O'Hare Intl Airport",
        timezone="America/Chicago", latitude=41.978, longitude=-87.905,
        icao="KORD", source="contract description",
    ),
    "miami": Station(
        city="miami", name="Miami International Airport",
        timezone="America/New_York", latitude=25.793, longitude=-80.290,
        icao="KMIA", source="contract description",
    ),
    "toronto": Station(
        city="toronto", name="Toronto Pearson International Airport",
        timezone="America/Toronto", latitude=43.677, longitude=-79.631,
        icao="CYYZ", source="contract description",
    ),
}

# Historical contract variants (city → stations used by older contracts).
STATIONS_BY_ICAO: Dict[str, Station] = {
    "LFPG": Station(
        city="paris", name="Charles de Gaulle Airport", timezone="Europe/Paris",
        latitude=49.010, longitude=2.548, icao="LFPG",
        source="contract description",
        note="older Paris contracts only",
    ),
    "RCTP": Station(
        city="taipei", name="Taiwan Taoyuan International Airport",
        timezone="Asia/Taipei", latitude=25.077, longitude=121.234,
        icao="RCTP", source="contract description",
        note="NOAA-era Taipei contracts (Mar–Jun 2026); METAR era uses Songshan",
    ),
    "VHHH": Station(
        city="hong kong", name="Hong Kong International Airport",
        timezone="Asia/Hong_Kong", latitude=22.309, longitude=113.915,
        icao="VHHH", source="contract description",
        note="two early contracts only; the Observatory is the usual station",
    ),
}


def normalise_city(city: str) -> str:
    """Normalise a config/slug city name (`New-York` → `new york`)."""
    return city.strip().lower().replace("-", " ")


def _index(station: Station) -> None:
    STATIONS[normalise_city(station.city)] = station
    if station.icao:
        STATIONS_BY_ICAO[station.icao] = station


for _station in STATIONS.values():
    if _station.icao:
        STATIONS_BY_ICAO[_station.icao] = _station


def register_station(station: Station) -> Station:
    """Add or replace a station definition (used when a contract is verified)."""
    _index(station)
    return station


def resolve_station(city: str) -> Station:
    """Look up the current station for `city`; raises `StationNotFound`."""
    key = normalise_city(city)
    station = STATIONS.get(key)
    if station is None:
        raise StationNotFound(
            f"no verified resolution station for {city!r}; add it to "
            f"forecast/station.py after reading the contract's resolution rules"
        )
    return station


def resolve_icao(icao: str) -> Optional[Station]:
    """Look up a station by ICAO code (None if unregistered)."""
    return STATIONS_BY_ICAO.get(icao.strip().upper())


def resolve_station_name(name: str) -> Optional[Station]:
    """Look up a station by (loosely) matching its name."""
    wanted = _name_key(name)
    if not wanted:
        return None
    for station in _all_stations():
        if _name_key(station.name) == wanted:
            return station
    # Fall back to containment ("Chicago O'Hare" vs "Chicago O'Hare Intl");
    # only accept unambiguous matches.
    matches = [
        station for station in _all_stations()
        if wanted in _name_key(station.name)
        or _name_key(station.name) in wanted
    ]
    unique = {(s.city, s.name): s for s in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


# Qualifiers that differ between contract descriptions and station names.
_NAME_NOISE = {
    "the", "station", "airport", "airfield", "field", "municipal",
    "intl", "international", "regional", "weather", "observation",
    "obs", "report", "reporting",
}


def _all_stations() -> list:
    unique = {(s.city, s.name): s
              for s in list(STATIONS.values()) + list(STATIONS_BY_ICAO.values())}
    return list(unique.values())


def _name_key(name: str) -> str:
    """Normalise a station name for matching (drop Airport/Intl/Station…)."""
    tokens = [
        token for token in normalise_city(name).split()
        if token not in _NAME_NOISE
    ]
    return " ".join(tokens)
