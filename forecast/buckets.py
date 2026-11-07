"""
Contract bucket definitions — exact parity with settlement.

A Polymarket weather market is a set of outcome buckets over the **rounded
daily maximum** at a specific station, e.g.:

    "11°C or below" | "12°C" | "13°C" | … | "20°C or above"

`pm-weather` currently bins with `int(temp + 0.5)`, a rounding convention that
does not correspond to any stated contract rule.  This module makes the
rounding, unit and bucket edges explicit, validates that the buckets actually
partition the outcome space, and can therefore be tested for parity before a
single probability is priced (ROADMAP §3, §5).

Membership is evaluated as: convert the observation to the bucket's unit,
apply the declared rounding convention, then compare against inclusive bounds.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence

INTEGRAL = 1e-9


class Rounding(str, Enum):
    """How a continuous observation becomes a contract outcome."""

    HALF_UP = "half_up"        # floor(x + 0.5): matches `int(x + 0.5)`
    HALF_EVEN = "half_even"    # banker's rounding
    FLOOR = "floor"            # always down
    TRUNCATE = "truncate"      # toward zero


def round_temp(temp_c: float, rounding: Rounding = Rounding.HALF_UP) -> int:
    """Round a temperature to the integer the contract settles on."""
    value = float(temp_c)
    if rounding is Rounding.FLOOR:
        return math.floor(value)
    if rounding is Rounding.TRUNCATE:
        return int(value)
    if rounding is Rounding.HALF_EVEN:
        return round(value)
    return math.floor(value + 0.5)


def convert(temp: float, from_unit: str, to_unit: str) -> float:
    """Convert a temperature between Celsius and Fahrenheit."""
    src, dst = from_unit.upper(), to_unit.upper()
    if src == dst:
        return float(temp)
    if src == "C" and dst == "F":
        return float(temp) * 9.0 / 5.0 + 32.0
    if src == "F" and dst == "C":
        return (float(temp) - 32.0) * 5.0 / 9.0
    raise ValueError(f"unsupported unit conversion: {from_unit} → {to_unit}")


class BucketParityError(ValueError):
    """Buckets do not describe a valid, gap-free partition of outcomes."""


_LABEL_RE = re.compile(
    r"^\s*"
    r"(?:(?P<le>[≤<]=?)|(?P<ge>[≥>]=?))?\s*"
    r"(?P<num>[+-]?\d+(?:\.\d+)?)\s*"
    r"°?\s*(?P<unit>[CF])?\s*"
    r"(?:(?:or|and)\s+(?P<dir>below|lower|less|above|higher|more))?"
    r"\s*$",
    re.IGNORECASE,
)

_LOW_TAIL = {"below", "lower", "less"}
_HIGH_TAIL = {"above", "higher", "more"}


@dataclass(frozen=True)
class Bucket:
    """One contract outcome: an inclusive range on a rounded integer value."""

    label: str
    lower: Optional[float] = None   # inclusive; None = open low tail
    upper: Optional[float] = None   # inclusive; None = open high tail
    unit: str = "C"
    rounding: Rounding = Rounding.HALF_UP

    def __post_init__(self) -> None:
        unit = self.unit.upper()
        if unit not in {"C", "F"}:
            raise ValueError(f"unsupported bucket unit: {self.unit!r}")
        object.__setattr__(self, "unit", unit)
        if self.lower is None and self.upper is None:
            raise BucketParityError(f"bucket {self.label!r} has no bounds")
        if (self.lower is not None and self.upper is not None
                and self.lower > self.upper):
            raise BucketParityError(
                f"bucket {self.label!r}: lower {self.lower} > upper {self.upper}"
            )
        object.__setattr__(self, "rounding", Rounding(self.rounding))

    # ── Construction helpers ─────────────────────────────────────────────

    @classmethod
    def at_most(cls, value: int, label: Optional[str] = None,
                unit: str = "C",
                rounding: Rounding = Rounding.HALF_UP) -> "Bucket":
        return cls(label or f"{value}°{unit} or below", None, value, unit, rounding)

    @classmethod
    def exact(cls, value: int, label: Optional[str] = None,
              unit: str = "C",
              rounding: Rounding = Rounding.HALF_UP) -> "Bucket":
        return cls(label or f"{value}°{unit}", value, value, unit, rounding)

    @classmethod
    def at_least(cls, value: int, label: Optional[str] = None,
                 unit: str = "C",
                 rounding: Rounding = Rounding.HALF_UP) -> "Bucket":
        return cls(label or f"{value}°{unit} or above", value, None, unit, rounding)

    @classmethod
    def parse(cls, label: str, unit: str = "C",
              rounding: Rounding = Rounding.HALF_UP) -> "Bucket":
        """Parse a contract outcome label such as `"12°C or below"`."""
        match = _LABEL_RE.match(label)
        if not match:
            raise BucketParityError(f"unparseable bucket label: {label!r}")

        value = float(match.group("num"))
        parsed_unit = (match.group("unit") or unit).upper()
        direction = (match.group("dir") or "").lower()

        if match.group("le") or direction in _LOW_TAIL:
            return cls(label, None, value, parsed_unit, rounding)
        if match.group("ge") or direction in _HIGH_TAIL:
            return cls(label, value, None, parsed_unit, rounding)
        return cls(label, value, value, parsed_unit, rounding)

    # ── Semantics ────────────────────────────────────────────────────────

    @property
    def is_open_lower(self) -> bool:
        return self.lower is None

    @property
    def is_open_upper(self) -> bool:
        return self.upper is None

    @property
    def is_exact(self) -> bool:
        return (self.lower is not None and self.upper is not None
                and abs(self.lower - self.upper) < INTEGRAL)

    def contains(self, temp_c: float) -> bool:
        """Does an observation (in °C) settle into this bucket?"""
        value = round_temp(convert(temp_c, "C", self.unit), self.rounding)
        if self.lower is not None and value < self.lower:
            return False
        if self.upper is not None and value > self.upper:
            return False
        return True

    def upper_bound_key(self) -> float:
        """Sort key: buckets ordered by their low edge, low tail first."""
        return float("-inf") if self.lower is None else float(self.lower)


def buckets_from_labels(labels: Sequence[str], unit: str = "C",
                        rounding: Rounding = Rounding.HALF_UP) -> List[Bucket]:
    """Parse and validate a contract's full outcome set."""
    buckets = [Bucket.parse(label, unit=unit, rounding=rounding)
               for label in labels]
    validate_partition(buckets)
    return buckets


