"""Flask dashboard for collected Steam reviews.

Run:  python app.py   (then open http://127.0.0.1:5000)

Charts (sentiment over time + release callouts) are computed server-side from
SQLite and refilter instantly via the selectors. The AI parts (top-5 themes and
the two executive summaries) are generated via the Anthropic API on first request
for a given view and cached in the DB — see analyze.py.
"""

from __future__ import annotations

import json
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
import db

CONFIG_PATH = Path(__file__).with_name("config.json")
DB_PATH = Path(__file__).with_name("reviews.db")

app = Flask(__name__)

# A language earns its own AI analysis only with enough volume AND enough sentiment
# divergence from the blended average — otherwise it either tracks the blend (adds
# nothing) or is too small to sample reliably. Tunable via env.
LANG_MIN_VOLUME = int(os.environ.get("STEAM_LANG_MIN_VOLUME", "1000"))
LANG_MIN_DIVERGENCE = float(os.environ.get("STEAM_LANG_MIN_DIVERGENCE", "8"))


def _db_path() -> str:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("db_path", str(DB_PATH))
    return str(DB_PATH)


def _conn():
    return db.connect(_db_path())


def _resolve_window(releases: list[dict], scope: str) -> tuple[int | None, int | None]:
    """Map a scope to a [ts_from, ts_to) review window.

    'all' → the whole history (None, None). A release gid → from that release's
    date up to the next release's date (or now for the latest one).
    """
    if scope == "all":
        return None, None
    ordered = sorted(releases, key=lambda r: r["date"])
    for i, rel in enumerate(ordered):
        if rel["gid"] == scope:
            ts_from = rel["date"]
            ts_to = ordered[i + 1]["date"] if i + 1 < len(ordered) else None
            return ts_from, ts_to
    return None, None


def _scope_label(releases: list[dict], scope: str, latest_gid: str | None, ts_to: int | None) -> str:
    """Human-readable, timeline-aware description of the analysis window.

    Gives the summary model enough context to place the window in the product's
    lifecycle, so it won't frame a mid-life patch as the game's launch.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ordered = sorted(releases, key=lambda r: r["date"])
    if scope == "all":
        if ordered:
            launch = datetime.fromtimestamp(ordered[0]["date"], timezone.utc).strftime("%Y-%m-%d")
            return (f"ALL reviews across the game's entire lifetime, from launch ({launch}) through "
                    f"today ({today}). This is cumulative lifetime sentiment, NOT a launch snapshot — "
                    f"the launch was {launch} and most of these reviews accrued in the months/years "
                    f"since. Do NOT frame current sentiment as a 'launch', 'day-one', or "
                    f"'first-impression' problem; the game is well past launch.")
        return f"All reviews to date (full lifetime history). Today is {today}."

    rel = next((r for r in ordered if r["gid"] == scope), None)
    if rel is None:
        return f"Release window '{scope}'. Today is {today}."

    launch_ts = ordered[0]["date"] if ordered else rel["date"]
    rel_date = datetime.fromtimestamp(rel["date"], timezone.utc).strftime("%Y-%m-%d")
    days_since_launch = max(0, (rel["date"] - launch_ts) // 86400)
    is_launch = rel["gid"] == ordered[0]["gid"]
    is_latest = rel["gid"] == latest_gid

    if ts_to is None:
        end = "now (this is the most recent release; window runs to the present)"
    else:
        end = datetime.fromtimestamp(ts_to, timezone.utc).strftime("%Y-%m-%d") + " (the next release)"

    if is_launch:
        position = "This IS the game's initial launch window."
    else:
        yrs = days_since_launch / 365.0
        age = f"~{yrs:.1f} years" if yrs >= 1 else f"~{days_since_launch} days"
        position = (f"This is a post-launch update, NOT the launch — it shipped {age} after the "
                    f"game's original launch. The game was already established by this point.")

    return (f"Reviews posted in the '{rel['title']}' release window: from {rel_date} to {end}. "
            f"{position}{' This is the current/latest release.' if is_latest and not is_launch else ''} "
            f"Today is {today}.")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/titles")
def api_titles():
    conn = _conn()
    products = db.list_products(conn)
    conn.close()
    return jsonify(products)


def _language_eligibility(breakdown: list[tuple[str, int, int]]) -> dict:
    """Decide which languages (and the Chinese bucket) qualify for their own analysis.

    Returns dropdown lists plus per-language stats so the UI can mark eligible
    languages and explain fallbacks. Eligible = volume ≥ threshold AND sentiment
    diverges ≥ threshold from the blend. When the combined Chinese bucket qualifies,
    it replaces the individual schinese/tchinese options for *analysis* (they remain
    selectable for the chart).
    """
    total = sum(c for _, c, _ in breakdown)
    totup = sum(u for _, _, u in breakdown)
    blend = round(100 * totup / total, 1) if total else 0.0

    stats: dict = {"_blend": blend}
    chart_langs: list[str] = []  # collected codes, ordered by volume (for the chart filter)
    for code, c, up in sorted(breakdown, key=lambda x: -x[1]):
        pct = round(100 * up / c, 1) if c else 0.0
        delta = round(pct - blend, 1)
        stats[code] = {"count": c, "pct": pct, "delta": delta,
                       "eligible": c >= LANG_MIN_VOLUME and abs(delta) >= LANG_MIN_DIVERGENCE}
        chart_langs.append(code)

    # Combined Chinese bucket
    cn_members = [c for c in ("schinese", "tchinese") if c in stats]
    chinese = None
    if cn_members:
        c = sum(stats[m]["count"] for m in cn_members)
        up = sum(round(stats[m]["count"] * stats[m]["pct"] / 100) for m in cn_members)
        pct = round(100 * up / c, 1) if c else 0.0
        delta = round(pct - blend, 1)
        chinese = {"count": c, "pct": pct, "delta": delta,
                   "eligible": c >= LANG_MIN_VOLUME and abs(delta) >= LANG_MIN_DIVERGENCE}
        stats["chinese"] = chinese

    analysis = ["all"]
    bucket_on = bool(chinese and chinese["eligible"])
    if bucket_on:
        chart_langs.append("chinese")
        analysis.append("chinese")
    for code, _, _ in breakdown:
        if stats[code]["eligible"] and not (bucket_on and code in cn_members):
            analysis.append(code)

    return {"languages": chart_langs, "analysis_languages": analysis, "lang_stats": stats,
            "thresholds": {"min_volume": LANG_MIN_VOLUME, "min_divergence": LANG_MIN_DIVERGENCE}}


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
