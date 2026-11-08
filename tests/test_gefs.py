"""GEFS archive maths: lead times, local windows, station grouping, scoring."""
from datetime import date, datetime, timezone

import pytest

import forecast.gefs as G
from eval import analyze
from forecast.station import resolve_station


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ── Local day / lead times ──────────────────────────────────────────────────

def test_local_day_window_respects_dst():
    # London is BST (UTC+1) on 2026-10-15 and GMT (UTC+0) on 2026-11-15.
    start, end = G.local_day_window(date(2026, 10, 15), "Europe/London")
    assert (start, end) == (utc(2026, 10, 14, 23), utc(2026, 10, 15, 23))

    start, end = G.local_day_window(date(2026, 11, 15), "Europe/London")
    assert (start, end) == (utc(2026, 11, 15, 0), utc(2026, 11, 16, 0))


def test_decision_time_is_before_local_midnight():
    assert G.decision_time(date(2026, 10, 15), "Europe/London", 24) == \
        utc(2026, 10, 13, 23)


def test_forecast_hours_cover_the_local_day_at_three_hourly_steps():
    cycle = utc(2026, 10, 13, 18)
    start, end = G.local_day_window(date(2026, 10, 15), "Europe/London")
    assert G.forecast_hours(cycle, start, end) == list(range(30, 52, 3))


def test_forecast_hours_reject_unreachable_windows():
    cycle = utc(2026, 10, 13, 18)
    with pytest.raises(ValueError):
        G.forecast_hours(cycle, utc(2026, 10, 12), utc(2026, 10, 13))


# ── Multi-station grouping ─────────────────────────────────────────────────

def test_daily_maxima_share_downloads_across_stations(monkeypatch):
    london = resolve_station("London")
    paris = resolve_station("Paris")
    entries = [(london, london.timezone), (paris, paris.timezone)]

    calls = []

    def fake_fetch(cycle, hours, points, **kwargs):
        calls.append((cycle, tuple(hours), tuple(p.key for p in points)))
        values = {}
        for member in kwargs["members"]:
            for index, fxx in enumerate(hours):
                values[(member, fxx)] = {
                    point.key: 10.0 + index + (1.0 if point.latitude < 50 else 0.0)
                    for point in points
                }
        return values

    monkeypatch.setattr(G, "fetch_ensemble", fake_fetch)

    result = G.daily_maxima_for_day(
        date(2026, 10, 15), entries, members=("gec00", "gep01"),
    )

    # One cycle group, so one shared fetch for both stations.
    assert len(calls) == 1
    assert len(result) == 2

    london_max = max(result["EGLC"]["maxima"].values())
    paris_max = max(result["LFPB"]["maxima"].values())
    # Paris is north of 50°N in this fixture → +1; both see the last hour.
    assert london_max == pytest.approx(10.0 + 7)
    assert paris_max == pytest.approx(11.0 + 7)


# ── Scoring ─────────────────────────────────────────────────────────────────

def test_multiclass_brier_is_mean_squared_error_over_buckets():
    perfect = [{"a": 1.0, "b": 0.0}]
    coin = [{"a": 0.5, "b": 0.5}]
    assert analyze.multiclass_brier(perfect, ["a"]) == 0.0
    assert analyze.multiclass_brier(coin, ["a"]) == pytest.approx(0.25)


def test_skill_score_positive_when_model_beats_market():
    model = [{"a": 0.9, "b": 0.1}, {"a": 0.1, "b": 0.9}]
    market = [{"a": 0.6, "b": 0.4}, {"a": 0.4, "b": 0.6}]
    winners = ["a", "b"]
    score = analyze.skill_score(model, market, winners)
    assert score["brier_skill_score"] > 0
    assert score["log_loss_model"] < score["log_loss_market"]
    assert analyze.skill_score(market, model, winners)["brier_skill_score"] < 0


def test_edge_summary_counts_large_disagreements():
    model = [{"a": 0.60, "b": 0.40}]
    market = [{"a": 0.40, "b": 0.60}]
    edge = analyze.edge_summary(model, market, threshold=0.02)
    assert edge["observations"] == 2
    assert edge["mean_edge"] == pytest.approx(0.0)
    assert edge["mean_abs_edge"] == pytest.approx(0.2)
    assert edge["share_edge_gt_threshold"] == pytest.approx(0.5)


