"""db_turso's HTTP encoding and decoding, against a scripted fake server (no network).

These always run. They pin down the wire format (Hrana over HTTP, /v2/pipeline),
the bearer header, buffering and the all-or-nothing commit batch, and error
mapping. The same adapter is exercised end to end by the other test files when
TEST_TURSO_URL points at a real libSQL server.
"""

from __future__ import annotations

import sqlite3

import pytest
import requests

import db_turso


class FakeResponse:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def ok_execute(cols=(), rows=(), affected=0, last_rowid=None):
    return {"type": "ok", "response": {"type": "execute", "result": {
        "cols": [{"name": c, "decltype": None} for c in cols], "rows": [list(r) for r in rows],
        "affected_row_count": affected, "last_insert_rowid": last_rowid}}}


class FakeSession:
    """Records every POST; answers from a queue of responses, else a generic success."""

    def __init__(self):
        self.posts: list[dict] = []
        self.queue: list = []
        self.closed = False

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if self.queue:
            nxt = self.queue.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        results = []
        for req in json["requests"]:
            if req["type"] == "execute":
                results.append(ok_execute())
            elif req["type"] == "batch":
                n = len(req["batch"]["steps"])
                step_results = [{"cols": [], "rows": [], "affected_row_count": 1, "last_insert_rowid": str(i)}
                                for i in range(n)]
                step_results[-1] = None  # the ROLLBACK step is skipped when COMMIT succeeds
                results.append({"type": "ok", "response": {"type": "batch", "result": {
                    "step_results": step_results, "step_errors": [None] * n}}})
            else:
                results.append({"type": "ok", "response": {"type": req["type"]}})
        return FakeResponse(200, {"baton": None, "base_url": None, "results": results})

    def close(self):
        self.closed = True


@pytest.fixture
def fake():
    return FakeSession()


@pytest.fixture
def tconn(fake):
    return db_turso.TursoConnection("https://steam-org.turso.io/", "tok-123", session=fake)


def test_endpoint_and_bearer_header(tconn, fake):
    tconn.execute("SELECT 1")
    post = fake.posts[0]
    assert post["url"] == "https://steam-org.turso.io/v2/pipeline"
    assert post["headers"]["Authorization"] == "Bearer tok-123"
    assert post["json"]["baton"] is None
    assert post["json"]["requests"][-1] == {"type": "close"}


def test_no_token_sends_no_auth_header(fake):
    conn = db_turso.TursoConnection("http://127.0.0.1:8080", None, session=fake)
    conn.execute("SELECT 1")
    assert "Authorization" not in fake.posts[0]["headers"]


def test_argument_encoding(tconn, fake):
    tconn.execute("SELECT ?,?,?,?,?,?,?", (None, True, 7, 2**63 - 1, 1.5, "té", b"\x00\xff"))
    args = fake.posts[0]["json"]["requests"][0]["stmt"]["args"]
    assert args == [
        {"type": "null"},
        {"type": "integer", "value": "1"},
        {"type": "integer", "value": "7"},
        {"type": "integer", "value": str(2**63 - 1)},  # integers travel as strings
        {"type": "float", "value": 1.5},
        {"type": "text", "value": "té"},
        {"type": "blob", "base64": "AP8="},
    ]
    tconn.execute("SELECT :a", {"a": 1})
    assert fake.posts[1]["json"]["requests"][0]["stmt"]["named_args"] == [
        {"name": "a", "value": {"type": "integer", "value": "1"}}]
    with pytest.raises(sqlite3.ProgrammingError):
        tconn.execute("SELECT ?", ([1, 2],))


def test_response_decoding_and_rows(tconn, fake):
    fake.queue.append(FakeResponse(200, {"results": [ok_execute(
        cols=["n", "f", "t", "z", "b"],
        rows=[[{"type": "integer", "value": "9007199254740993"}, {"type": "float", "value": 2.5},
               {"type": "text", "value": "x"}, {"type": "null"}, {"type": "blob", "base64": "AP8"}]],
    ), {"type": "ok", "response": {"type": "close"}}]}))
    cur = tconn.execute("SELECT ...")
    row = cur.fetchone()
    assert row["n"] == 9007199254740993 and isinstance(row["n"], int)  # beyond float precision
    assert row["F"] == 2.5 and row[2] == "x" and row["z"] is None and row["b"] == b"\x00\xff"
    assert dict(row) == {"n": 9007199254740993, "f": 2.5, "t": "x", "z": None, "b": b"\x00\xff"}
    assert [d[0] for d in cur.description] == ["n", "f", "t", "z", "b"]
    assert cur.fetchone() is None and cur.fetchall() == []
    with pytest.raises(IndexError):
        row["missing"]


