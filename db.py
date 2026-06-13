"""SQLite storage for collected reviews.

Five tables:
  products - one row per tracked title (appid PK, name, languages)
  reviews  - one row per Steam review, keyed on recommendationid (upsert-safe)
  runs     - one row per (collection run, language) with how many were new
  releases - launch + patch-note posts per title (drives scopes + graph callouts)
  analyses - cached LLM output (themes + summaries) keyed by view

Multiple titles live in one DB; every review carries its appid, and the
downstream program can JOIN reviews ON products to filter/group by title name.
The downstream program can also read `reviews` directly, or join against `runs`
(or filter on fetched_at) to ingest only what changed on a given day.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    appid     INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    languages TEXT NOT NULL,   -- comma-separated review codes
    added_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);

CREATE TABLE IF NOT EXISTS reviews (
    recommendationid   TEXT PRIMARY KEY,
    appid              INTEGER NOT NULL,
    language           TEXT NOT NULL,
    author_steamid     TEXT,
    playtime_forever   INTEGER,
    playtime_at_review INTEGER,
    voted_up           INTEGER,
    review_text        TEXT,
    timestamp_created  INTEGER,
    timestamp_updated  INTEGER,
    votes_up           INTEGER,
    votes_funny        INTEGER,
    weighted_vote_score REAL,
    steam_purchase     INTEGER,
    fetched_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_appid_lang ON reviews(appid, language);
CREATE INDEX IF NOT EXISTS idx_reviews_fetched ON reviews(fetched_at);

CREATE TABLE IF NOT EXISTS runs (
    run_at      INTEGER NOT NULL,
    appid       INTEGER NOT NULL,
    language    TEXT NOT NULL,
    new_count   INTEGER NOT NULL,
    max_ts_seen INTEGER NOT NULL
);

-- Releases/patches sourced from the Steam news feed (plus the store launch
-- date). is_major drives which ones become callouts on the all-time graph.
CREATE TABLE IF NOT EXISTS releases (
    appid    INTEGER NOT NULL,
    gid      TEXT NOT NULL,    -- steam news gid, or 'launch' for the store release
    title    TEXT NOT NULL,
    date     INTEGER NOT NULL, -- unix ts
    url      TEXT,
    is_major INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (appid, gid)
);
CREATE INDEX IF NOT EXISTS idx_releases_appid_date ON releases(appid, date);

-- Cache of LLM-generated analyses so the dashboard doesn't re-call the API on
-- every page load. Keyed by the exact view (title + scope + language filter +
-- kind). review_count lets the app detect staleness when new reviews arrive.
CREATE TABLE IF NOT EXISTS analyses (
    appid           INTEGER NOT NULL,
    scope           TEXT NOT NULL,  -- 'all' or a release gid
    language_filter TEXT NOT NULL,  -- 'all' or a single review code
    kind            TEXT NOT NULL,  -- themes_good | themes_bad | themes_recent | summary_product | summary_qa
    generated_at    INTEGER NOT NULL,
    review_count    INTEGER NOT NULL,
    payload         TEXT NOT NULL,  -- JSON
    PRIMARY KEY (appid, scope, language_filter, kind)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (or create) the DB and ensure the schema exists.

    executescript is safe to call on an existing DB because every statement
    uses CREATE TABLE/INDEX IF NOT EXISTS — it's a no-op when the schema is
    already in place.
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets readers and a writer coexist (the dashboard reads while collect.py
    # or a slow analysis writes); busy_timeout makes a brief lock wait instead of
    # raising "database is locked" immediately.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def upsert_product(conn: sqlite3.Connection, appid: int, name: str, language_codes: list[str]) -> None:
    """Add or update a tracked title. Languages stored as a comma-joined list."""
    conn.execute(
        """
        INSERT INTO products (appid, name, languages, added_at)
        VALUES (?,?,?,?)
        ON CONFLICT(appid) DO UPDATE SET
            name=excluded.name,
            languages=excluded.languages
        """,
        (appid, name, ",".join(language_codes), int(time.time())),
    )


def list_products(conn: sqlite3.Connection) -> list[dict]:
    """Every tracked title with its languages parsed back into a list."""
    rows = conn.execute("SELECT appid, name, languages FROM products ORDER BY name").fetchall()
    return [
        {"appid": r["appid"], "name": r["name"], "languages": r["languages"].split(",") if r["languages"] else []}
        for r in rows
    ]


def last_seen_ts(conn: sqlite3.Connection, appid: int, language: str) -> int:
    """Newest timestamp_created stored for this app+language, or 0 if none."""
    row = conn.execute(
        "SELECT MAX(timestamp_created) AS m FROM reviews WHERE appid = ? AND language = ?",
        (appid, language),
    ).fetchone()
    return int(row["m"]) if row and row["m"] is not None else 0


def upsert_review(conn: sqlite3.Connection, appid: int, language: str, r: dict, fetched_at: int) -> None:
    """Insert a review, or update its mutable fields if it already exists.

    On conflict we update: voted_up, review_text, timestamp_updated, votes_up,
    votes_funny, weighted_vote_score, and fetched_at. We intentionally do NOT
    update appid, language, author_steamid, timestamp_created, or steam_purchase
    — those are immutable facts about the review's origin and don't change.

    weighted_vote_score uses `or 0` because Steam occasionally returns an empty
    string for this field instead of 0.0.
    """
    author = r.get("author", {}) or {}
    conn.execute(
        """
        INSERT INTO reviews (
            recommendationid, appid, language, author_steamid,
            playtime_forever, playtime_at_review, voted_up, review_text,
            timestamp_created, timestamp_updated, votes_up, votes_funny,
            weighted_vote_score, steam_purchase, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(recommendationid) DO UPDATE SET
            voted_up=excluded.voted_up,
            review_text=excluded.review_text,
            timestamp_updated=excluded.timestamp_updated,
            votes_up=excluded.votes_up,
            votes_funny=excluded.votes_funny,
            weighted_vote_score=excluded.weighted_vote_score,
            fetched_at=excluded.fetched_at
        """,
        (
            r.get("recommendationid"),
            appid,
            language,
            author.get("steamid"),
            author.get("playtime_forever"),
            author.get("playtime_at_review"),
            1 if r.get("voted_up") else 0,
            r.get("review"),
            int(r.get("timestamp_created", 0)),
            int(r.get("timestamp_updated", 0)),
            r.get("votes_up"),
            r.get("votes_funny"),
            float(r.get("weighted_vote_score", 0) or 0),
            1 if r.get("steam_purchase") else 0,
            fetched_at,
        ),
    )


def upsert_release(conn: sqlite3.Connection, appid: int, gid: str, title: str, date: int,
                   url: str = "", is_major: bool | None = None) -> None:
    """Add or update a release. is_major=None preserves any existing manual flag."""
    if is_major is None:
        conn.execute(
            """
            INSERT INTO releases (appid, gid, title, date, url, is_major)
            VALUES (?,?,?,?,?,0)
            ON CONFLICT(appid, gid) DO UPDATE SET title=excluded.title, date=excluded.date, url=excluded.url
            """,
            (appid, gid, title, date, url),
        )
    else:
        conn.execute(
            """
            INSERT INTO releases (appid, gid, title, date, url, is_major)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(appid, gid) DO UPDATE SET
                title=excluded.title, date=excluded.date, url=excluded.url, is_major=excluded.is_major
            """,
            (appid, gid, title, date, url, 1 if is_major else 0),
        )


def list_releases(conn: sqlite3.Connection, appid: int, *, major_only: bool = False) -> list[dict]:
    sql = "SELECT gid, title, date, url, is_major FROM releases WHERE appid = ?"
    if major_only:
        sql += " AND is_major = 1"
    sql += " ORDER BY date"
    return [dict(r) for r in conn.execute(sql, (appid,)).fetchall()]


def get_cached_analysis(conn: sqlite3.Connection, appid: int, scope: str, language_filter: str, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT generated_at, review_count, payload FROM analyses "
        "WHERE appid=? AND scope=? AND language_filter=? AND kind=?",
        (appid, scope, language_filter, kind),
    ).fetchone()
    if not row:
        return None
    return {"generated_at": row["generated_at"], "review_count": row["review_count"], "payload": row["payload"]}


def save_analysis(conn: sqlite3.Connection, appid: int, scope: str, language_filter: str, kind: str,
                  review_count: int, payload_json: str) -> None:
    conn.execute(
        """
        INSERT INTO analyses (appid, scope, language_filter, kind, generated_at, review_count, payload)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(appid, scope, language_filter, kind) DO UPDATE SET
            generated_at=excluded.generated_at, review_count=excluded.review_count, payload=excluded.payload
        """,
        (appid, scope, language_filter, kind, int(time.time()), review_count, payload_json),
    )


# Language "buckets" combine related review codes into one filter — e.g. both
# Chinese variants, which individually may be small but together carry a clear
# distinct signal. A bucket key is usable anywhere a single language code is.
LANGUAGE_BUCKETS = {"chinese": ["schinese", "tchinese"]}


def _scope_filter(language: str | None, ts_from: int | None, ts_to: int | None) -> tuple[str, list]:
    """Build a shared WHERE clause for review queries. language=None/'all' = all langs.

    `language` may be a single review code, 'all', or a bucket key (see
    LANGUAGE_BUCKETS), which expands to an IN (...) over its member codes.
    """
    clauses, params = ["appid = ?"], []
    if language in LANGUAGE_BUCKETS:
        codes = LANGUAGE_BUCKETS[language]
        clauses.append("language IN (%s)" % ",".join(["?"] * len(codes)))
        params.extend(codes)
    elif language and language != "all":
        clauses.append("language = ?")
        params.append(language)
    if ts_from is not None:
        clauses.append("timestamp_created >= ?")
        params.append(ts_from)
    if ts_to is not None:
        clauses.append("timestamp_created < ?")
        params.append(ts_to)
    return " AND ".join(clauses), params


def languages_for(conn: sqlite3.Connection, appid: int) -> list[str]:
    """Distinct review-language codes that actually have stored reviews for this app."""
    rows = conn.execute(
        "SELECT DISTINCT language FROM reviews WHERE appid = ? ORDER BY language", (appid,)
    ).fetchall()
    return [r["language"] for r in rows]


def language_breakdown(conn: sqlite3.Connection, appid: int) -> list[tuple[str, int, int]]:
    """Per-language (code, review_count, positive_count) for this app, for eligibility."""
    rows = conn.execute(
        "SELECT language, COUNT(*) c, SUM(voted_up) up FROM reviews WHERE appid = ? GROUP BY language",
        (appid,),
    ).fetchall()
    return [(r["language"], r["c"], r["up"] or 0) for r in rows]


def last_collected(conn: sqlite3.Connection, appid: int) -> int | None:
    """Most recent fetched_at for this app (when reviews were last stored), or None."""
    row = conn.execute(
        "SELECT MAX(fetched_at) AS m FROM reviews WHERE appid = ?", (appid,)
    ).fetchone()
    return int(row["m"]) if row and row["m"] is not None else None


def sentiment_rows(conn: sqlite3.Connection, appid: int, *, language: str | None = None,
                   ts_from: int | None = None, ts_to: int | None = None) -> list[tuple[int, int]]:
    """Return (timestamp_created, voted_up) for every matching review, oldest-first.

    The dashboard buckets these into a % positive time series client-side; keeping
    it raw here means one query serves any bucket size the chart wants.
    """
    where, params = _scope_filter(language, ts_from, ts_to)
    rows = conn.execute(
        f"SELECT timestamp_created, voted_up FROM reviews WHERE {where} ORDER BY timestamp_created",
        (appid, *params),
    ).fetchall()
    return [(r["timestamp_created"], r["voted_up"]) for r in rows]


def sample_reviews(conn: sqlite3.Connection, appid: int, *, voted_up: int, language: str | None = None,
                   ts_from: int | None = None, ts_to: int | None = None,
                   fetched_from: int | None = None, limit: int = 150) -> list[str]:
    """Return up to `limit` review texts for theme extraction, most-helpful first.

    Ordered by votes_up so the LLM sees the reviews other players found useful,
    which yields more representative themes than a purely chronological sample.
    `fetched_from` limits to reviews collected at/after that time (fetched_at) —
    used for incremental refreshes that only feed the model the new reviews.
    """
    where, params = _scope_filter(language, ts_from, ts_to)
    extra = ""
    if fetched_from is not None:
        extra = " AND fetched_at >= ?"
        params = [*params, fetched_from]
    rows = conn.execute(
        f"SELECT review_text FROM reviews WHERE {where}{extra} AND voted_up = ? "
        f"AND review_text IS NOT NULL AND length(trim(review_text)) > 0 "
        f"ORDER BY votes_up DESC, timestamp_created DESC LIMIT ?",
        (appid, *params, voted_up, limit),
    ).fetchall()
    return [r["review_text"] for r in rows]


def count_reviews(conn: sqlite3.Connection, appid: int, *, language: str | None = None,
                  ts_from: int | None = None, ts_to: int | None = None) -> int:
    where, params = _scope_filter(language, ts_from, ts_to)
    row = conn.execute(f"SELECT COUNT(*) AS c FROM reviews WHERE {where}", (appid, *params)).fetchone()
    return int(row["c"])


def log_run(conn: sqlite3.Connection, appid: int, language: str, new_count: int, max_ts: int) -> None:
    """Append one row to the runs audit log after each language is collected.

    max_ts_seen is the most recent timestamp_created seen in this run. The
    downstream program can read this to detect "which run last touched which
    language" without scanning the reviews table.
    """
    conn.execute(
        "INSERT INTO runs (run_at, appid, language, new_count, max_ts_seen) VALUES (?,?,?,?,?)",
        (int(time.time()), appid, language, new_count, max_ts),
    )
