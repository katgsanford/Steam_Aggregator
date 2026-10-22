"""A small sqlite3-style connection over Turso's (libSQL) HTTP API.

db.connect() returns a TursoConnection instead of a sqlite3.Connection when
DATABASE_URL points at a libSQL server (see config.py). It copies only the part
of sqlite3 this project uses, so db.py's query functions and the raw SQL in
collect.py, export.py and clear_cache.py run unchanged:

  conn.execute / executemany / executescript / commit / rollback / close / cursor()
  cursor.fetchone / fetchall / fetchmany / iteration / rowcount / lastrowid / description
  rows that support r["col"] (case-insensitive, like sqlite3.Row), r[0] and dict(r)

Wire protocol: every call is one `POST <base>/v2/pipeline` (Hrana over HTTP) with
`Authorization: Bearer <TURSO_AUTH_TOKEN>`. Plain `requests`, not a native client,
so nothing checks the token's format locally — in the cloud sandbox the token is a
vault placeholder that is only swapped for the real one at egress. No baton is
kept: each pipeline is its own short-lived stream, closed in the same request.

Values travel typed. Integers come back as JSON *strings* ({"type": "integer",
"value": "42"}) so 64-bit values survive JavaScript-style parsers; the adapter
turns them back into ints, and blobs (unpadded base64) back into bytes.

Transactions. Data-changing statements (INSERT / UPDATE / DELETE / REPLACE) are
buffered locally, and commit() sends the whole buffer as ONE batch wrapped in
BEGIN ... COMMIT, each step conditioned on the previous one succeeding, with a
ROLLBACK step if the COMMIT doesn't happen — so a flush is all or nothing.
Everything else (SELECT, PRAGMA, DDL, a write with RETURNING) first flushes the
buffer, then runs at once; that's what lets a read see the writes before it. Two
differences from sqlite3 follow, neither of which this codebase depends on:

  - A read commits any writes buffered before it (sqlite3 would keep them in the
    open transaction until commit()).
  - A constraint violation in a buffered write surfaces at commit() (or at the
    next read), not at the execute() that queued it, and it rolls back the whole
    buffer. cursor.rowcount / lastrowid of a buffered write are filled in at flush.

As with sqlite3, close() without commit() discards buffered writes. Keep commits
reasonably small (collect.py already commits every 500 reviews): one commit is one
HTTP request carrying every buffered statement.

Errors are raised as subclasses of sqlite3's own exceptions (IntegrityError for
constraint failures, OperationalError for everything else, including network and
auth failures), so `except sqlite3.OperationalError` works on both backends.
"""

from __future__ import annotations

import base64
import sqlite3
import time
from typing import Any, Iterable, Iterator, Sequence

import requests

DEFAULT_TIMEOUT = 60  # seconds per HTTP request; a 500-row commit is one request

# Read-only pipelines are retried on a dropped connection or a 5xx/429, with these
# pauses (seconds). Writes are never retried: after a lost response we can't tell
# whether the batch committed, and re-sending would duplicate non-idempotent rows.
_READ_RETRY_DELAYS = (0.5, 2.0)

# First keyword of a statement that is buffered until commit(). Anything else is
# run immediately after flushing the buffer.
_BUFFERED_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE"}


class TursoError(sqlite3.OperationalError):
    """A libSQL server or transport error. `code` is the server's error code, if any."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class TursoIntegrityError(sqlite3.IntegrityError):
    """A constraint violation reported by the server (SQLITE_CONSTRAINT*)."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def _raise_server_error(err: dict, sql: str | None = None) -> None:
    code = err.get("code") or None
    msg = err.get("message") or "libSQL error"
    if sql:
        msg = f"{msg} [SQL: {sql.strip()[:200]}]"
    if code and code.startswith("SQLITE_CONSTRAINT"):
        raise TursoIntegrityError(msg, code)
    raise TursoError(msg, code)


# ---------------------------------------------------------------------------
# Value encoding
# ---------------------------------------------------------------------------

def encode_value(v: Any) -> dict:
    """Python value -> Hrana typed value. Same types sqlite3 accepts."""
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):  # before int: bool is an int subclass; sqlite3 stores 1/0
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, str):
        return {"type": "text", "value": v}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode("ascii")}
    raise sqlite3.ProgrammingError(f"Error binding parameter: type {type(v).__name__!r} is not supported")


def decode_value(v: dict) -> Any:
    """Hrana typed value -> Python value (integers arrive as strings)."""
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "text":
        return v["value"]
    if t == "blob":
        b64 = v.get("base64", "")
        return base64.b64decode(b64 + "=" * (-len(b64) % 4))  # the server omits padding
    raise TursoError(f"Unknown value type from server: {t!r}")


