"""Shared pytest fixtures: a synthetic database on SQLite, and optionally on libSQL.

Every test that takes `conn` / `target` runs once per backend:

  sqlite  always; a fresh file under pytest's tmp_path (never the real reviews.db)
  turso   only when TEST_TURSO_URL is set, e.g. a local server:
              sqld --http-listen-addr 127.0.0.1:8080          (or `turso dev`)
              TEST_TURSO_URL=http://127.0.0.1:8080 pytest
          or a throwaway Turso database (`turso db create steam-test`):
              TEST_TURSO_URL=libsql://steam-test-<org>.turso.io \
              TEST_TURSO_AUTH_TOKEN=... pytest

The turso backend DROPS EVERY TABLE in that database before each test. Point it
only at a database you can throw away; it refuses to run against DATABASE_URL.

An autouse fixture clears DATABASE_URL / TURSO_AUTH_TOKEN and points config.py at
a temp config.json, so no test can reach a real database by accident.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402  (after the sys.path tweak)
import db  # noqa: E402

import synthetic  # noqa: E402

# Read once, before the autouse fixture scrubs the environment.
TEST_TURSO_URL = os.environ.get("TEST_TURSO_URL", "").strip()
TEST_TURSO_AUTH_TOKEN = os.environ.get("TEST_TURSO_AUTH_TOKEN", "").strip() or None
_REAL_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No test sees the developer's DATABASE_URL, token, config.json or reviews.db."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", tmp_path / "default-reviews.db")
    yield


def _wipe_remote(url: str, token: str | None) -> None:
    """Drop every user table on the test server so each test starts empty."""
    import db_turso

    conn = db_turso.connect(config.remote_http_url(url), token)
    try:
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' AND name NOT LIKE 'libsql\\_%' ESCAPE '\\'"
        ).fetchall()]
        if names:
            conn.executescript("".join(f'DROP TABLE IF EXISTS "{n}";' for n in names))
    finally:
        conn.close()
    db._SCHEMA_READY.clear()


@pytest.fixture(params=["sqlite", "turso"])
def backend(request):
    if request.param == "turso":
        if not TEST_TURSO_URL:
            pytest.skip("TEST_TURSO_URL not set (no libSQL server to test against)")
        if _REAL_DATABASE_URL and TEST_TURSO_URL.rstrip("/") == _REAL_DATABASE_URL.rstrip("/"):
            pytest.skip("TEST_TURSO_URL equals DATABASE_URL; refusing to wipe a real database")
    return request.param


@pytest.fixture
def target(backend, tmp_path, monkeypatch):
    """An empty database for this backend: a file path or a server URL for db.connect().

    For turso the token goes in TURSO_AUTH_TOKEN, the way the app gets it.
    """
    if backend == "sqlite":
        return str(tmp_path / "reviews.db")
    if TEST_TURSO_AUTH_TOKEN:
        monkeypatch.setenv("TURSO_AUTH_TOKEN", TEST_TURSO_AUTH_TOKEN)
    _wipe_remote(TEST_TURSO_URL, TEST_TURSO_AUTH_TOKEN)
    return TEST_TURSO_URL


@pytest.fixture
def database_url(backend, target):
    """target in DATABASE_URL form, for tests that go through config.db_target()."""
    return f"sqlite:///{target}" if backend == "sqlite" else target


@pytest.fixture
def conn(target):
    """A connection to a database seeded with the synthetic dataset (committed)."""
    c = db.connect(target)
    synthetic.seed(c)
    c.commit()
    yield c
    c.close()