def validate_partition(buckets: Sequence[Bucket]) -> bool:
    """
    Assert that `buckets` partitions the integers exactly once.

    Requires inclusive integer bounds, one open low tail first, one open high
    tail last, and adjacent buckets to be consecutive (no gaps, no overlaps).
    Raises `BucketParityError` otherwise — a contract whose buckets fail this
    check must not be traded.
    """
    if not buckets:
        raise BucketParityError("no buckets given")

    labels = [b.label for b in buckets]
    if len(set(labels)) != len(labels):
        raise BucketParityError(f"duplicate bucket labels: {labels}")

    for bucket in buckets:
        for bound in (bucket.lower, bucket.upper):
            if bound is None:
                continue
            if abs(bound - round(bound)) > INTEGRAL:
                raise BucketParityError(
                    f"bucket {bucket.label!r} has non-integer bound {bound}; "
                    f"contract buckets are rounded integers"
                )

    ordered = sorted(buckets, key=Bucket.upper_bound_key)

    if ordered[0].lower is not None:
        raise BucketParityError(
            f"low tail is not covered: lowest bucket {ordered[0].label!r} "
            f"starts at {ordered[0].lower}"
        )
    if ordered[-1].upper is not None:
        raise BucketParityError(
            f"high tail is not covered: highest bucket {ordered[-1].label!r} "
            f"ends at {ordered[-1].upper}"
        )

    for previous, following in zip(ordered, ordered[1:]):
        if previous.upper is None:
            raise BucketParityError(
                f"open-ended bucket {previous.label!r} is not last"
            )
        if following.lower is None:
            raise BucketParityError(
                f"open-ended bucket {following.label!r} is not first"
            )
        if following.lower <= previous.upper:
            raise BucketParityError(
                f"buckets {previous.label!r} and {following.label!r} overlap"
            )
        if following.lower > previous.upper + 1:
            raise BucketParityError(
                f"gap between {previous.label!r} and {following.label!r}"
            )
    return True


def bucket_probabilities(member_temps_c: Iterable[float],
                         buckets: Sequence[Bucket],
                         validate: bool = True) -> Dict[str, float]:
    """
    Empirical `P(bucket)` from ensemble member daily maxima.

    Every member must land in exactly one bucket; with `validate=True` the
    partition is checked first and an unmapped member is a parity error, not
    a silently dropped sample.
    """
    if validate:
        validate_partition(buckets)

    members = [float(t) for t in member_temps_c]
    if not members:
        raise ValueError("bucket_probabilities needs at least one member")

    counts = {bucket.label: 0 for bucket in buckets}
    for temp in members:
        for bucket in buckets:
            if bucket.contains(temp):
                counts[bucket.label] += 1
                break
        else:  # pragma: no cover - unreachable for a validated partition
            raise BucketParityError(
                f"member {temp}°C is not covered by any bucket"
            )

    total = len(members)
    return {label: count / total for label, count in counts.items()}