def _stmt(sql: str, params: Any = None) -> dict:
    """Build a Hrana statement. Positional params -> args; a mapping -> named_args."""
    stmt: dict = {"sql": sql}
    if params is None:
        stmt["args"] = []
    elif isinstance(params, dict):
        stmt["named_args"] = [{"name": k, "value": encode_value(v)} for k, v in params.items()]
    else:
        stmt["args"] = [encode_value(v) for v in params]
    return stmt


def _verb(sql: str) -> str:
    """First keyword of a statement, upper-cased, skipping leading comments."""
    s = sql.lstrip()
    while s.startswith(("--", "/*")):
        if s.startswith("--"):
            nl = s.find("\n")
            s = "" if nl < 0 else s[nl + 1:].lstrip()
        else:
            end = s.find("*/")
            s = "" if end < 0 else s[end + 2:].lstrip()
    word = s.split(None, 1)[0] if s else ""
    return word.rstrip(";").upper()


# ---------------------------------------------------------------------------
# Rows and cursors
# ---------------------------------------------------------------------------

class Row:
    """One result row with the sqlite3.Row surface: r[0], r["col"], dict(r), keys()."""

    __slots__ = ("_names", "_index", "_values")

    def __init__(self, names: list[str], index: dict[str, int], values: tuple):
        self._names = names
        self._index = index
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return self._values[self._index[key.lower()]]
            except KeyError:
                raise IndexError("No item with that key") from None
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._names)

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator:
        return iter(self._values)

    def __eq__(self, other) -> bool:
        if isinstance(other, Row):
            return self._names == other._names and self._values == other._values
        return NotImplemented

    def __hash__(self) -> int:
        return hash((tuple(self._names), self._values))

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._names, self._values))!r})"


class Cursor:
    """Result holder for one statement, filled immediately (reads) or at flush (buffered writes)."""

    arraysize = 1

    def __init__(self, conn: "TursoConnection"):
        self.connection = conn
        self.rowcount = -1
        self.lastrowid: int | None = None
        self.description: tuple | None = None
        self._rows: list[Row] = []
        self._pos = 0

    def _set_result(self, result: dict, *, is_write: bool) -> None:
        names = [c.get("name") or "" for c in result.get("cols", [])]
        index: dict[str, int] = {}
        for i, n in enumerate(names):
            index.setdefault(n.lower(), i)  # duplicate names: first wins, as in sqlite3.Row
        self._rows = [Row(names, index, tuple(decode_value(v) for v in row)) for row in result.get("rows", [])]
        self._pos = 0
        self.description = tuple((n, None, None, None, None, None, None) for n in names) or None
        if is_write:
            self.rowcount = (self.rowcount if self.rowcount > 0 else 0) + int(result.get("affected_row_count") or 0)
        rid = result.get("last_insert_rowid")
        if rid is not None:
            self.lastrowid = int(rid)

    # sqlite3 lets you call execute on a cursor too.
    def execute(self, sql: str, params: Any = None) -> "Cursor":
        return self.connection._execute(sql, params, self)

    def executemany(self, sql: str, seq_of_params: Iterable) -> "Cursor":
        return self.connection._executemany(sql, seq_of_params, self)

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: int | None = None) -> list:
        n = self.arraysize if size is None else size
        out = self._rows[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def fetchall(self) -> list:
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def __iter__(self) -> Iterator:
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self) -> None:
        self._rows = []
        self._pos = 0


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

