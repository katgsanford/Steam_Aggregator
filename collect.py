"""Daily unattended review collection for every tracked title.

Reads the shared DB path from config.json, loops over all titles in the
products table, and for each one pulls only reviews newer than what's already
stored (per language), upserts them, and logs the run. Each title also gets its
releases refreshed two ways: from the Steam news feed (tagged patch notes) and
from PICS (the live public-branch build id — catches silent pushes the news feed
misses). Designed to be invoked by Windows Task Scheduler with pythonw.exe.

MUST run under the project's Python 3.12 venv: the PICS step depends on the
`steam` package, whose gevent/protobuf stack only works on 3.12 (see
steam_api.py's module docstring for the full why). Run outside the venv it still
works — review + news-feed collection are unaffected — but the PICS public-build
detection silently no-ops. The scheduled task is registered against the venv's
pythonw.exe by register-task.ps1 for this reason.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import db
import steam_api

CONFIG_PATH = Path(__file__).with_name("config.json")
DB_PATH = Path(__file__).with_name("reviews.db")
LOG_PATH = Path(__file__).with_name("collect.log")


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _db_path() -> str:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("db_path", str(DB_PATH))
    return str(DB_PATH)


# Keywords that suggest a news post is a significant release rather than a minor
# hotfix. Steam has no authoritative "major" flag, so this is a curatable guess —
# the is_major column can be edited by hand to override these.
_MAJOR_HINTS = ("update", "patch", "season", "chapter", "expansion", "major",
                "anniversary", "launch", "release", "version", "dlc", "content")
_MINOR_HINTS = ("hotfix", "small patch", "minor", "tiny", "weekly", "post-launch")


def _looks_major(title: str) -> bool:
    t = title.lower()
    if any(h in t for h in _MINOR_HINTS):
        return False
    return any(h in t for h in _MAJOR_HINTS)


def _is_release(item: dict) -> bool:
    """A news post is a release only if Steam tagged it 'patchnotes' (update notes).

    This excludes store/community/marketing posts and third-party press, which
    share the news feed but aren't releases.
    """
    return any(str(t).lower() == "patchnotes" for t in (item.get("tags") or []))


# A public build push and a tagged patch-note post for the same update rarely
# share a timestamp to the second, so when correlating the two we treat any
# existing release within this window of the build's push time as "the same
# update already recorded" and skip creating a duplicate row.
_BUILD_DEDUP_WINDOW = 36 * 3600  # 36 hours


def record_public_build(conn: sqlite3.Connection, appid: int) -> bool:
    """Record the current PUBLIC-branch build as a release, if it's a new one.

    PICS (via steam_api.get_public_build_id) is the reliable update signal: it
    sees every push to the live branch, including silent ones the news feed
    misses. We turn each *new* public build id into a release row so it appears
    in the dashboard's scope selector like any other release.

    De-dup keeps the selector clean: if the build's push time lines up with a
    release we already have (a tagged patch note for the same update), we don't
    add a second row — a build only becomes its own release when the news feed
    DIDN'T cover it (exactly the gap PICS exists to fill).

    is_major=True: a PICS build is an authoritative depot push (the actual live
    version), so we flag it like a real release — it gets a ★ in the scope
    selector and a callout on the sentiment graph. Demote any to minor by editing
    the releases.is_major flag if a particular push isn't worth highlighting.

    Returns True if a new row was inserted. Requires the 3.12 venv (the `steam`
    package); outside it, get_public_build_id returns None and this is a no-op.
    """
    info = steam_api.get_public_build_id(appid)
    if not info:
        return False
    build_id, build_ts = info
    gid = f"build_{build_id}"

    # Idempotent: we already recorded this exact build on a previous run.
    if conn.execute("SELECT 1 FROM releases WHERE appid=? AND gid=?", (appid, gid)).fetchone():
        return False

    ts = build_ts if build_ts > 0 else int(time.time())
    # Only de-dup when we have a real push timestamp to correlate against.
    if build_ts > 0:
        near = conn.execute(
            "SELECT 1 FROM releases WHERE appid=? AND ABS(date - ?) <= ? LIMIT 1",
            (appid, ts, _BUILD_DEDUP_WINDOW),
        ).fetchone()
        if near:
            return False  # same update already recorded via the news feed

    db.upsert_release(conn, appid, gid, f"Build {build_id}", ts, is_major=True)
    return True


def refresh_releases(conn: sqlite3.Connection, appid: int) -> int:
    """Upsert the store launch date + patch-note posts as releases; return total known.

    Only items Steam tags `patchnotes` count as releases. Auto-flags the launch and
    major-looking patches as `is_major` on first insert; re-runs preserve manual
    is_major overrides. Also prunes any previously-stored rows that the current news
    window confirms are NOT patch notes (self-heals an older, unfiltered DB) — rows
    outside the fetch window are left untouched so legitimate old releases survive.

    This covers only news-feed (patch-note) releases; silent public-branch pushes
    are recorded separately by record_public_build (PICS).
    """
    launch = steam_api.get_release_date(appid)
    if launch:
        db.upsert_release(conn, appid, "launch", "Initial release", launch, is_major=True)
    try:
        news = steam_api.get_news(appid, count=100)
    except steam_api.SteamError:
        news = []

    prune = []  # gids in the current window that are confirmed non-releases
    for item in news:
        if not item["date"]:
            continue
        if not _is_release(item):
            prune.append(item["gid"])
            continue
        # Only set is_major on first insert; later runs pass None to keep manual edits.
        existing = conn.execute(
            "SELECT 1 FROM releases WHERE appid=? AND gid=?", (appid, item["gid"])
        ).fetchone()
        is_major = _looks_major(item["title"]) if existing is None else None
        db.upsert_release(conn, appid, item["gid"], item["title"], item["date"], item["url"], is_major=is_major)

    if prune:
        conn.executemany(
            "DELETE FROM releases WHERE appid=? AND gid=? AND gid != 'launch'",
            [(appid, g) for g in prune],
        )
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
            conn.commit()
            _log(f"    {code}: ERROR after {new_count} new ({exc})")
            continue
        db.log_run(conn, appid, code, new_count, max_ts)
        conn.commit()
        total += new_count
        _log(f"    {code} ({mode}): {new_count} new")
    return total


def main() -> int:
    conn = db.connect(_db_path())
    products = db.list_products(conn)
    if not products:
        _log("No titles tracked yet. Run setup.py first.")
        conn.close()
        return 1

    # fetched_at is fixed once per run (not per product/language) so the
    # downstream program can filter "all rows from today's run" with a single
    # WHERE clause against one timestamp, even across multiple titles.
    fetched_at = int(time.time())
    grand_total = 0
    _log(f"Run start: {len(products)} title(s)")
    for p in products:
        _log(f"  {p['name']} (appid {p['appid']})")
        try:
            rel_count = refresh_releases(conn, p["appid"])
            conn.commit()
            _log(f"    releases refreshed: {rel_count} known")
        except Exception as exc:  # release refresh is best-effort; never block review collection
            _log(f"    releases: ERROR ({exc})")
        # PICS public-build check is separate + best-effort: it needs the 3.12
        # venv (the `steam` package) and a CM connection, neither of which should
        # ever be able to abort review collection.
        try:
            if record_public_build(conn, p["appid"]):
                conn.commit()
                _log("    public build: new release recorded from PICS")
        except Exception as exc:
            _log(f"    public build: ERROR ({exc})")
        grand_total += collect_product(conn, p["appid"], p["name"], p["languages"], fetched_at)
    conn.close()
    _log(f"Run done: {grand_total} new reviews total -> {_db_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
