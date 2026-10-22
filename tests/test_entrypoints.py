"""The existing entry points, reached through config.db_target() and DATABASE_URL.

Each test sets DATABASE_URL for its backend (sqlite:///<tmp file>, or the libSQL
test server), so these exercise the same switch a real run uses.
"""

from __future__ import annotations

import json
import sys

import pytest

import analyze
import clear_cache
import config
import db
import export
import synthetic as s


@pytest.fixture
def seeded_url(conn, database_url, monkeypatch):
    """DATABASE_URL pointing at the seeded synthetic DB."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    return database_url


def test_export_all_and_by_appid(seeded_url, tmp_path, monkeypatch, capsys):
    out = tmp_path / "export.json"
    monkeypatch.setattr(sys, "argv", ["export.py", "--out", str(out)])
    export.main()
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert len(rows) == len(s.REVIEWS)
    assert {r["title"] for r in rows} == {"Alpha Game", "Beta Game"}
    assert [r["timestamp_created"] for r in rows] == sorted((r["timestamp_created"] for r in rows), reverse=True)

    monkeypatch.setattr(sys, "argv", ["export.py", "--appid", str(s.APP_B), "--out", str(out)])
    export.main()
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert len(rows) == 10 and all(r["appid"] == s.APP_B and r["title"] == "Beta Game" for r in rows)
    assert "Wrote 10 reviews" in capsys.readouterr().out


def test_clear_cache_reports_rowcount(conn, seeded_url, monkeypatch, capsys):
    for kind in ("summary_product", "summary_qa", "themes_good"):
        db.save_analysis(conn, s.APP_A, "all", "all", kind, 1, "{}")
    conn.commit()
    monkeypatch.setattr(sys, "argv", ["clear_cache.py"])
    assert clear_cache.main() == 0
    assert "Cleared 2 cached row(s)" in capsys.readouterr().out
    left = [r["kind"] for r in conn.execute("SELECT kind FROM analyses").fetchall()]
    assert left == ["themes_good"]


def test_setup_load_db_path_writes_config_once(tmp_path):
    import setup

    assert not config.CONFIG_PATH.exists()
    assert setup.load_db_path() == str(config.DEFAULT_DB_PATH)
    assert json.loads(config.CONFIG_PATH.read_text(encoding="utf-8")) == {"db_path": str(config.DEFAULT_DB_PATH)}
    # A hand-edited config.json is respected and left alone.
    custom = str(tmp_path / "elsewhere.db")
    config.CONFIG_PATH.write_text(json.dumps({"db_path": custom}), encoding="utf-8")
    assert setup.load_db_path() == custom
    assert json.loads(config.CONFIG_PATH.read_text(encoding="utf-8")) == {"db_path": custom}


@pytest.fixture
def client(seeded_url, monkeypatch):
    import app as app_module

    def no_llm():
        raise AssertionError("dashboard tried to call the API")
    monkeypatch.setattr(analyze, "_client", no_llm)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_dashboard_endpoints(client, conn):
    titles = client.get("/api/titles").get_json()
    assert [t["appid"] for t in titles] == [s.APP_A, s.APP_B]

    meta = client.get(f"/api/meta?appid={s.APP_A}").get_json()
    assert set(meta["languages"]) >= set(s.LANGS_A)
    assert [sc["id"] for sc in meta["scopes"]] == ["all", "news_2", "news_1", "launch"]
    assert meta["last_collected"] == s.FETCHED_1

    breakdown = client.get(f"/api/breakdown?appid={s.APP_A}&scope=news_1").get_json()
    window = s.matching(s.APP_A, None, s.T0 + 5 * s.DAY, s.T0 + 10 * s.DAY)
    assert sum(v["count"] for k, v in breakdown["lang_stats"].items()
               if k in s.LANGS_A) == len(window)

    sent = client.get(f"/api/sentiment?appid={s.APP_A}&scope=all&lang=chinese").get_json()
    exp = s.stats(s.APP_A, "chinese")
    assert sent["summary"] == {"total": exp["total_reviews"], "percent_positive": exp["percent_positive"]}
    assert len(sent["callouts"]) == 2  # launch + the major update

    # Too few reviews for a primary view: stats only, no model call.
    small = client.get(f"/api/analysis?appid={s.APP_B}&scope=all&lang=all").get_json()
    assert small["insufficient"] is True and small["stats"] == s.stats(s.APP_B)

    # A past release window with every artifact cached: served without a model call.
    ts_from, ts_to = s.T0 + 5 * s.DAY, s.T0 + 10 * s.DAY
    n = s.stats(s.APP_A, "all", ts_from, ts_to)["total_reviews"]
    for kind, payload in {"themes_good": [], "themes_bad": [], "summary_product": {"text": "p"},
                          "summary_qa": {"text": "q"}}.items():
        db.save_analysis(conn, s.APP_A, "news_1", "all", kind, n, json.dumps(payload))
    conn.commit()
    view = client.get(f"/api/analysis?appid={s.APP_A}&scope=news_1&lang=all").get_json()
    assert "error" not in view, view
    assert view["stats"] == s.stats(s.APP_A, "all", ts_from, ts_to)
    assert view["summary_product"] == "p" and view["summary_qa"] == "q"
