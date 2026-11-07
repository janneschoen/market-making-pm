"""Price-history join: point-in-time decisions, forward-fill, coverage."""
from datetime import datetime, timedelta, timezone

import pytest

import eval.prices as P


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


ROW = {
    "slug": "highest-temperature-in-tokyo-on-june-12-2026",
    "city": "Tokyo",
    "date": "2026-06-12",
    "winner": "20°C",
    "created_at": "2026-06-09T00:00:00Z",
    "spec": {"timezone": "Asia/Tokyo"},
    "markets": [
        {"label": "19°C", "token_ids": ["tok-a", "no-a"]},
        {"label": "20°C", "token_ids": ["tok-b", "no-b"]},
    ],
}


# ── Decision times ──────────────────────────────────────────────────────────

def test_decisions_are_anchored_to_the_local_day():
    decisions = P.decision_times(ROW)
    # Tokyo local midnight on 2026-06-12 is 15:00Z on 2026-06-11.
    assert decisions == {
        "T-24h": utc(2026, 6, 10, 15),
        "T-12h": utc(2026, 6, 11, 3),
    }
    assert all(moment.tzinfo is not None for moment in decisions.values())


def test_decisions_far_east_never_start_after_the_local_day():
    wellington = dict(ROW, city="Wellington",
                      spec={"timezone": "Pacific/Auckland"})
    local_start = utc(2026, 6, 11, 12)      # 00:00 NZST on 2026-06-12
    decisions = P.decision_times(wellington)
    assert decisions == {
        "T-24h": local_start - timedelta(hours=24),
        "T-12h": local_start - timedelta(hours=12),
    }


def test_decisions_need_a_timezone():
    row = dict(ROW, spec={})
    assert P.decision_times(row) == {}


def test_window_covers_all_decisions():
    window = P.window_for(P.decision_times(ROW))
    start, end = window
    assert start == int(utc(2026, 6, 10, 15).timestamp()) - 12 * 3600
    assert end == int(utc(2026, 6, 11, 3).timestamp()) + 3600


# ── Price lookup ────────────────────────────────────────────────────────────

def test_price_at_forward_fills_and_respects_staleness():
    history = [(1000, 0.40), (2000, 0.55)]
    assert P.price_at(history, 2000) == pytest.approx(0.55)
    assert P.price_at(history, 2500, max_staleness_s=600) == pytest.approx(0.55)
    assert P.price_at(history, 3000, max_staleness_s=600) is None
    assert P.price_at(history, 500) is None
    assert P.price_at([], 2000) is None


# ── Assembly ────────────────────────────────────────────────────────────────

def _histories(price_a, price_b):
    decisions = P.decision_times(ROW)
    stamp = int(min(m.timestamp() for m in decisions.values()))
    return {"tok-a": [(stamp - 60, price_a)], "tok-b": [(stamp - 60, price_b)]}


def test_assemble_complete_distribution():
    joined = P.assemble(ROW, _histories(0.4, 0.55))
    entry = joined["decisions"]["T-24h"]
    assert entry["available"] and entry["complete"]
    assert entry["prices"] == {"19°C": pytest.approx(0.4),
                               "20°C": pytest.approx(0.55)}
    assert entry["sum"] == pytest.approx(0.95)


def test_assemble_reports_missing_buckets():
    joined = P.assemble(ROW, {"tok-a": [(int(utc(2026, 6, 10, 15).timestamp()) - 60, 0.4)]})
    entry = joined["decisions"]["T-24h"]
    assert entry["available"] and not entry["complete"]
    assert entry["missing"] == ["20°C"]
    assert entry["prices"] == {"19°C": pytest.approx(0.4)}


def test_assemble_marks_contracts_not_listed_yet():
    row = dict(ROW, created_at="2026-06-11T00:00:00Z")   # after T-24h
    stamp = int(P.decision_times(row)["T-12h"].timestamp()) - 60
    histories = {"tok-a": [(stamp, 0.4)], "tok-b": [(stamp, 0.5)]}
    joined = P.assemble(row, histories)
    assert joined["decisions"]["T-24h"]["available"] is False
    assert joined["decisions"]["T-24h"]["reason"] == "not_listed"
    assert joined["decisions"]["T-12h"]["available"] is True


# ── Fetch + cache ───────────────────────────────────────────────────────────

def test_fetch_history_caches_and_sorts(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_get_json(url, params, timeout=20):
        calls["n"] += 1
        assert params["market"] == "tok-a"
        return {"history": [{"t": 2000, "p": 0.6}, {"t": 1000, "p": 0.5}]}

    monkeypatch.setattr(P, "_get_json", fake_get_json)
    cache = str(tmp_path)

    points, from_cache = P.fetch_history("tok-a", 900, 2100, 60, cache)
    assert points == [(1000, 0.5), (2000, 0.6)]
    assert from_cache is False

    points, from_cache = P.fetch_history("tok-a", 900, 2100, 60, cache)
    assert points == [(1000, 0.5), (2000, 0.6)]
    assert from_cache is True
    assert calls["n"] == 1


def test_fetch_history_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_get_json", lambda *a, **k: None)
    points, from_cache = P.fetch_history("tok-a", 900, 2100, 60, str(tmp_path))
    assert points == [] and from_cache is False


def test_write_rows_roundtrip(tmp_path):
    path = str(tmp_path / "prices.jsonl")
    rows = [{"slug": "a", "x": 1}, {"slug": "b", "x": 2}]
    assert P.write_rows(rows, path) == 2
    loaded = P.load_rows(path)
    assert loaded == rows
