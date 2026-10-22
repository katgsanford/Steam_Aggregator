"""Flask dashboard for collected Steam reviews.

Run:  python app.py   (then open http://127.0.0.1:5000)

Charts (sentiment over time + release callouts) are computed server-side from
SQLite and refilter instantly via the selectors. The AI parts (top-5 themes and
the two executive summaries) are generated via the Anthropic API on first request
for a given view and cached in the DB — see analyze.py.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# Load .env (ANTHROPIC_API_KEY etc.) before importing analyze, which reads them.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ModuleNotFoundError:
    pass

import analyze
import config
import db
from scopes import _language_eligibility, _resolve_window, _scope_label

app = Flask(__name__)


def _conn():
    # Local reviews.db by default; the Turso DB when DATABASE_URL is set (config.py).
    return db.connect(config.db_target())


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/titles")
def api_titles():
    conn = _conn()
    products = db.list_products(conn)
    conn.close()
    return jsonify(products)


@app.route("/api/meta")
def api_meta():
    appid = int(request.args["appid"])
    conn = _conn()
    breakdown = db.language_breakdown(conn, appid)
    releases = db.list_releases(conn, appid)
    last_collected = db.last_collected(conn, appid)
    conn.close()
    # Scope options: all-time plus each release (newest first), with a flag for
    # which are "major" so the UI can mark them.
    scopes = [{"id": "all", "label": "All time", "is_major": True}]
    for r in sorted(releases, key=lambda x: x["date"], reverse=True):
        when = datetime.fromtimestamp(r["date"], timezone.utc).strftime("%Y-%m-%d")
        scopes.append({"id": r["gid"], "label": f"{when} — {r['title']}", "is_major": bool(r["is_major"])})

    meta = _language_eligibility(breakdown)
    meta["scopes"] = scopes
    meta["last_collected"] = last_collected  # unix ts of newest stored review, or None
    return jsonify(meta)


@app.route("/api/breakdown")
def api_breakdown():
    """Language eligibility/stats for a single scope window.

    Same shape as the eligibility block in /api/meta, but the per-language counts,
    blended baseline, divergence, and own-analysis gate are all computed over the
    selected scope's review window instead of the full lifetime. The frontend
    refetches this on every scope change so the language breakdown table (and which
    languages route to their own AI analysis) tracks the current scope.
    """
    appid = int(request.args["appid"])
    scope = request.args.get("scope", "all")
    conn = _conn()
    releases = db.list_releases(conn, appid)
    ts_from, ts_to = _resolve_window(releases, scope)
    breakdown = db.language_breakdown(conn, appid, ts_from=ts_from, ts_to=ts_to)
    conn.close()
    return jsonify(_language_eligibility(breakdown))


@app.route("/api/sentiment")
def api_sentiment():
    appid = int(request.args["appid"])
    scope = request.args.get("scope", "all")
    lang = request.args.get("lang", "all")
    conn = _conn()
    releases = db.list_releases(conn, appid)
    ts_from, ts_to = _resolve_window(releases, scope)
    # Daily aggregates; the browser re-buckets these to day/week/month as the user
    # zooms (no re-query). The seed carries lifetime history before the window so the
    # cumulative line stays a true lifetime average regardless of scope/zoom.
    daily = db.daily_sentiment(conn, appid, language=lang, ts_from=ts_from, ts_to=ts_to)
    seed_total, seed_positive = (0, 0)
    if ts_from is not None:
        seed_total, seed_positive = db.cumulative_totals(conn, appid, language=lang, before=ts_from)
    conn.close()

    total = sum(c for _, c, _ in daily)
    positive = sum(up for _, _, up in daily)
    # Callouts: major releases (only meaningful on the all-time view; per-release
    # views are already a single window). In-view ones show; the rest are off-axis.
    callouts = []
    if scope == "all":
        for r in releases:
            if r["is_major"]:
                callouts.append({"ts": r["date"] * 1000, "title": r["title"], "url": r["url"]})
    return jsonify({
        "daily": [{"ts": ts * 1000, "c": c, "up": up} for ts, c, up in daily],
        "seed": {"total": seed_total, "positive": seed_positive},
        "callouts": callouts,
        "summary": {
            "total": total,
            "percent_positive": round(100 * positive / total, 1) if total else 0.0,
        },
    })


@app.route("/api/analysis")
def api_analysis():
    appid = int(request.args["appid"])
    scope = request.args.get("scope", "all")
    lang = request.args.get("lang", "all")
    force = request.args.get("force") == "1"
    conn = _conn()
    products = {p["appid"]: p["name"] for p in db.list_products(conn)}
    product_name = products.get(appid, str(appid))
    releases = db.list_releases(conn, appid)
    ts_from, ts_to = _resolve_window(releases, scope)
    # Show the "last 7 days" block on the all-time view and on the latest release
    # (its window runs up to now, so the trailing 7 days are meaningful there too —
    # even if that release shipped under 7 days ago).
    latest_gid = max(releases, key=lambda r: r["date"])["gid"] if releases else None
    # All-time and the current/latest release are the "primary" views: they get the
    # last-7-days block AND enforce the minimum-review floors. Past release windows
    # always generate (just caveated when small) so every patch shows something.
    is_primary = scope == "all" or scope == latest_gid
    scope_label = _scope_label(releases, scope, latest_gid, ts_to)
    try:
        result = analyze.get_analysis(
            conn, appid, scope=scope, language_filter=lang, ts_from=ts_from, ts_to=ts_to,
            product_name=product_name, include_recent=is_primary, force=force,
            scope_label=scope_label, enforce_minimums=is_primary,
        )
        conn.commit()
        conn.close()
        return jsonify(result)
    except Exception as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", "5000")))
