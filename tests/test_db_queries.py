"""db.py query functions against the synthetic dataset, on every backend."""

from __future__ import annotations

from collections import Counter

import db
import synthetic as s

WINDOW = (s.T0 + 5 * s.DAY, s.T0 + 10 * s.DAY)  # the "Big Update 1.1" release window


def test_list_products(conn):
    assert db.list_products(conn) == [
        {"appid": s.APP_A, "name": "Alpha Game", "languages": s.LANGS_A},
        {"appid": s.APP_B, "name": "Beta Game", "languages": ["english"]},
    ]


def test_upsert_product_updates_name_and_languages(conn):
    db.upsert_product(conn, s.APP_B, "Beta Game GOTY", ["english", "french"])
    conn.commit()
    beta = [p for p in db.list_products(conn) if p["appid"] == s.APP_B][0]
    assert beta == {"appid": s.APP_B, "name": "Beta Game GOTY", "languages": ["english", "french"]}


def test_last_seen_ts(conn):
    for lang in s.LANGS_A:
        expected = max(r["timestamp_created"] for _, r in s.matching(s.APP_A, lang))
        assert db.last_seen_ts(conn, s.APP_A, lang) == expected
    assert db.last_seen_ts(conn, s.APP_A, "french") == 0
    assert db.last_seen_ts(conn, 999, "english") == 0


def test_languages_for(conn):
    assert db.languages_for(conn, s.APP_A) == sorted(s.LANGS_A)
    assert db.languages_for(conn, s.APP_B) == ["english"]
    assert db.languages_for(conn, 999) == []


def test_language_breakdown_all_time_and_window(conn):
    for ts_from, ts_to in [(None, None), WINDOW]:
        got = sorted(db.language_breakdown(conn, s.APP_A, ts_from=ts_from, ts_to=ts_to))
        rows = s.matching(s.APP_A, None, ts_from, ts_to)
        counts = Counter(lang for lang, _ in rows)
        ups = Counter(lang for lang, r in rows if r["voted_up"])
        assert got == sorted((lang, counts[lang], ups[lang]) for lang in counts)
        assert all(isinstance(c, int) and isinstance(u, int) for _, c, u in got)


def test_cumulative_totals(conn):
    before = s.T0 + 7 * s.DAY
    for lang in [None, "all", "english", "chinese"]:
        exp = s.stats(s.APP_A, lang, None, before)
        assert db.cumulative_totals(conn, s.APP_A, language=lang, before=before) == (
            exp["total_reviews"], exp["positive"])
    assert db.cumulative_totals(conn, s.APP_A, before=s.T0) == (0, 0)


def test_last_collected(conn):
    assert db.last_collected(conn, s.APP_A) == s.FETCHED_1
    assert db.last_collected(conn, 999) is None


def test_sentiment_rows_oldest_first(conn):
    got = db.sentiment_rows(conn, s.APP_A, language="chinese", ts_from=WINDOW[0], ts_to=WINDOW[1])
    exp = sorted((r["timestamp_created"], 1 if r["voted_up"] else 0)
                 for _, r in s.matching(s.APP_A, "chinese", *WINDOW))
    assert got == exp and got


