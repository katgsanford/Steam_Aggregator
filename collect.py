"""Daily unattended review collection for every tracked title.

Reads the shared DB path from config.json, loops over all titles in the
products table, and for each one pulls only reviews newer than what's already
stored (per language), upserts them, and logs the run. Each title also gets its
releases refreshed: the store launch date, plus one release per new public-branch
build id (PICS, with the api.steamcmd.net mirror as fallback). Build ids are the
ONLY release signal — news posts are not releases. Designed to be invoked by
Windows Task Scheduler with pythonw.exe.

collect_all() does the work and returns per-title results; main() is the thin
wrapper for local use.

Run under the project's Python 3.12 venv: the PICS WebSocket step depends on the
`steam` package, whose gevent/protobuf stack only works on 3.12 (see
steam_api.py's module docstring for the full why). Run outside the venv it still
works — review collection is unaffected and build ids come from the HTTPS mirror
instead. The scheduled task is registered against the venv's pythonw.exe by
register-task.ps1 for this reason.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from pathlib import Path

import config
import db
import steam_api

LOG_PATH = Path(__file__).with_name("collect.log")


def _log(msg: str) -> None:
    """Log to stdout AND collect.log (a cloud sandbox's file is thrown away; stdout isn't)."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # a read-only checkout still logs to stdout


class _LogBridge(logging.Handler):
    """Route steam_api's warnings (why a build source failed) into _log."""

    def emit(self, record: logging.LogRecord) -> None:
        _log(f"    {record.getMessage()}")


logging.getLogger("steam_api").addHandler(_LogBridge(level=logging.WARNING))


def _db_path() -> str:
    # Local reviews.db by default; the Turso DB when DATABASE_URL is set (config.py).
    return config.db_target()


def _is_release(item: dict) -> bool:
    """True if Steam tagged a news post 'patchnotes' (update notes).

    Not used to create releases any more — build ids are the only release signal.
    Kept for callers that show patch-note posts as context next to a build.
    """
    return any(str(t).lower() == "patchnotes" for t in (item.get("tags") or []))


_FETCH = object()  # record_public_build: "look the build up yourself"


def _newest_build(conn: sqlite3.Connection, appid: int):
    """The newest stored build release row (gid, date) for a title, or None."""
    return conn.execute(
        "SELECT gid, date FROM releases WHERE appid=? AND substr(gid, 1, 6)='build_' "
        "ORDER BY date DESC LIMIT 1",
        (appid,),
    ).fetchone()


def record_public_build(conn: sqlite3.Connection, appid: int, build=_FETCH) -> dict:
    """Record the current PUBLIC-branch build as a release, if it's a new one.

    The public-branch build id (steam_api.build_source: PICS over WebSocket, then the
    api.steamcmd.net mirror) is the one release signal: it sees every push to the live
    branch, silent ones included. Each *new* build id becomes a release row so it
    appears in the dashboard's scope selector like any other release. Nothing else
    (news posts, nearby releases) suppresses a build.

    is_major=True: a build is an authoritative depot push (the actual live version),
    so we flag it like a real release — it gets a ★ in the scope selector and a
    callout on the sentiment graph. Demote any to minor by editing the
    releases.is_major flag if a particular push isn't worth highlighting.

    `build` is a steam_api.BuildInfo (or None for "no source answered") when the
    caller already resolved it, e.g. collect_all's single PICS login for every title;
    left out, it is looked up here.

    Returns the build info:
      {appid, build_id, build_ts, source, previous_build_id, inserted, status}
    where status is
      'new'         a build we hadn't seen, and an earlier build was on record
      'baseline'    the first build on record for this title: stored, but nothing
                    to compare with, so no "update" claim
      'unchanged'   the build we already hold
      'unavailable' no source answered; make no claim about updates
    """
    prior = _newest_build(conn, appid)
    if build is _FETCH:
        build = steam_api.build_source(appid, min_timeupdated=prior["date"] if prior else 0)
    result = {
        "appid": appid,
        "build_id": None,
        "build_ts": None,
        "source": None,
        "previous_build_id": prior["gid"][len("build_"):] if prior else None,
        "inserted": False,
        "status": "unavailable",
    }
    if not build:
        return result

    result.update(build_id=build.build_id, build_ts=build.timeupdated, source=build.source)
    gid = f"build_{build.build_id}"

    # Idempotent: we already recorded this exact build on a previous run.
    if conn.execute("SELECT 1 FROM releases WHERE appid=? AND gid=?", (appid, gid)).fetchone():
        result["status"] = "unchanged"
        return result

    ts = build.timeupdated if build.timeupdated > 0 else int(time.time())
    db.upsert_release(conn, appid, gid, f"Build {build.build_id}", ts, is_major=True)
    result.update(inserted=True, status="new" if prior else "baseline")
    return result


