"""SQLite storage for collected reviews.

Core tables:
  products - one row per tracked title (appid PK, name, languages)
  reviews  - one row per Steam review, keyed on recommendationid (upsert-safe)
  runs     - one row per (collection run, language) with how many were new, and
             whether it finished (status 'complete') or failed ('error' + error)
  releases - launch + patch-note posts per title (drives scopes + graph callouts)
  analyses - cached LLM output (themes + summaries) keyed by view

Daily-job tables (docs/AGENT_PLAN.md, Section 4.6), created here so the local DB
and the cloud DB share one schema: job_runs, daily_snapshots, signals, reports,
llm_usage, backfill_state.

Multiple titles live in one DB; every review carries its appid, and the
downstream program can JOIN reviews ON products to filter/group by title name.
The downstream program can also read `reviews` directly, or join against `runs`
(or filter on fetched_at) to ingest only what changed on a given day.

Backends: by default this is a local SQLite file. When DATABASE_URL points at a
Turso / libSQL server (see config.py), connect() returns a db_turso connection
instead; it speaks the same SQL, so every function below works on either.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import config

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
-- Serves last_seen_ts and every time-window query (and cuts rows read, which
-- Turso bills). The first connect after upgrading builds it once.
CREATE INDEX IF NOT EXISTS idx_reviews_appid_lang_ts ON reviews(appid, language, timestamp_created);

-- status / error / trigger / pages are added by _migrate (guarded ALTER TABLE),
-- so new and pre-existing databases end up with the same columns.
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

-- ---- Daily job (docs/AGENT_PLAN.md, Sections 4.6, 6 and 7) ----------------
-- Dates are TEXT 'YYYY-MM-DD', JSON is TEXT, timestamps are unix seconds.

-- One row per report date: the idempotency key for the daily run.
CREATE TABLE IF NOT EXISTS job_runs (
    report_date  TEXT PRIMARY KEY,      -- local date in REPORT_TZ
    status       TEXT NOT NULL,         -- running | collected | reported | emailed | failed
    attempt      INTEGER NOT NULL DEFAULT 1,
    started_at   INTEGER,
    heartbeat_at INTEGER,
    finished_at  INTEGER,
    session_id   TEXT,                  -- Managed Agents session, for tracing
    build_source TEXT,                  -- pics_ws | steamcmd_net | github_job | none
    build_status TEXT,                  -- ok | partial | unavailable
    summary      TEXT,                  -- JSON
    error        TEXT,
    email_id     TEXT,                  -- one result per delivery channel
    slack_ts     TEXT
);

-- Per title / language / day, so "since yesterday" is a cheap lookup.
CREATE TABLE IF NOT EXISTS daily_snapshots (
    report_date  TEXT NOT NULL,
    appid        INTEGER NOT NULL,
    language     TEXT NOT NULL,
    total        INTEGER,
    positive     INTEGER,
    new_24h      INTEGER,
    positive_24h INTEGER,
    pct_7d       REAL,
    n_7d         INTEGER,
    pct_28d      REAL,
    n_28d        INTEGER,
    PRIMARY KEY (report_date, appid, language)
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE,            -- detector|appid|scope|language|week
    report_date TEXT,
    appid       INTEGER,
    detector    TEXT,
    language    TEXT,
    severity    TEXT,
    metrics     TEXT,                   -- JSON
    first_seen  TEXT,
    last_seen   TEXT,
    status      TEXT                    -- open | resolved
);

CREATE TABLE IF NOT EXISTS reports (
    report_date TEXT PRIMARY KEY,
    kind        TEXT,                   -- full | quiet | failure
    subject     TEXT,
    html        TEXT,
    text_body   TEXT,
    narrative   TEXT,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER,
    report_date       TEXT,
    purpose           TEXT,             -- narrative | backfill | backfill_lazy | dashboard | theme_tagging
    model             TEXT,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cache_read_tokens INTEGER,
    cost_usd          REAL
);

CREATE TABLE IF NOT EXISTS backfill_state (
    appid            INTEGER PRIMARY KEY,
    status           TEXT NOT NULL,     -- none | collecting | analyzing | baselined | insufficient
    collected_at     INTEGER,
    priority_done_at INTEGER,
    spent_usd        REAL DEFAULT 0,
    calls            INTEGER DEFAULT 0,
    last_scope       TEXT,
    batch_ids        TEXT,              -- JSON
    updated_at       INTEGER
);
"""

