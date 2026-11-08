"""After-cost taker edge and reliability reporting for the Phase 1 gate."""
from datetime import date

import pytest

from eval import analyze
from forecast.buckets import buckets_from_labels
from forecast.contracts import ContractSpec

LABELS = ("19°C or below", "20°C", "21°C or higher")


def make_sample(slug="slug-a", city="London", winner="20°C",
                prices=None) -> analyze.Sample:
    buckets = tuple(buckets_from_labels(LABELS))
    spec = ContractSpec(
        city=city,
        series="test",
        station_name="Test",
        icao=None,
        timezone="Europe/London",
        unit="C",
        rounding=None,
        source="metar",
        labels=LABELS,
        buckets=buckets,
    )
    return analyze.Sample(
        slug=slug,
        city=city,
        station="EGLL",
        day=date(2026, 9, 1),
        spec=spec,
        winner=winner,
        market=dict(prices or {
            "19°C or below": 0.3,
            "20°C": 0.3,
            "21°C or higher": 0.4,
        }),
    )


# ── Cost model ──────────────────────────────────────────────────────────────

def test_cost_model_sums_spread_and_fee():
    costs = analyze.CostModel(half_spread=0.01, fee=0.002)
    assert costs.per_share == pytest.approx(0.012)


def test_polymarket_fee_is_rate_times_p_times_one_minus_p():
    costs = analyze.CostModel(fee_rate=0.05)
    assert costs.fee_at(0.50) == pytest.approx(0.0125)
    assert costs.fee_at(0.10) == pytest.approx(0.0045)
    assert costs.fee_at(0.90) == pytest.approx(0.0045)


def test_fee_rate_can_turn_an_edge_negative():
    prices = {"19°C or below": 0.50, "20°C": 0.25,
              "21°C or higher": 0.25}
    sample = make_sample(prices=prices)
    costs = analyze.CostModel(fee_rate=0.05)

    thin = [{"19°C or below": 0.51, "20°C": 0.24,
             "21°C or higher": 0.25}]
    assert analyze.taker_trades([sample], thin, costs) == []

    fatter = [{"19°C or below": 0.55, "20°C": 0.225,
               "21°C or higher": 0.225}]
    trades = analyze.taker_trades([sample], fatter, costs)
    assert [trade.label for trade in trades] == ["19°C or below"]
    assert trades[0].fee == pytest.approx(0.05 * 0.5 * 0.5)


def test_taker_trade_pays_the_crossing_cost():
    sample = make_sample(prices={
        "19°C or below": 0.30,
        "20°C": 0.30,
        "21°C or higher": 0.40,
    })
    model = [{"19°C or below": 0.70, "20°C": 0.30, "21°C or higher": 0.00}]
    trades = analyze.taker_trades(
        [sample], model, analyze.CostModel(half_spread=0.01, fee=0.001)
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.label == "19°C or below"
    assert trade.price == pytest.approx(0.31)
    assert trade.edge == pytest.approx(0.70 - 0.31 - 0.001)
    assert trade.price + trade.fee == pytest.approx(0.311)


def test_taker_trades_respect_the_threshold():
    sample = make_sample()
    model = [{"19°C or below": 0.50, "20°C": 0.50, "21°C or higher": 0.00}]

    assert len(analyze.taker_trades([sample], model, analyze.CostModel())) == 2
    assert analyze.taker_trades(
        [sample], model, analyze.CostModel(), threshold=0.25
    ) == []


def test_after_cost_summary_accounts_for_wins_and_stake():
    trades = [
        analyze.TakerTrade(slug="a", city="London", label="20°C",
                           model_p=0.5, mid=0.4, price=0.41, fee=0.0,
                           won=True),
        analyze.TakerTrade(slug="b", city="Paris", label="21°C",
                           model_p=0.4, mid=0.3, price=0.31, fee=0.0,
                           won=False),
    ]
    summary = analyze.after_cost_summary(trades)

    assert summary["trades"] == 2
    assert summary["contracts"] == 2
    assert summary["staked"] == pytest.approx(0.72)
    assert summary["pnl"] == pytest.approx((1 - 0.41) - 0.31)
    assert summary["hit_rate"] == pytest.approx(0.5)
    assert summary["mean_edge"] == pytest.approx((0.09 + 0.09) / 2)
    assert summary["roi"] == pytest.approx(summary["pnl"] / 0.72)


def test_after_cost_summary_of_no_trades_is_zero():
    summary = analyze.after_cost_summary([])
    assert summary["trades"] == 0
    assert summary["pnl"] == 0.0
    assert summary["roi"] == 0.0


def test_after_cost_report_sensitivity_shrinks_with_threshold():
    samples = [
        make_sample(slug="a", winner="20°C",
                    prices={"19°C or below": 0.3, "20°C": 0.3,
                            "21°C or higher": 0.4}),
        make_sample(slug="b", winner="21°C or higher",
                    prices={"19°C or below": 0.4, "20°C": 0.4,
                            "21°C or higher": 0.2}),
    ]
    model = [{"19°C or below": 0.10, "20°C": 0.20, "21°C or higher": 0.70},
             {"19°C or below": 0.05, "20°C": 0.05, "21°C or higher": 0.90}]
    report = analyze.after_cost_report(
        samples, model, analyze.CostModel(half_spread=0.01),
        threshold=0.0, thresholds=(0.0, 0.05, 0.45),
    )

    assert report["contract_share"] == pytest.approx(1.0)
    assert set(report["by_city"]) == {"London"}
    counts = [row["summary"]["trades"] for row in report["thresholds"]]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] > counts[-1]
    assert report["summary"]["trades"] == counts[0]


# ── Reliability ─────────────────────────────────────────────────────────────

def test_flatten_outcomes_labels_every_bucket():
    probs = [{"a": 0.7, "b": 0.3}, {"a": 0.2, "b": 0.8}]
    ps, outcomes = analyze.flatten_outcomes(probs, ["a", "b"])
    assert ps == [0.7, 0.3, 0.2, 0.8]
    assert outcomes == [1.0, 0.0, 0.0, 1.0]


def test_reliability_summary_zero_ece_for_calibrated_forecasts():
    # Ten forecasts at 0.2 for `a`; two of them win.
    probs = [{"a": 0.2, "b": 0.8} for _ in range(10)]
    winners = ["a"] * 2 + ["b"] * 8
    summary = analyze.reliability_summary(probs, winners, n_bins=10)

    assert summary["observations"] == 20
    assert summary["ece"] == pytest.approx(0.0)
    assert len(summary["bins"]) == 2


def test_reliability_summary_flags_overconfidence():
    probs = [{"a": 0.8, "b": 0.2} for _ in range(5)]
    winners = ["b"] * 5
    summary = analyze.reliability_summary(probs, winners, n_bins=10)
    assert summary["ece"] == pytest.approx(0.8)