def test_daily_sentiment_buckets_by_utc_day(conn):
    got = db.daily_sentiment(conn, s.APP_A)
    days: dict[int, list[int]] = {}
    for _, r in s.matching(s.APP_A):
        d = (r["timestamp_created"] // 86400) * 86400
        days.setdefault(d, [0, 0])
        days[d][0] += 1
        days[d][1] += 1 if r["voted_up"] else 0
    assert got == [(d, c, up) for d, (c, up) in sorted(days.items())]


def test_sample_reviews_order_filters_and_limit(conn):
    got = db.sample_reviews(conn, s.APP_A, voted_up=1, language="english", limit=5)
    cands = [r for _, r in s.matching(s.APP_A, "english") if r["voted_up"] and (r["review"] or "").strip()]
    cands.sort(key=lambda r: (-r["votes_up"], -r["timestamp_created"]))
    assert got == [r["review"] for r in cands[:5]]
    # Empty review text is never sampled.
    assert "" not in db.sample_reviews(conn, s.APP_A, voted_up=0, limit=500)


def test_sample_reviews_fetched_from(conn):
    # Re-fetch two reviews in a later run; only they are "new since FETCHED_2".
    for appid, lang, r in s.REVIEWS[:2]:
        db.upsert_review(conn, appid, lang, r, s.FETCHED_2)
    conn.commit()
    got = set(db.sample_reviews(conn, s.APP_A, voted_up=1, fetched_from=s.FETCHED_2, limit=100))
    exp = {r["review"] for _, _, r in s.REVIEWS[:2] if r["voted_up"] and r["review"]}
    assert got == exp


def test_count_reviews_with_bucket(conn):
    assert db.count_reviews(conn, s.APP_A) == len(s.matching(s.APP_A))
    assert db.count_reviews(conn, s.APP_A, language="chinese") == len(s.matching(s.APP_A, "chinese"))
    assert db.count_reviews(conn, s.APP_A, language="schinese", ts_from=WINDOW[0]) == len(
        s.matching(s.APP_A, "schinese", WINDOW[0]))


def test_upsert_review_updates_only_mutable_fields(conn):
    appid, lang, r = s.REVIEWS[5]
    changed = dict(r, voted_up=not r["voted_up"], review="edited", votes_up=99,
                   timestamp_created=1, steam_purchase=not r["steam_purchase"], weighted_vote_score="")
    db.upsert_review(conn, appid, "german", changed, s.FETCHED_2)
    conn.commit()
    row = conn.execute("SELECT * FROM reviews WHERE recommendationid=?", (r["recommendationid"],)).fetchone()
    assert row["voted_up"] == (0 if r["voted_up"] else 1)
    assert row["review_text"] == "edited" and row["votes_up"] == 99 and row["fetched_at"] == s.FETCHED_2
    assert row["weighted_vote_score"] == 0.0
    # Immutable origin facts are kept.
    assert row["language"] == lang and row["timestamp_created"] == r["timestamp_created"]
    assert row["steam_purchase"] == 1


def test_upsert_release_preserves_manual_major_flag(conn):
    assert [r["gid"] for r in db.list_releases(conn, s.APP_A)] == ["launch", "news_1", "news_2"]
    assert [r["gid"] for r in db.list_releases(conn, s.APP_A, major_only=True)] == ["launch", "news_1"]
    # A manual edit, then a re-run that passes is_major=None, keeps the edit.
    conn.execute("UPDATE releases SET is_major=1 WHERE appid=? AND gid='news_2'", (s.APP_A,))
    db.upsert_release(conn, s.APP_A, "news_2", "Hotfix 1.1.1 (renamed)", s.T0 + 10 * s.DAY)
    conn.commit()
    rel = {r["gid"]: r for r in db.list_releases(conn, s.APP_A)}["news_2"]
    assert rel == {"gid": "news_2", "title": "Hotfix 1.1.1 (renamed)", "date": s.T0 + 10 * s.DAY,
                   "url": "", "is_major": 1}


def test_analysis_cache_roundtrip(conn):
    assert db.get_cached_analysis(conn, s.APP_A, "all", "all", "summary_qa") is None
    db.save_analysis(conn, s.APP_A, "all", "all", "summary_qa", 42, '{"text": "v1"}')
    conn.commit()
    hit = db.get_cached_analysis(conn, s.APP_A, "all", "all", "summary_qa")
    assert hit["review_count"] == 42 and hit["payload"] == '{"text": "v1"}'
    assert isinstance(hit["generated_at"], int)
    db.save_analysis(conn, s.APP_A, "all", "all", "summary_qa", 50, '{"text": "v2 é中"}')
    conn.commit()
    hit = db.get_cached_analysis(conn, s.APP_A, "all", "all", "summary_qa")
    assert hit["review_count"] == 50 and hit["payload"] == '{"text": "v2 é中"}'


def test_rows_behave_like_sqlite_row(conn):
    row = conn.execute("SELECT appid, name AS Name FROM products ORDER BY appid").fetchone()
    assert row[0] == s.APP_A and row["appid"] == s.APP_A
    assert row["name"] == row["NAME"] == "Alpha Game"   # name lookup is case-insensitive
    assert dict(row) == {"appid": s.APP_A, "Name": "Alpha Game"}
    assert list(row.keys()) == ["appid", "Name"] and len(row) == 2
    assert tuple(row) == (s.APP_A, "Alpha Game")


def test_value_types_round_trip(conn):
    row = conn.execute(
        "SELECT COUNT(*) AS c, MAX(weighted_vote_score) AS w, NULL AS n, ? AS t, ? AS b, ? AS big, ? AS f "
        "FROM reviews",
        ("text é", b"\x00\xffbytes", 2**62, 1.25),
    ).fetchone()
    assert isinstance(row["c"], int) and row["c"] > 0
    assert isinstance(row["w"], float)
    assert row["n"] is None and row["t"] == "text é" and row["b"] == b"\x00\xffbytes"
    assert row["big"] == 2**62 and row["f"] == 1.25


def test_read_sees_earlier_uncommitted_write(conn):
    """A read must see writes made earlier on the same connection (Turso flushes first)."""
    db.upsert_product(conn, 300, "Gamma", ["english"])
    assert [p["appid"] for p in db.list_products(conn)] == [s.APP_A, s.APP_B, 300]


def test_executemany_and_rowcount(conn):
    cur = conn.executemany("DELETE FROM releases WHERE appid=? AND gid=?",
                           [(s.APP_A, "news_1"), (s.APP_A, "news_2"), (s.APP_A, "missing")])
    conn.commit()
    assert cur.rowcount == 2
    assert [r["gid"] for r in db.list_releases(conn, s.APP_A)] == ["launch"]