# Columns added to existing tables after they first shipped: (table, column, DDL type).
# _migrate adds whichever are missing, so it is safe on a new DB, an old DB, and
# on every later connect. Rows written before the upgrade get the DEFAULT — and
# every pre-existing `runs` row is a completed run (the old code wrote no row on
# failure), so 'complete' is the accurate backfill for them.
_ADDED_COLUMNS = [
    ("runs", "status", "TEXT NOT NULL DEFAULT 'complete'"),  # complete | error
    ("runs", "error", "TEXT"),                               # the error text when status='error'
    ("runs", "trigger", "TEXT"),                             # what started the run (e.g. daily | backfill | manual)
    ("runs", "pages", "INTEGER"),                            # review pages fetched, when known
]

# Remote databases whose schema this process has already ensured. The dashboard
# opens a connection per request; over HTTP, re-running the schema each time
# would add round trips for nothing. Local SQLite still runs it on every connect
# (cheap, and unchanged from before).
_SCHEMA_READY: set[str] = set()


def _migrate(conn) -> None:
    """Add any missing columns from _ADDED_COLUMNS (guarded ALTER TABLE ... ADD COLUMN)."""
    for table, column, decl in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"SELECT name FROM pragma_table_info('{table}')").fetchall()}
        if column in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            conn.commit()
        except sqlite3.OperationalError as exc:
            # Another process may have added it between our check and the ALTER.
            if "duplicate column" not in str(exc).lower():
                raise


def ensure_schema(conn) -> None:
    """Create any missing tables/indexes and apply column migrations. Idempotent.

    executescript is safe to call on an existing DB because every statement
    uses CREATE TABLE/INDEX IF NOT EXISTS — it's a no-op when the schema is
    already in place; _migrate only adds columns that aren't there yet.
    """
    conn.executescript(SCHEMA)
    _migrate(conn)


