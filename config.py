"""Where the database lives, and the environment settings that choose it.

Every entry point (collect.py, app.py, setup.py, export.py, clear_cache.py) used
to carry its own copy of the config.json lookup. They now ask db_target() here,
which also understands a DATABASE_URL switch for the hosted (Turso) store:

  DATABASE_URL unset or empty   -> local SQLite at config.json's db_path (default:
                                   reviews.db next to this file), exactly as before
  DATABASE_URL=sqlite:///path   -> local SQLite at that path
  DATABASE_URL=libsql://host    -> Turso (libSQL) over its HTTP API, authenticated
  DATABASE_URL=https://host        with TURSO_AUTH_TOKEN as a bearer token
  DATABASE_URL=http://127.0.0.1:8080
                                -> a local libSQL server (`turso dev` or sqld), for
                                   testing; no token needed on localhost

db.connect() takes whatever db_target() returns (a file path or an http(s) URL)
and picks the backend from its shape, so an explicit file path always means
SQLite no matter what DATABASE_URL says.

The .env file next to this module is loaded on import (when python-dotenv is
installed), so DATABASE_URL and TURSO_AUTH_TOKEN can live there alongside
ANTHROPIC_API_KEY. Real environment variables win over .env values.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_DB_PATH = Path(__file__).with_name("reviews.db")

# Hosts that count as "a server on this machine": a token is optional there,
# because a local sqld / `turso dev` instance normally runs without auth.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Guarded so the module still imports if python-dotenv isn't installed — the env
# vars can also be set the normal way.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ModuleNotFoundError:
    pass


def db_path() -> str:
    """The local SQLite path: config.json's db_path, or reviews.db next to this file."""
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("db_path", str(DEFAULT_DB_PATH))
    return str(DEFAULT_DB_PATH)


def ensure_config_file() -> None:
    """Write config.json with the default DB path if it doesn't exist yet (setup.py's first run)."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps({"db_path": str(DEFAULT_DB_PATH)}, indent=2), encoding="utf-8")


def database_url() -> str:
    """The raw DATABASE_URL setting, or "" when unset."""
    return os.environ.get("DATABASE_URL", "").strip()


def turso_auth_token() -> str | None:
    """TURSO_AUTH_TOKEN (a database-scoped token), or None when unset."""
    return os.environ.get("TURSO_AUTH_TOKEN", "").strip() or None


def is_remote(target: str | Path) -> bool:
    """True when a db target names a libSQL server rather than a local file."""
    return str(target).lower().startswith(("libsql://", "https://", "http://"))


def remote_http_url(target: str) -> str:
    """Normalize a libSQL server URL to the http(s) base the HTTP API lives under.

    libsql://host -> https://host. A query string is refused rather than dropped:
    some tools put the token in `?authToken=`, but the token belongs in
    TURSO_AUTH_TOKEN so it only ever travels in the Authorization header.
    """
    parts = urlsplit(target)
    scheme = parts.scheme.lower()
    if scheme == "libsql":
        scheme = "https"
    if scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"Not a libSQL server URL: {target!r}")
    if parts.query:
        raise ValueError(
            "DATABASE_URL must not carry a query string (put the token in TURSO_AUTH_TOKEN instead)."
        )
    return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def needs_token(url: str) -> bool:
    """A remote server needs a token unless it's on this machine."""
    return (urlsplit(url).hostname or "").lower() not in _LOCAL_HOSTS


def db_target() -> str:
    """What to hand db.connect(): a SQLite file path, or a libSQL server URL.

    Raises ValueError for a DATABASE_URL it doesn't understand, or for a remote
    Turso URL without TURSO_AUTH_TOKEN (better to fail at startup than on the
    first query).
    """
    url = database_url()
    if not url:
        return db_path()
    if url.lower().startswith("sqlite:///"):
        # SQLAlchemy-style: sqlite:///relative.db or sqlite:////absolute/path.db
        return url[len("sqlite:///"):]
    if is_remote(url):
        base = remote_http_url(url)
        if needs_token(base) and not turso_auth_token():
            raise ValueError("DATABASE_URL points at a remote libSQL server but TURSO_AUTH_TOKEN is not set.")
        return base
    raise ValueError(
        f"Unsupported DATABASE_URL {url!r}: use sqlite:///path, libsql://host or https://host "
        f"(or leave it unset for the local reviews.db)."
    )
