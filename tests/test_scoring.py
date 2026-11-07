"""Forecast scoring: Brier, log-loss, reliability."""
import math

import pytest

from measure.scoring import (
    brier_score,
    brier_skill_score,
    expected_calibration_error,
    log_loss,
    reliability_bins,
)


def test_brier_score_of_perfect_and_coin_flip_forecasts():
    assert brier_score([1.0, 0.0], [1, 0]) == pytest.approx(0.0)
    assert brier_score([0.5], [1]) == pytest.approx(0.25)
    assert brier_score([0.0, 1.0], [1, 0]) == pytest.approx(1.0)


def test_brier_skill_score_sign():
    outcomes = [1, 0, 1, 0]
    good_model = [0.9, 0.1, 0.8, 0.2]
    market = [0.6, 0.4, 0.6, 0.4]
    assert brier_skill_score(good_model, market, outcomes) > 0
    assert brier_skill_score(market, good_model, outcomes) < 0


def test_log_loss_penalises_confident_mistakes():
    cautious = log_loss([0.5], [1])
    wrong = log_loss([0.01], [1])
    assert cautious == pytest.approx(math.log(2))
    assert wrong > cautious
    assert math.isfinite(wrong)          # clipping keeps it finite


def test_length_mismatch_is_rejected():
    with pytest.raises(ValueError):
        brier_score([0.5], [1, 0])
    with pytest.raises(ValueError):
        brier_score([], [])


def test_reliability_bins_group_by_predicted_probability():
    bins = reliability_bins([0.05, 0.15, 0.95], [0, 0, 1], n_bins=10)
    assert [b.count for b in bins] == [1, 1, 1]
    assert bins[0].mean_predicted == pytest.approx(0.05)
    assert bins[0].observed_frequency == pytest.approx(0.0)
    assert bins[2].observed_frequency == pytest.approx(1.0)


def test_expected_calibration_error_of_calibrated_forecasts_is_small():
    probabilities = [0.2] * 5 + [0.8] * 5
    outcomes = [1, 0, 0, 0, 0] + [1, 1, 1, 1, 0]
    assert expected_calibration_error(probabilities, outcomes) == pytest.approx(0.0)

    overconfident = [0.9] * 10
    outcomes = [0] * 5 + [1] * 5
    assert expected_calibration_error(overconfident, outcomes) > 0.1