def refresh_releases(conn: sqlite3.Connection, appid: int) -> int:
    """Upsert the store launch date as the 'launch' release; return total releases known.

    News-feed posts are NOT releases: they include posts that aren't patches, so build
    ids (record_public_build) are the only release signal. Release rows an earlier
    version created from news posts are left in place as history — nothing is deleted
    or pruned here — so existing scopes and their cached analyses keep working.
    """
    launch = steam_api.get_release_date(appid)
    if launch:
        db.upsert_release(conn, appid, "launch", "Initial release", launch, is_major=True)
    return conn.execute("SELECT COUNT(*) FROM releases WHERE appid=?", (appid,)).fetchone()[0]


def collect_product(conn: sqlite3.Connection, appid: int, name: str, languages: list[str], fetched_at: int) -> int:
    """Pull new reviews for every language of one title and upsert them.

    Returns the total number of new rows written across all languages.
    A SteamError on one language is logged and skipped rather than aborting
    the whole run, so a transient failure for one language doesn't prevent the
    others from collecting.
    """
    total = 0
    for code in languages:
        stop_before = db.last_seen_ts(conn, appid, code)
        mode = "backfill" if stop_before == 0 else f"since ts {stop_before}"
        new_count = 0
        max_ts = stop_before
        try:
            for review in steam_api.iter_reviews(appid, code, stop_before_ts=stop_before):
                db.upsert_review(conn, appid, code, review, fetched_at)
                new_count += 1
                ts = int(review.get("timestamp_created", 0))
                if ts > max_ts:
                    max_ts = ts
                # Commit every 500 rows so a crash mid-language doesn't lose
                # everything; the next run will skip already-stored reviews.
                if new_count % 500 == 0:
                    conn.commit()
        except steam_api.SteamError as exc:
            # Record the failure as a runs row too (status='error'), so feed health
            # can report it and a later backfill knows this language may be incomplete.
            db.log_run(conn, appid, code, new_count, max_ts, status="error", error=str(exc))
            conn.commit()
            _log(f"    {code}: ERROR after {new_count} new ({exc})")
            continue
        db.log_run(conn, appid, code, new_count, max_ts)
        conn.commit()
        total += new_count
        _log(f"    {code} ({mode}): {new_count} new")
    return total


def _run_outcome(conn: sqlite3.Connection, appid: int, language: str, since: int) -> tuple[str, str | None, int | None]:
    """(status, error, new_count) of the runs row a language wrote at or after `since`.

    A language that left no row at all (it failed before one could be written) counts
    as an error with new_count None. new_count is what was stored, including the rows
    stored before a failure.
    """
    row = conn.execute(
        "SELECT * FROM runs WHERE appid=? AND language=? AND run_at>=? ORDER BY rowid DESC LIMIT 1",
        (appid, language, since),
    ).fetchone()
    if row is None:
        return "error", "no run recorded (collection failed)", None
    row = dict(row)
    return row.get("status") or "complete", row.get("error"), row.get("new_count")


