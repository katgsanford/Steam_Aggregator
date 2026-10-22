"""db.review_stats (which replaced analyze._basic_stats) and the analyze paths that use it."""

from __future__ import annotations

import json
import time

import pytest

import analyze
import db
import synthetic as s

VIEWS = [
    (s.APP_A, None, None, None),
    (s.APP_A, "all", None, None),
    (s.APP_A, "english", None, None),
    (s.APP_A, "chinese", None, None),                          # bucket key
    (s.APP_A, "german", s.T0 + 5 * s.DAY, s.T0 + 10 * s.DAY),  # a release window
    (s.APP_A, "all", s.T0 + 10 * s.DAY, None),                 # open-ended window
    (s.APP_B, "english", None, None),
    (s.APP_A, "french", None, None),                           # no reviews at all
    (999, None, None, None),                                   # unknown title
]


def _old_basic_stats(conn, appid, language_filter, ts_from, ts_to) -> dict:
    """The removed analyze._basic_stats, verbatim, as the reference implementation."""
    rows = db.sentiment_rows(conn, appid, language=language_filter, ts_from=ts_from, ts_to=ts_to)
    total = len(rows)
    positive = sum(1 for _, up in rows if up)
    pct = round(100 * positive / total, 1) if total else 0.0
    return {"total_reviews": total, "positive": positive, "negative": total - positive, "percent_positive": pct}


@pytest.mark.parametrize("appid,lang,ts_from,ts_to", VIEWS)
def test_review_stats_matches_old_basic_stats(conn, appid, lang, ts_from, ts_to):
    got = db.review_stats(conn, appid, language=lang, ts_from=ts_from, ts_to=ts_to)
    assert got == _old_basic_stats(conn, appid, lang, ts_from, ts_to)
    assert got == s.stats(appid, lang, ts_from, ts_to)
    assert all(isinstance(got[k], int) for k in ("total_reviews", "positive", "negative"))


def test_review_stats_empty_is_zero(conn):
    assert db.review_stats(conn, 999) == {"total_reviews": 0, "positive": 0, "negative": 0, "percent_positive": 0.0}


def _no_llm(monkeypatch):
    def boom():
        raise AssertionError("get_analysis tried to call the API")
    monkeypatch.setattr(analyze, "_client", boom)


def test_get_analysis_insufficient_uses_review_stats(conn, monkeypatch):
    """A primary view under MIN_REVIEWS returns stats only, with no model call."""
    _no_llm(monkeypatch)
    out = analyze.get_analysis(conn, s.APP_B, scope="all", language_filter="all", ts_from=None, ts_to=None,
                               product_name="Beta Game", include_recent=True, enforce_minimums=True)
    assert out["insufficient"] is True
    assert out["stats"] == s.stats(s.APP_B) and out["review_count"] == 10


def test_get_analysis_cached_view_stats_and_recent_stats(conn, monkeypatch):
    """With every artifact cached and fresh, get_analysis serves stats + 7-day stats from SQL only."""
    _no_llm(monkeypatch)
    total = s.stats(s.APP_A)["total_reviews"]
    payloads = {
        "themes_good": [], "themes_bad": [], "themes_recent": {"good": [], "bad": []},
        "summary_product": {"text": "product summary"}, "summary_qa": {"text": "qa summary"},
    }
    for kind, payload in payloads.items():
        db.save_analysis(conn, s.APP_A, "all", "all", kind, total, json.dumps(payload))
    conn.commit()
    out = analyze.get_analysis(conn, s.APP_A, scope="all", language_filter="all", ts_from=None, ts_to=None,
                               product_name="Alpha Game", include_recent=True, enforce_minimums=True)
    assert out["stats"] == s.stats(s.APP_A) and out["insufficient"] is False
    assert out["summary_qa"] == "qa summary"
    recent_from = int(time.time()) - analyze.WEEK_SECONDS
    assert out["recent_stats"] == s.stats(s.APP_A, "all", recent_from)
    assert out["recent_stats"]["total_reviews"] == 3
