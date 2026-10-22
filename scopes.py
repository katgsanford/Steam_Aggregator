"""Scope windows, scope labels and language-eligibility rules for the dashboard and the daily job.

Pure functions with no Flask import, so app.py, the daily job and the detectors all
apply the same rules. A "scope" is either 'all' (whole history) or a release gid;
its review window runs from that release's date to the next release's date.

LANG_MIN_VOLUME / LANG_MIN_DIVERGENCE decide which languages earn their own AI
analysis: a language needs enough volume AND enough sentiment divergence from the
blended average — otherwise it either tracks the blend (adds nothing) or is too
small to sample reliably. Tunable via env; the defaults match .env.example.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

LANG_MIN_VOLUME = int(os.environ.get("STEAM_LANG_MIN_VOLUME", "500"))
LANG_MIN_DIVERGENCE = float(os.environ.get("STEAM_LANG_MIN_DIVERGENCE", "8"))


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