def collect_all(conn: sqlite3.Connection) -> dict:
    """Refresh releases and collect new reviews for every tracked title.

    Never raises for a per-title problem: release, build-id and per-language review
    failures are recorded in the result and the run carries on. Returns:

      {fetched_at, total_new, build_source, build_status, titles: [
         {appid, name,
          releases: {count, error},
          build: record_public_build's info dict,
          reviews: {new, languages: [{language, new, status, error}]}}]}

    build_source / build_status are the run-level values job_runs stores:
    build_source is 'pics_ws' if any title was answered by PICS, else 'steamcmd_net'
    if any was answered by the mirror, else 'none'; build_status is 'ok' (every title
    has a build id), 'partial' (some) or 'unavailable' (none).
    """
    products = db.list_products(conn)
    # fetched_at is fixed once per run (not per product/language) so the
    # downstream program can filter "all rows from today's run" with a single
    # WHERE clause against one timestamp, even across multiple titles.
    fetched_at = int(time.time())
    result = {"fetched_at": fetched_at, "total_new": 0, "build_source": "none",
              "build_status": "unavailable", "titles": []}
    if not products:
        return result

    _log(f"Run start: {len(products)} title(s)")
    # One build-id lookup for every title (a single PICS login), best-effort: it
    # needs the `steam` stack and a CM connection or the HTTPS mirror, and none of
    # those may ever be able to abort review collection.
    appids = [p["appid"] for p in products]
    builds: dict = {}
    try:
        newest = {a: (_newest_build(conn, a) or {"date": 0})["date"] for a in appids}
        builds = steam_api.build_sources(appids, min_timeupdated=newest)
    except Exception as exc:
        _log(f"  public builds: ERROR ({exc})")

    for p in products:
        appid = p["appid"]
        _log(f"  {p['name']} (appid {appid})")
        title = {"appid": appid, "name": p["name"],
                 "releases": {"count": None, "error": None}, "build": None,
                 "reviews": {"new": 0, "languages": []}}
        result["titles"].append(title)

        try:
            title["releases"]["count"] = refresh_releases(conn, appid)
            conn.commit()
            _log(f"    releases refreshed: {title['releases']['count']} known")
        except Exception as exc:  # release refresh is best-effort; never block review collection
            title["releases"]["error"] = str(exc)
            _log(f"    releases: ERROR ({exc})")

        try:
            title["build"] = record_public_build(conn, appid, build=builds.get(appid))
            conn.commit()
        except Exception as exc:
            title["build"] = {"appid": appid, "build_id": None, "build_ts": None, "source": None,
                              "previous_build_id": None, "inserted": False,
                              "status": "unavailable", "error": str(exc)}
            _log(f"    public build: ERROR ({exc})")
        b = title["build"]
        if b["status"] == "unavailable":
            _log("    public build: unavailable (no source answered)")
        else:
            _log(f"    public build {b['build_id']} via {b['source']}: {b['status']}")

        # One collect_product call per language, so each language gets its own result.
        for code in p["languages"]:
            since = int(time.time())
            try:
                new = collect_product(conn, appid, p["name"], [code], fetched_at)
                status, error, stored = _run_outcome(conn, appid, code, since)
                if stored is not None:
                    new = stored
            except Exception as exc:  # a bug or DB error must not stop the other languages
                try:
                    conn.rollback()
                except Exception:
                    pass
                new, status, error = 0, "error", str(exc)
                _log(f"    {code}: ERROR ({exc})")
            title["reviews"]["languages"].append(
                {"language": code, "new": new, "status": status, "error": error})
            title["reviews"]["new"] += new
        result["total_new"] += title["reviews"]["new"]

    sources = {t["build"]["source"] for t in result["titles"] if t["build"]["source"]}
    answered = sum(1 for t in result["titles"] if t["build"]["status"] != "unavailable")
    if steam_api.SOURCE_PICS_WS in sources:
        result["build_source"] = steam_api.SOURCE_PICS_WS
    elif sources:
        result["build_source"] = steam_api.SOURCE_MIRROR
    result["build_status"] = ("ok" if answered == len(result["titles"])
                              else "partial" if answered else "unavailable")
    _log(f"Run done: {result['total_new']} new reviews total")
    return result


def main() -> int:
    """Local entry point: collect every tracked title into the configured database."""
    conn = db.connect(_db_path())
    if not db.list_products(conn):
        _log("No titles tracked yet. Run setup.py first.")
        conn.close()
        return 1
    try:
        collect_all(conn)
    finally:
        conn.close()
    _log(f"Database: {_db_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