class TursoConnection:
    """The sqlite3.Connection subset db.py and its callers use, over HTTP."""

    def __init__(self, base_url: str, auth_token: str | None = None, *,
                 timeout: float = DEFAULT_TIMEOUT, session: Any = None):
        self.url = base_url.rstrip("/") + "/v2/pipeline"
        self.timeout = timeout
        self._session = session or requests.Session()
        self._headers = {"Content-Type": "application/json"}
        if auth_token:
            self._headers["Authorization"] = f"Bearer {auth_token}"
        self._pending: list[tuple[dict, Cursor]] = []  # buffered writes and the cursors awaiting their results
        self._closed = False

    # -- transport -----------------------------------------------------------

    def _pipeline(self, requests_: list[dict], *, retry: bool) -> list[dict]:
        """POST one pipeline (plus a trailing close) and return its per-request results."""
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        body = {"baton": None, "requests": [*requests_, {"type": "close"}]}
        delays = _READ_RETRY_DELAYS if retry else ()
        attempt = 0
        while True:
            try:
                resp = self._session.post(self.url, json=body, headers=self._headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                    attempt += 1
                    continue
                raise TursoError(f"libSQL request failed: {exc}") from exc
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < len(delays):
                time.sleep(delays[attempt])
                attempt += 1
                continue
            break
        if resp.status_code in (401, 403):
            raise TursoError(f"libSQL server rejected the credentials (HTTP {resp.status_code}). "
                             f"Check TURSO_AUTH_TOKEN.")
        if resp.status_code != 200:
            raise TursoError(f"libSQL server returned HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            results = resp.json()["results"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TursoError(f"Unexpected libSQL response: {resp.text[:300]}") from exc
        return results[:len(requests_)]  # drop the close's result

    # -- buffering -----------------------------------------------------------

    def _flush(self) -> None:
        """Send buffered writes as one all-or-nothing batch (BEGIN ... COMMIT)."""
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        steps: list[dict] = [{"stmt": {"sql": "BEGIN"}}]
        for stmt, _ in pending:
            steps.append({"stmt": stmt, "condition": {"type": "ok", "step": len(steps) - 1}})
        commit_step = len(steps)
        steps.append({"stmt": {"sql": "COMMIT"}, "condition": {"type": "ok", "step": commit_step - 1}})
        steps.append({"stmt": {"sql": "ROLLBACK"},
                      "condition": {"type": "not", "cond": {"type": "ok", "step": commit_step}}})

        (res,) = self._pipeline([{"type": "batch", "batch": {"steps": steps}}], retry=False)
        if res.get("type") == "error":
            _raise_server_error(res.get("error", {}))
        out = res["response"]["result"]
        step_results, step_errors = out["step_results"], out["step_errors"]
        for i, err in enumerate(step_errors):
            if err:
                sql = pending[i - 1][0]["sql"] if 0 < i <= len(pending) else steps[i]["stmt"]["sql"]
                _raise_server_error(err, sql)
        if step_results[commit_step] is None:
            raise TursoError("libSQL batch was not committed (no step reported an error).")
        for i, (_, cur) in enumerate(pending, start=1):
            cur._set_result(step_results[i], is_write=True)

    def _run_now(self, stmt: dict, cur: Cursor, *, is_write: bool) -> None:
        self._flush()
        (res,) = self._pipeline([{"type": "execute", "stmt": stmt}], retry=not is_write)
        if res.get("type") == "error":
            _raise_server_error(res.get("error", {}), stmt["sql"])
        cur._set_result(res["response"]["result"], is_write=is_write)

    # -- the sqlite3 surface -------------------------------------------------

    def _execute(self, sql: str, params: Any, cur: Cursor) -> Cursor:
        verb = _verb(sql)
        if verb in ("BEGIN", "SAVEPOINT"):
            self._flush()  # the buffer already is the open transaction
            return cur
        if verb in ("COMMIT", "END", "RELEASE"):
            self.commit()
            return cur
        if verb == "ROLLBACK":
            self.rollback()
            return cur
        stmt = _stmt(sql, params)
        cur.rowcount = -1
        if verb in _BUFFERED_VERBS and "RETURNING" not in sql.upper():
            cur.rowcount = 0
            self._pending.append((stmt, cur))
        else:
            self._run_now(stmt, cur, is_write=verb in _BUFFERED_VERBS)
        return cur

    def _executemany(self, sql: str, seq_of_params: Iterable, cur: Cursor) -> Cursor:
        cur.rowcount = 0
        for params in seq_of_params:
            self._execute(sql, params, cur)
        return cur

    def cursor(self) -> Cursor:
        return Cursor(self)

    def execute(self, sql: str, params: Sequence | dict | None = None) -> Cursor:
        return self._execute(sql, params, Cursor(self))

    def executemany(self, sql: str, seq_of_params: Iterable) -> Cursor:
        return self._executemany(sql, seq_of_params, Cursor(self))

    def executescript(self, script: str) -> Cursor:
        """Run a multi-statement script in one request, like sqlite3 (commits pending writes first)."""
        self._flush()
        (res,) = self._pipeline([{"type": "sequence", "sql": script}], retry=False)
        if res.get("type") == "error":
            _raise_server_error(res.get("error", {}))
        return Cursor(self)

    def commit(self) -> None:
        self._flush()

    def rollback(self) -> None:
        self._pending = []

    @property
    def in_transaction(self) -> bool:
        return bool(self._pending)

    def close(self) -> None:
        """Close the connection. Like sqlite3, uncommitted (buffered) writes are discarded."""
        if self._closed:
            return
        self._pending = []
        self._closed = True
        self._session.close()

    # `with conn:` commits on success and rolls back on error, as sqlite3 does.
    def __enter__(self) -> "TursoConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


def connect(url: str, auth_token: str | None = None, *, timeout: float = DEFAULT_TIMEOUT) -> TursoConnection:
    """Open a connection to a libSQL server at an http(s) base URL (see config.remote_http_url)."""
    return TursoConnection(url, auth_token, timeout=timeout)
