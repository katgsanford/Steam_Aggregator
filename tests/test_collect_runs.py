"""collect.collect_product writes a runs row for a failed language, not just successful ones."""

from __future__ import annotations

import pytest

import collect
import db
import steam_api
import synthetic as s

APP = 300
FETCHED = s.T0 + 40 * s.DAY


def _fake_iter_reviews(plan: dict):
    """iter_reviews stand-in: yields plan[language] reviews, then raises if the plan says so."""
    def fake(appid, language, stop_before_ts=0, **_kw):
        reviews, error = plan[language]
        for i, ts in enumerate(reviews):
            yield s._review(f"c{language[:2]}{i}", ts, up=True, text=f"{language} {i}", votes=0)
        if error:
            raise steam_api.SteamError(error)
    return fake


@pytest.fixture(autouse=True)
def _no_repo_log(monkeypatch, tmp_path):
    # collect._log appends to collect.log next to collect.py; keep it out of the repo.
    monkeypatch.setattr(collect, "LOG_PATH", tmp_path / "collect.log", raising=False)


def test_failed_language_records_error_run(conn, target, monkeypatch):
    plan = {
        "english": ([s.T0 + 1, s.T0 + 2, s.T0 + 3], None),
        "german": ([s.T0 + 10, s.T0 + 20], "Steam returned HTTP 503 for appreviews"),
    }
    monkeypatch.setattr(steam_api, "iter_reviews", _fake_iter_reviews(plan))
    db.upsert_product(conn, APP, "Gamma", ["english", "german"])
    conn.commit()

    total = collect.collect_product(conn, APP, "Gamma", ["english", "german"], FETCHED)
    assert total == 3  # only the language that finished counts toward the total

    rows = {r["language"]: dict(r) for r in conn.execute(
        "SELECT language, new_count, max_ts_seen, status, error FROM runs WHERE appid=?", (APP,)).fetchall()}
    assert rows["english"] == {"language": "english", "new_count": 3, "max_ts_seen": s.T0 + 3,
                               "status": "complete", "error": None}
    assert rows["german"]["status"] == "error"
    assert "HTTP 503" in rows["german"]["error"]
    assert rows["german"]["new_count"] == 2 and rows["german"]["max_ts_seen"] == s.T0 + 20
    conn.close()

    # Both the error row and the reviews stored before the failure were committed.
    db._SCHEMA_READY.clear()
    fresh = db.connect(target)
    assert db.count_reviews(fresh, APP, language="german") == 2
    assert fresh.execute("SELECT COUNT(*) FROM runs WHERE appid=? AND status='error'", (APP,)).fetchone()[0] == 1
    fresh.close()


def test_immediate_failure_still_records_a_row(conn, monkeypatch):
    monkeypatch.setattr(steam_api, "iter_reviews", _fake_iter_reviews({"english": ([], "timeout")}))
    assert collect.collect_product(conn, APP, "Gamma", ["english"], FETCHED) == 0
    row = conn.execute("SELECT new_count, max_ts_seen, status, error FROM runs WHERE appid=?", (APP,)).fetchone()
    assert dict(row) == {"new_count": 0, "max_ts_seen": 0, "status": "error", "error": "timeout"}