def test_select_rows_filters_city_and_date():
    rows = [
        {"city": "London", "date": "2026-09-15"},
        {"city": "Paris", "date": "2026-09-15"},
        {"city": "London", "date": "2026-10-01"},
        {"city": "London", "date": "bad"},
    ]
    selected = analyze.select_rows(
        rows, ["London"], date(2026, 9, 1), date(2026, 9, 30)
    )
    assert selected == [rows[0]]


# ── Kernel calibration ──────────────────────────────────────────────────────

def test_kernel_probabilities_sum_to_one_and_smooth_tails():
    from forecast.buckets import buckets_from_labels
    from forecast.calibrate import kernel_bucket_probabilities

    buckets = buckets_from_labels(
        ["19°C or below", "20°C", "21°C", "22°C", "23°C or higher"]
    )
    # All members sit at 21.5, i.e. below the 23°C bucket.
    probs = kernel_bucket_probabilities([21.5] * 8, buckets, sigma_c=0.75)
    assert sum(probs.values()) == pytest.approx(1.0)
    assert probs["23°C or higher"] > 0.0        # tail no longer zero
    assert probs["21°C"] > probs["23°C or higher"]


def test_kernel_probabilities_reject_bad_inputs():
    from forecast.buckets import buckets_from_labels
    from forecast.calibrate import kernel_bucket_probabilities

    buckets = buckets_from_labels(["19°C or below", "20°C or higher"])
    with pytest.raises(ValueError):
        kernel_bucket_probabilities([], buckets)
    with pytest.raises(ValueError):
        kernel_bucket_probabilities([20.0], buckets, sigma_c=0.0)


# ── Bias / spread calibration ──────────────────────────────────────────────

def test_winner_value_uses_bucket_integer():
    from forecast.calibrate import winner_value
    assert winner_value("23°C") == 23.0
    assert winner_value("5°C or below") == 5.0
    assert winner_value("26°C or higher") == 26.0
    assert winner_value("51-52°F") == 51.5


def test_corrected_maxima_recentre_and_scale():
    from forecast.calibrate import corrected_maxima
    members = [19.0, 20.0, 21.0]          # mean 20
    corrected = corrected_maxima(members, centre=22.0, bias=0.5, scale=2.0)
    assert corrected == pytest.approx([22.5 - 2.0, 22.5, 22.5 + 2.0])
    # No centre: anomalies are used around the ensemble mean.
    assert corrected_maxima(members, bias=1.0) == pytest.approx([20.0, 21.0, 22.0])


def test_fit_calibration_recovers_a_cold_bias():
    from forecast.buckets import buckets_from_labels
    from forecast.calibrate import fit_calibration
    buckets = buckets_from_labels(
        ["18°C or below", "19°C", "20°C", "21°C", "22°C or higher"]
    )
    samples = [
        {"members": [19.6, 20.0, 20.4], "centre": 20.0,
         "buckets": buckets, "winner": "21°C"}      # centre is 1 °C too cold
        for _ in range(20)
    ]
    calibration = fit_calibration(samples)
    assert calibration.bias == pytest.approx(+1.0)
    assert calibration.scale in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
    assert calibration.sigma in (0.5, 0.75, 1.0, 1.25)
    assert calibration.samples == 20


# ── GFS hourly centre ──────────────────────────────────────────────────────

def test_deterministic_daily_maxima_use_hourly_leads(monkeypatch):
    london = resolve_station("London")
    calls = []

    def fake_fetch(cycle, hours, points, **kwargs):
        calls.append((cycle, tuple(hours)))
        return {hour: {points[0].key: 10.0 + (hour - hours[0])}
                for hour in hours}

    monkeypatch.setattr(G, "fetch_gfs_points", fake_fetch)
    result = G.deterministic_daily_maxima_for_day(
        date(2026, 9, 15), [(london, london.timezone)],
    )
    cycle, hours = calls[0]
    assert all(hours[i + 1] - hours[i] == 1 for i in range(len(hours) - 1))
    assert result["EGLC"]["max"] == pytest.approx(10.0 + len(hours) - 1)
