"""A small, deterministic review dataset plus plain-Python answers to check queries against.

Reviews are built in the shape steam_api.iter_reviews yields (the Steam API's
JSON), and written through db.upsert_* — so seeding also exercises the write path
of whichever backend the test runs on.
"""

from __future__ import annotations

import time

import db

DAY = 86400
T0 = 1_700_000_000          # 2023-11-14 22:13 UTC; launch of app 100
FETCHED_1 = T0 + 30 * DAY   # first collection
FETCHED_2 = T0 + 31 * DAY   # a later collection (re-fetched reviews only)

APP_A, APP_B = 100, 200
LANGS_A = ["english", "schinese", "tchinese", "german"]

# Releases of app 100 (launch, a major update, a minor hotfix).
RELEASES_A = [
    ("launch", "Initial release", T0, "", True),
    ("news_1", "Big Update 1.1", T0 + 5 * DAY, "https://example.invalid/1", True),
    ("news_2", "Hotfix 1.1.1", T0 + 10 * DAY, "https://example.invalid/2", None),
]

# Reviews that land inside the moving "last 7 days" window, relative to now. Kept
# 1-3 days old so a test running near a boundary can't flip them.
NOW = int(time.time())


def _review(rid: str, ts: int, *, up: bool, text: str | None, votes: int, wvs="0.5", purchase=True) -> dict:
    return {
        "recommendationid": rid,
        "author": {"steamid": f"7656{rid}", "playtime_forever": 600, "playtime_at_review": 300},
        "voted_up": up,
        "review": text,
        "timestamp_created": ts,
        "timestamp_updated": ts + 60,
        "votes_up": votes,
        "votes_funny": 0,
        "weighted_vote_score": wvs,
        "steam_purchase": purchase,
    }


def _build() -> list[tuple[int, str, dict]]:
    """(appid, language, review) rows."""
    rows = []
    # App 100: 60 reviews every 6 hours over 15 days, languages round-robin.
    for i in range(60):
        lang = LANGS_A[i % len(LANGS_A)]
        text = "" if i % 10 == 0 else f"review {i} in {lang}"
        rows.append((APP_A, lang, _review(
            f"a{i:03d}", T0 + i * 6 * 3600, up=(i % 3 != 0), text=text, votes=(i * 7) % 13,
            wvs="" if i % 11 == 0 else f"0.{i % 9 + 1}",  # Steam sometimes sends "" here
        )))
    # App 100: a few recent English reviews (inside the last 7 days).
    for k in range(1, 4):
        rows.append((APP_A, "english", _review(f"r{k}", NOW - k * DAY, up=(k != 2), text=f"recent {k}", votes=k)))
    # App 200: 10 English reviews, 7 positive.
    for i in range(10):
        rows.append((APP_B, "english", _review(f"b{i:03d}", T0 + i * DAY, up=(i < 7), text=f"beta {i}", votes=i)))
    return rows


REVIEWS = _build()


def seed(conn) -> None:
    """Write products, releases, and every review (fetched at FETCHED_1)."""
    db.upsert_product(conn, APP_A, "Alpha Game", LANGS_A)
    db.upsert_product(conn, APP_B, "Beta Game", ["english"])
    for gid, title, date, url, major in RELEASES_A:
        db.upsert_release(conn, APP_A, gid, title, date, url, is_major=major)
    for appid, lang, r in REVIEWS:
        db.upsert_review(conn, appid, lang, r, FETCHED_1)


# ---- expectations (plain Python over REVIEWS) ------------------------------

def matching(appid: int, language: str | None = None, ts_from: int | None = None,
             ts_to: int | None = None) -> list[tuple[str, dict]]:
    """(language, review) pairs a db query with these filters should see."""
    langs = None
    if language in db.LANGUAGE_BUCKETS:
        langs = set(db.LANGUAGE_BUCKETS[language])
    elif language and language != "all":
        langs = {language}
    out = []
    for a, lang, r in REVIEWS:
        ts = r["timestamp_created"]
        if a != appid or (langs is not None and lang not in langs):
            continue
        if ts_from is not None and ts < ts_from:
            continue
        if ts_to is not None and ts >= ts_to:
            continue
        out.append((lang, r))
    return out


def stats(appid: int, language: str | None = None, ts_from: int | None = None, ts_to: int | None = None) -> dict:
    rows = matching(appid, language, ts_from, ts_to)
    total = len(rows)
    positive = sum(1 for _, r in rows if r["voted_up"])
    return {"total_reviews": total, "positive": positive, "negative": total - positive,
            "percent_positive": round(100 * positive / total, 1) if total else 0.0}