def test_writes_are_buffered_until_commit(tconn, fake):
    c1 = tconn.execute("INSERT INTO t VALUES (?)", (1,))
    c2 = tconn.execute("UPDATE t SET x=?", (2,))
    assert fake.posts == [] and tconn.in_transaction
    tconn.commit()
    assert len(fake.posts) == 1
    (req, close) = fake.posts[0]["json"]["requests"]
    assert close == {"type": "close"} and req["type"] == "batch"
    steps = req["batch"]["steps"]
    assert [st["stmt"]["sql"] for st in steps] == ["BEGIN", "INSERT INTO t VALUES (?)", "UPDATE t SET x=?",
                                                   "COMMIT", "ROLLBACK"]
    # Each step runs only if the one before it succeeded; ROLLBACK only if COMMIT didn't run.
    assert "condition" not in steps[0]
    assert [st["condition"] for st in steps[1:4]] == [{"type": "ok", "step": i} for i in range(3)]
    assert steps[4]["condition"] == {"type": "not", "cond": {"type": "ok", "step": 3}}
    # rowcount / lastrowid are filled in once the batch has run.
    assert (c1.rowcount, c1.lastrowid, c2.rowcount) == (1, 1, 1)
    tconn.commit()  # nothing buffered: no request
    assert len(fake.posts) == 1


def test_read_flushes_buffer_first(tconn, fake):
    tconn.execute("DELETE FROM t")
    tconn.execute("SELECT * FROM t")
    assert [p["json"]["requests"][0]["type"] for p in fake.posts] == ["batch", "execute"]


def test_ddl_and_returning_run_immediately(tconn, fake):
    tconn.execute("  -- comment\nCREATE TABLE x(a)")
    tconn.execute("INSERT INTO x VALUES (1) RETURNING a")
    assert [p["json"]["requests"][0]["type"] for p in fake.posts] == ["execute", "execute"]


def test_close_discards_uncommitted_writes(tconn, fake):
    tconn.execute("INSERT INTO t VALUES (1)")
    tconn.close()
    assert fake.posts == [] and fake.closed
    with pytest.raises(sqlite3.ProgrammingError):
        tconn.execute("SELECT 1")


def test_rollback_and_transaction_statements(tconn, fake):
    tconn.execute("INSERT INTO t VALUES (1)")
    tconn.rollback()
    tconn.execute("BEGIN")
    tconn.execute("INSERT INTO t VALUES (2)")
    tconn.execute("COMMIT")
    assert len(fake.posts) == 1
    sqls = [st["stmt"]["sql"] for st in fake.posts[0]["json"]["requests"][0]["batch"]["steps"]]
    assert sqls == ["BEGIN", "INSERT INTO t VALUES (2)", "COMMIT", "ROLLBACK"]


def test_context_manager_commits_or_rolls_back(tconn, fake):
    with tconn:
        tconn.execute("INSERT INTO t VALUES (1)")
    assert len(fake.posts) == 1
    with pytest.raises(RuntimeError):
        with tconn:
            tconn.execute("INSERT INTO t VALUES (2)")
            raise RuntimeError
    assert len(fake.posts) == 1 and not tconn.in_transaction


def test_executescript_is_one_sequence_request(tconn, fake):
    tconn.execute("INSERT INTO t VALUES (1)")
    tconn.executescript("CREATE TABLE a(x); CREATE TABLE b(y);")
    assert [p["json"]["requests"][0]["type"] for p in fake.posts] == ["batch", "sequence"]
    assert fake.posts[1]["json"]["requests"][0]["sql"] == "CREATE TABLE a(x); CREATE TABLE b(y);"


def test_constraint_failure_raises_integrity_error_and_clears_buffer(tconn, fake):
    fake.queue.append(FakeResponse(200, {"results": [{"type": "ok", "response": {"type": "batch", "result": {
        "step_results": [{}, {}, None, None, None],
        "step_errors": [None, None, {"message": "UNIQUE constraint failed: t.x", "code": "SQLITE_CONSTRAINT"},
                        None, None]}}}, {"type": "ok", "response": {"type": "close"}}]}))
    tconn.execute("INSERT INTO t VALUES (1)")
    tconn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        tconn.commit()
    assert not tconn.in_transaction


def test_statement_error_raises_operational_error(tconn, fake):
    fake.queue.append(FakeResponse(200, {"results": [
        {"type": "error", "error": {"message": "no such table: nope", "code": "SQLITE_UNKNOWN"}},
        {"type": "ok", "response": {"type": "close"}}]}))
    with pytest.raises(sqlite3.OperationalError, match="no such table") as ei:
        tconn.execute("SELECT * FROM nope")
    assert ei.value.code == "SQLITE_UNKNOWN"


def test_http_401_is_a_clear_operational_error(tconn, fake):
    fake.queue.append(FakeResponse(401, text="Unauthorized"))
    with pytest.raises(sqlite3.OperationalError, match="TURSO_AUTH_TOKEN"):
        tconn.execute("SELECT 1")


def test_reads_retry_transient_failures_writes_do_not(tconn, fake, monkeypatch):
    monkeypatch.setattr(db_turso.time, "sleep", lambda s: None)
    fake.queue += [requests.ConnectionError("reset"), FakeResponse(503, text="busy")]
    assert tconn.execute("SELECT 1").fetchall() == []
    assert len(fake.posts) == 3

    fake.queue.append(FakeResponse(503, text="busy"))
    tconn.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(sqlite3.OperationalError, match="HTTP 503"):
        tconn.commit()
    assert len(fake.posts) == 4  # the write batch was sent once


def test_network_failure_is_operational_error(tconn, fake, monkeypatch):
    monkeypatch.setattr(db_turso.time, "sleep", lambda s: None)
    fake.queue += [requests.ConnectionError("down")] * 3
    with pytest.raises(sqlite3.OperationalError, match="request failed"):
        tconn.execute("SELECT 1")
