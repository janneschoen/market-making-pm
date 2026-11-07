"""
Resolution stations — the other half of bucket parity.

A forecast gridpoint is not a station, and a contract settles on the value
observed at one specific station, in one specific unit, on one specific local
day.  If the model prices a different random variable than the contract, every
number downstream is noise (ROADMAP §5).

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
    latitude: float
    longitude: float
    timezone: str           # IANA name; defines the day boundary
    unit: str = "C"         # settlement unit, "C" or "F"
    wmo_id: Optional[str] = None
    source: str = ""

    def __post_init__(self) -> None:
        unit = self.unit.upper()
        if unit not in {"C", "F"}:
            raise ValueError(f"unsupported station unit: {self.unit!r}")


# Only stations with a traceable origin are registered.  Entries below mirror
# `../pm-weather/locations.json`; **verify each one against the contract's
# resolution rules before trading it**.  Add stations here as they are
# confirmed — never infer a station from a city name at runtime.
STATIONS: Dict[str, Station] = {
    "london": Station(
        city="london",
        name="London City Airport",
        latitude=51.505278,
        longitude=0.055278,
        timezone="Europe/London",
        unit="C",
        wmo_id="EGLC",
        source="pm-weather/locations.json",
    ),
    "paris": Station(
        city="paris",
        name="Paris Charles de Gaulle",
        latitude=49.009722,
        longitude=2.547778,
        timezone="Europe/Paris",
        unit="C",
        wmo_id="LFPG",
        source="pm-weather/locations.json",
    ),
    "amsterdam": Station(
        city="amsterdam",
        name="Amsterdam Schiphol",
        latitude=52.308889,
        longitude=4.761667,
        timezone="Europe/Amsterdam",
        unit="C",
        wmo_id="EHAM",
        source="pm-weather/locations.json",
    ),
}


def normalise_city(city: str) -> str:
    """Normalise a config/slug city name (`New-York` → `new york`)."""
    return city.strip().lower().replace("-", " ")


def register_station(station: Station) -> Station:
    """Add or replace a station definition (used when a contract is verified)."""
    STATIONS[normalise_city(station.city)] = station
    return station


def resolve_station(city: str) -> Station:
    """Look up the verified station for `city`; raises `StationNotFound`."""
    key = normalise_city(city)
    station = STATIONS.get(key)
    if station is None:
        raise StationNotFound(
            f"no verified resolution station for {city!r}; add it to "
            f"forecast/station.py after reading the contract's resolution rules"
        )
    return station
