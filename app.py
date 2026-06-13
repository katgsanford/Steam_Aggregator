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
    conn.close()
    # Scope options: all-time plus each release (newest first), with a flag for
    # which are "major" so the UI can mark them.
    scopes = [{"id": "all", "label": "All time", "is_major": True}]
    for r in sorted(releases, key=lambda x: x["date"], reverse=True):
        when = datetime.fromtimestamp(r["date"], timezone.utc).strftime("%Y-%m-%d")
        scopes.append({"id": r["gid"], "label": f"{when} — {r['title']}", "is_major": bool(r["is_major"])})

    meta = _language_eligibility(breakdown)
    meta["scopes"] = scopes
    return jsonify(meta)


def _bucketed_series(rows: list[tuple[int, int]]) -> list[dict]:
    """Bucket (ts, voted_up) rows into a % positive series.

    Bucket size adapts to span: weekly for <2 years of data, otherwise monthly,
    so the chart stays readable across both short and long histories.
    """
    if not rows:
        return []
    span = rows[-1][0] - rows[0][0]
    monthly = span > 2 * 365 * 24 * 3600
    buckets: dict[str, list[int]] = {}
    order: list[str] = []
    for ts, up in rows:
        d = datetime.fromtimestamp(ts, timezone.utc)
        key = f"{d.year}-{d.month:02d}" if monthly else f"{d.year}-W{d.isocalendar().week:02d}"
        if key not in buckets:
            buckets[key] = []
            order.append((key, ts))
        buckets[key].append(up)
    out = []
    running_total = 0
    running_positive = 0
    for key, first_ts in order:
        vals = buckets[key]
        running_total += len(vals)
        running_positive += sum(vals)
        out.append({
            "label": key,
            "ts": first_ts * 1000,  # ms for the chart
            "pct_positive": round(100 * running_positive / running_total, 1),
            "count": running_total,
        })
    return out


@app.route("/api/sentiment")
def api_sentiment():
    appid = int(request.args["appid"])
    scope = request.args.get("scope", "all")
    lang = request.args.get("lang", "all")
    conn = _conn()
    releases = db.list_releases(conn, appid)
    ts_from, ts_to = _resolve_window(releases, scope)
    rows = db.sentiment_rows(conn, appid, language=lang, ts_from=ts_from, ts_to=ts_to)
    conn.close()

    series = _bucketed_series(rows)
    total = len(rows)
    positive = sum(1 for _, up in rows if up)
    # Callouts: major releases that fall within the visible window (only meaningful
    # for the all-time view; per-release views are already a single window).
    callouts = []
    if scope == "all":
        for r in releases:
            if r["is_major"]:
                callouts.append({"ts": r["date"] * 1000, "title": r["title"], "url": r["url"]})
    return jsonify({
        "series": series,
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
    try:
        result = analyze.get_analysis(
            conn, appid, scope=scope, language_filter=lang, ts_from=ts_from, ts_to=ts_to,
            product_name=product_name, include_recent=(scope == "all"), force=force,
        )
        conn.commit()
        conn.close()
        return jsonify(result)
    except Exception as exc:
        conn.close()
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