def connect(db_path: str | Path | None = None, *, auth_token: str | None = None):
    """Open (or create) the DB and ensure the schema exists.

    db_path is what config.db_target() returns: a SQLite file path (the default
    when DATABASE_URL is unset, so local use is unchanged), or a libSQL server URL,
    which returns a db_turso.TursoConnection with the same execute/fetch/commit
    surface. Omit it to use config.db_target(). An explicit file path always
    opens SQLite, whatever DATABASE_URL says.
    """
    target = config.db_target() if db_path is None else str(db_path)
    if config.is_remote(target):
        import db_turso  # imported here so the SQLite path never needs it

        url = config.remote_http_url(target)
        conn = db_turso.connect(url, auth_token if auth_token is not None else config.turso_auth_token())
        # No PRAGMAs here: journal mode and locking are the server's business.
        if url not in _SCHEMA_READY:
            try:
                ensure_schema(conn)
            except Exception:
                conn.close()
                raise
            _SCHEMA_READY.add(url)
        return conn

    conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets readers and a writer coexist (the dashboard reads while collect.py
    # or a slow analysis writes); busy_timeout makes a brief lock wait instead of
    # raising "database is locked" immediately.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_schema(conn)
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


def language_breakdown(conn: sqlite3.Connection, appid: int, *,
                       ts_from: int | None = None, ts_to: int | None = None) -> list[tuple[str, int, int]]:
    """Per-language (code, review_count, positive_count) for this app, for eligibility.

    With ts_from/ts_to the counts are restricted to a scope window (a release
    period), so the dashboard's language breakdown can reflect the selected scope
    rather than the whole lifetime. Omit both for the all-time breakdown.
    """
    where, params = _scope_filter(None, ts_from, ts_to)
    rows = conn.execute(
        f"SELECT language, COUNT(*) c, SUM(voted_up) up FROM reviews WHERE {where} GROUP BY language",
        (appid, *params),
    ).fetchall()
    return [(r["language"], r["c"], r["up"] or 0) for r in rows]


def cumulative_totals(conn: sqlite3.Connection, appid: int, *, language: str | None = None,
                      before: int) -> tuple[int, int]:
    """(review_count, positive_count) for reviews strictly before `before` (a unix ts).

    Used to seed the sentiment chart's cumulative line so it reflects lifetime history
    up to the start of a scope window, not just the reviews inside the window. Honors
    the same language filter/bucketing as the rest of the chart.
    """
    where, params = _scope_filter(language, None, before)
    row = conn.execute(
        f"SELECT COUNT(*) c, SUM(voted_up) up FROM reviews WHERE {where}",
        (appid, *params),
    ).fetchone()
    return (row["c"] or 0, row["up"] or 0)


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


def daily_sentiment(conn: sqlite3.Connection, appid: int, *, language: str | None = None,
                    ts_from: int | None = None, ts_to: int | None = None) -> list[tuple[int, int, int]]:
    """Per-UTC-day (day_start_ts, review_count, positive_count) rows, oldest-first.

    The dashboard chart re-buckets these daily rows into day/week/month *in the browser*
    as the user zooms, so the per-period line's resolution can change without re-querying.
    Day is the finest bucket reviews realistically support. `day_start_ts` is the UTC
    midnight (unix seconds) the review's timestamp falls in.
    """
    where, params = _scope_filter(language, ts_from, ts_to)
    rows = conn.execute(
        f"SELECT (timestamp_created / 86400) * 86400 AS day, COUNT(*) c, SUM(voted_up) up "
        f"FROM reviews WHERE {where} GROUP BY day ORDER BY day",
        (appid, *params),
    ).fetchall()
    return [(int(r["day"]), r["c"], int(r["up"] or 0)) for r in rows]


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


def review_stats(conn: sqlite3.Connection, appid: int, *, language: str | None = None,
                 ts_from: int | None = None, ts_to: int | None = None) -> dict:
    """Headline counts for one view: total, positive, negative and % positive.

    One aggregate query, so only a single row comes back however many reviews
    match — this replaced analyze._basic_stats, which loaded every row just to
    count them (fine on a local file, a full-table download over HTTP).
    """
    where, params = _scope_filter(language, ts_from, ts_to)
    row = conn.execute(
        f"SELECT COUNT(*) AS c, SUM(voted_up) AS up FROM reviews WHERE {where}",
        (appid, *params),
    ).fetchone()
    total = int(row["c"] or 0)
    positive = int(row["up"] or 0)
    pct = round(100 * positive / total, 1) if total else 0.0
    return {"total_reviews": total, "positive": positive, "negative": total - positive, "percent_positive": pct}


def log_run(conn: sqlite3.Connection, appid: int, language: str, new_count: int, max_ts: int, *,
            status: str = "complete", error: str | None = None) -> None:
    """Append one row to the runs audit log after each language is collected.

    max_ts_seen is the most recent timestamp_created seen in this run. The
    downstream program can read this to detect "which run last touched which
    language" without scanning the reviews table.

    A failed language gets a row too, with status='error' and the error text, so
    feed health can report it and a later backfill knows that language's history
    may be incomplete. new_count is then the rows stored before the failure.
    """
    conn.execute(
        "INSERT INTO runs (run_at, appid, language, new_count, max_ts_seen, status, error) VALUES (?,?,?,?,?,?,?)",
        (int(time.time()), appid, language, new_count, max_ts, status, error),
    )
