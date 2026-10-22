"""Schema creation and the runs-column migration: idempotent on new and old databases."""

from __future__ import annotations

import sqlite3

import pytest

import config
import db

NEW_TABLES = {"job_runs", "daily_snapshots", "signals", "reports", "llm_usage", "backfill_state"}
CORE_TABLES = {"products", "reviews", "runs", "releases", "analyses"}
RUNS_COLUMNS = ["run_at", "appid", "language", "new_count", "max_ts_seen", "status", "error", "trigger", "pages"]

# The runs table exactly as it shipped before this change.
LEGACY_SCHEMA = """
CREATE TABLE runs (
    run_at      INTEGER NOT NULL,
    appid       INTEGER NOT NULL,
    language    TEXT NOT NULL,
    new_count   INTEGER NOT NULL,
    max_ts_seen INTEGER NOT NULL
);
INSERT INTO runs VALUES (1700000000, 100, 'english', 12, 1699999999);
"""


def _tables(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _indexes(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


def _columns(conn, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"SELECT name FROM pragma_table_info('{table}')").fetchall()]


def _reconnect(target):
    db._SCHEMA_READY.clear()  # force the schema to run again on a remote target too
    return db.connect(target)


def test_fresh_database_has_full_schema(target):
    conn = db.connect(target)
    assert CORE_TABLES | NEW_TABLES <= _tables(conn)
    assert _columns(conn, "runs") == RUNS_COLUMNS
    assert "idx_reviews_appid_lang_ts" in _indexes(conn)
    conn.close()


def test_connect_is_idempotent(target):
    for _ in range(3):
        conn = _reconnect(target)
        db.ensure_schema(conn)  # and again on the same connection
        assert _columns(conn, "runs") == RUNS_COLUMNS
        conn.close()


def test_schema_keeps_data_across_reconnects(conn, target):
    db.log_run(conn, 100, "english", 5, 123)
    conn.commit()
    conn.close()
    conn = _reconnect(target)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert db.list_products(conn)  # seeded data survived
    conn.close()


def test_legacy_runs_table_is_migrated(backend, target):
    """A DB created by the old code gains the columns; its rows read as complete runs."""
    if backend == "sqlite":
        raw = sqlite3.connect(target)
        raw.executescript(LEGACY_SCHEMA)
        raw.close()
    else:
        import db_turso

        raw = db_turso.connect(config.remote_http_url(target), config.turso_auth_token())
        raw.executescript(LEGACY_SCHEMA)
        raw.close()

    conn = _reconnect(target)
    assert _columns(conn, "runs") == RUNS_COLUMNS
    old = dict(conn.execute("SELECT * FROM runs").fetchone())
    assert old["status"] == "complete" and old["error"] is None and old["new_count"] == 12
    conn.close()

    conn = _reconnect(target)  # second pass: nothing left to add, no error
    assert _columns(conn, "runs") == RUNS_COLUMNS
    conn.close()


class _ColumnsLookMissing:
    """Wraps a connection so the column check reports nothing, as if another process
    added the columns between our check and our ALTER (the race _migrate tolerates)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        if "pragma_table_info" in sql:
            return self._conn.execute("SELECT 'x' AS name WHERE 0")
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_migrate_tolerates_duplicate_column_race(target):
    conn = db.connect(target)
    db._migrate(_ColumnsLookMissing(conn))  # every ALTER hits "duplicate column"; swallowed
    assert _columns(conn, "runs") == RUNS_COLUMNS
    conn.close()


def test_wrong_token_is_rejected(backend, target):
    """Against an auth-enabled server, a bad bearer token fails loudly at connect."""
    import conftest

    if backend != "turso" or not conftest.TEST_TURSO_AUTH_TOKEN:
        pytest.skip("needs a libSQL server with auth (TEST_TURSO_AUTH_TOKEN)")
    db._SCHEMA_READY.clear()
    with pytest.raises(sqlite3.OperationalError, match="TURSO_AUTH_TOKEN"):
        db.connect(target, auth_token="not-a-valid-token")


def test_log_run_status_and_error(conn):
    db.log_run(conn, 100, "english", 3, 111)
    db.log_run(conn, 100, "german", 1, 222, status="error", error="HTTP 503")
    conn.commit()
    rows = [dict(r) for r in conn.execute(
        "SELECT language, new_count, max_ts_seen, status, error FROM runs ORDER BY language").fetchall()]
    assert rows == [
        {"language": "english", "new_count": 3, "max_ts_seen": 111, "status": "complete", "error": None},
        {"language": "german", "new_count": 1, "max_ts_seen": 222, "status": "error", "error": "HTTP 503"},
    ]
