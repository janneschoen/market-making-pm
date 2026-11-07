"""Bucket parity: rounding, membership and partition validation."""
import pytest

from forecast.buckets import (
    Bucket,
    BucketParityError,
    Rounding,
    bucket_probabilities,
    buckets_from_labels,
    convert,
    round_temp,
    validate_partition,
)

LABELS = ["11°C or below", "12°C", "13°C", "14°C or above"]


def test_rounding_conventions():
    assert round_temp(11.4) == 11
    assert round_temp(11.5) == 12          # floor(x + 0.5), as pm-weather did
    assert round_temp(12.9) == 13
    assert round_temp(11.5, Rounding.FLOOR) == 11
    assert round_temp(-0.4, Rounding.TRUNCATE) == 0


def test_parse_outcome_labels():
    at_most = Bucket.parse("12°C or below")
    assert (at_most.lower, at_most.upper) == (None, 12)
    assert at_most.is_open_lower

    exact = Bucket.parse("13°C")
    assert (exact.lower, exact.upper) == (13, 13)
    assert exact.is_exact

    at_least = Bucket.parse("20°C or above")
    assert (at_least.lower, at_least.upper) == (20, None)
    assert at_least.is_open_upper

    symbol = Bucket.parse("≤ 12 °C")
    assert (symbol.lower, symbol.upper) == (None, 12)


def test_unparseable_label_is_a_parity_error():
    with pytest.raises(BucketParityError):
        Bucket.parse("warmer than average")


def test_contains_applies_contract_rounding():
    buckets = buckets_from_labels(LABELS)
    assert buckets[0].contains(11.2)
    assert buckets[1].contains(11.5)       # rounds up into 12
    assert buckets[1].contains(12.49)
    assert buckets[2].contains(12.5)
    assert buckets[3].contains(40.0)


def test_bucket_probabilities_partition_members():
    buckets = buckets_from_labels(LABELS)
    probs = bucket_probabilities([11.2, 12.49, 12.5, 13.7, 20.0], buckets)

    assert probs["11°C or below"] == pytest.approx(0.2)
    assert probs["12°C"] == pytest.approx(0.2)
    assert probs["13°C"] == pytest.approx(0.2)
    assert probs["14°C or above"] == pytest.approx(0.4)
    assert sum(probs.values()) == pytest.approx(1.0)


def test_gap_fails_parity():
    buckets = [Bucket.at_most(11), Bucket.exact(13), Bucket.at_least(14)]
    with pytest.raises(BucketParityError, match="gap"):
        validate_partition(buckets)


def test_overlap_fails_parity():
    buckets = [Bucket.at_most(12), Bucket.exact(12), Bucket.at_least(13)]
    with pytest.raises(BucketParityError, match="overlap"):
        validate_partition(buckets)


def test_missing_tails_fail_parity():
    with pytest.raises(BucketParityError, match="low tail"):
        validate_partition([Bucket.exact(12), Bucket.at_least(13)])
    with pytest.raises(BucketParityError, match="high tail"):
        validate_partition([Bucket.at_most(12), Bucket.exact(13)])


def test_duplicate_labels_fail_parity():
    buckets = [Bucket.at_most(12), Bucket.exact(13),
               Bucket.at_least(14, label="13°C")]
    with pytest.raises(BucketParityError, match="duplicate"):
        validate_partition(buckets)


def test_fahrenheit_buckets_convert_the_observation():
    bucket = Bucket.at_most(50, unit="F")
    assert bucket.contains(10.0)           # 10 °C == 50 °F → 50 ≤ 50
    assert not bucket.contains(10.5)       # 50.9 °F → rounds to 51 → outside


# ── Real label families observed on Polymarket ------------------------------

def test_parse_two_degree_fahrenheit_ranges():
    hyphen = Bucket.parse("51-52°F")
    assert (hyphen.lower, hyphen.upper, hyphen.unit) == (51, 52, "F")

    en_dash = Bucket.parse("25–26°F")
    assert (en_dash.lower, en_dash.upper) == (25, 26)

    spaced = Bucket.parse("51 – 52 °F")
    assert (spaced.lower, spaced.upper) == (51, 52)


def test_parse_alternate_label_spellings():
    assert Bucket.parse("<51°F").upper == 51
    assert Bucket.parse("3 °C or below").upper == 3
    assert Bucket.parse("35°F or lower").upper == 35
    assert Bucket.parse("35°F or above").lower == 35
    unitless = Bucket.parse("23 or higher", unit="C")
    assert (unitless.lower, unitless.unit) == (23, "C")


def test_two_degree_ranges_partition_cleanly():
    labels = ["24°F or below", "25–26°F", "27–28°F", "29–30°F",
              "31–32°F", "33–34°F", "35°F or higher"]
    buckets = buckets_from_labels(labels, unit="F")
    # Member temperatures are supplied in °C, as everywhere else.
    members = [convert(v, "F", "C") for v in (26.0, 30.0, 40.0)]
    probs = bucket_probabilities(members, buckets, validate=True)
    assert probs["25–26°F"] == pytest.approx(1 / 3)
    assert probs["29–30°F"] == pytest.approx(1 / 3)
    assert probs["35°F or higher"] == pytest.approx(1 / 3)
    assert sum(probs.values()) == pytest.approx(1.0)


def test_range_parity_errors():
    with pytest.raises(BucketParityError):
        Bucket.parse("52-51°F")                     # inverted
    with pytest.raises(BucketParityError):
        Bucket.parse("51-52°F or below")            # ambiguous bound
    with pytest.raises(BucketParityError):
        validate_partition([Bucket.parse("51-52°F"),
                            Bucket.parse("53-54°F"),
                            Bucket.parse("55-56°F")])   # missing tails
