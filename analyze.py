"""LLM-backed analysis: theme extraction + executive summaries.

Uses the Anthropic API (anthropic SDK). Results are cached in the `analyses`
table keyed by the exact view (appid + scope + language filter), so the
dashboard only pays for a generation when the view is first requested or the
underlying review count has changed.

Requires ANTHROPIC_API_KEY in the environment. Model defaults to Opus 4.8 and
can be overridden with STEAM_REVIEW_MODEL.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List

import anthropic
from pydantic import BaseModel

import db

# Load secrets from a local .env (ANTHROPIC_API_KEY, optional STEAM_REVIEW_MODEL)
# before reading them. Guarded so the module still imports if python-dotenv
# isn't installed — the env vars can also be set the normal way.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ModuleNotFoundError:
    pass

# Theme extraction reads ~150 reviews per call and dominates token spend, so it
# defaults to the cheapest capable model. Summaries are tiny (stats + themes JSON)
# but benefit from a stronger writer, so they default higher. STEAM_REVIEW_MODEL,
# if set, is honored as a fallback for both (legacy single-model config).
_LEGACY_MODEL = os.environ.get("STEAM_REVIEW_MODEL")
EXTRACT_MODEL = os.environ.get("STEAM_EXTRACT_MODEL") or _LEGACY_MODEL or "claude-haiku-4-5"
SUMMARY_MODEL = os.environ.get("STEAM_SUMMARY_MODEL") or _LEGACY_MODEL or "claude-sonnet-4-6"

# Only these models accept adaptive thinking + the effort parameter; sending them
# to Haiku 4.5 / Sonnet 4.5 / older models returns a 400. The summary call adapts
# its params based on the chosen model so any model is safe to select.
_RICH_PARAM_MODELS = ("opus-4-6", "opus-4-7", "opus-4-8", "sonnet-4-6", "fable-5")


def _thinking_kwargs(model: str) -> dict:
    if any(tag in model for tag in _RICH_PARAM_MODELS):
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}
    return {}


WEEK_SECONDS = 7 * 24 * 3600

# A cached view is only refreshed once this many new reviews have accumulated
# since it was last generated. Below this, the saved analysis is served as-is
# (zero token spend) — so a daily trickle of reviews no longer forces a full
# regeneration. Override with STEAM_ANALYSIS_MIN_NEW.
REFRESH_MIN_NEW = int(os.environ.get("STEAM_ANALYSIS_MIN_NEW", "100"))

# The "last 7 days" themes use a moving window — roughly 1/7 of it turns over every
# day as old reviews age out — so they're refreshed on a time basis (default once
# per day) rather than by accumulated review count. Override STEAM_RECENT_REFRESH_HOURS.
RECENT_REFRESH_SECONDS = int(os.environ.get("STEAM_RECENT_REFRESH_HOURS", "24")) * 3600

# Below this many reviews in a window the sample is statistically thin (a proportion's
# 95% CI half-width is ~1/sqrt(n): ~10 pts at 100, ~14 at 50). We still generate, but
# the summary opens with a small-sample caveat and the UI flags it. Override with
# STEAM_ANALYSIS_LOW_CONFIDENCE.
LOW_CONFIDENCE_REVIEWS = int(os.environ.get("STEAM_ANALYSIS_LOW_CONFIDENCE", "100"))

# Hard floor + per-sentiment floor, applied only to the "primary" views (all-time and
# the current/latest release) via enforce_minimums. Below MIN_REVIEWS total, the view
# isn't generated at all (a note is shown instead); a sentiment side with fewer than
# MIN_BUCKET_REVIEWS is skipped rather than fabricated. Past release windows bypass
# these floors (they always generate, just caveated) so every patch shows something.
MIN_REVIEWS = int(os.environ.get("STEAM_ANALYSIS_MIN_REVIEWS", "25"))
MIN_BUCKET_REVIEWS = int(os.environ.get("STEAM_ANALYSIS_MIN_BUCKET", "10"))


class Theme(BaseModel):
    label: str          # short noun phrase, e.g. "Combat feel"
    description: str     # one sentence on what players are saying
    mention_estimate: int  # rough count of sampled reviews touching this theme
    representative_quote: str  # short snippet from the reviews, translated to English if needed


class ThemeList(BaseModel):
    themes: List[Theme]


def _client() -> anthropic.Anthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Set it in the environment before generating analyses."
        )
    return anthropic.Anthropic()


def _extract_themes(client: anthropic.Anthropic, product: str, reviews: list[str], sentiment: str) -> list[dict]:
    """Ask the model for the top 5 recurring themes among same-sentiment reviews."""
    if not reviews:
        return []
    joined = "\n\n---\n\n".join(f"[{i+1}] {r}" for i, r in enumerate(reviews))
    verb = "praise" if sentiment == "positive" else "complaint"
    resp = client.messages.parse(
        model=EXTRACT_MODEL,
        max_tokens=4000,
        system=(
            "You are a senior product analyst who reads player reviews and distills the "
            "recurring themes. Be specific and evidence-based; do not invent themes that "
            "aren't supported by the sample."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"These are {sentiment} Steam reviews for '{product}'. Identify the top 5 "
                    f"recurring {verb} themes across the sample. For each theme give a short label, "
                    f"a one-sentence description, an estimate of how many of the sampled reviews "
                    f"mention it, and one short representative quote. All output — including the "
                    f"quote — must be in English; if the source review is in another language, "
                    f"translate the quote into natural English.\n\n{joined}"
                ),
            }
        ],
        output_format=ThemeList,
    )
    parsed = resp.parsed_output
    if not parsed:
        return []
    return [t.model_dump() for t in parsed.themes[:5]]


def _update_themes(client: anthropic.Anthropic, product: str, prev_themes: list[dict],
                   new_reviews: list[str], sentiment: str) -> list[dict]:
    """Refresh an existing top-5 theme set using only the newly-collected reviews.

    Sends the prior themes plus the new-review sample (not the whole history), so
    input tokens scale with the delta rather than re-reading everything.
    """
    if not new_reviews:
        return prev_themes  # nothing new to fold in — keep what we had, no spend
    joined = "\n\n---\n\n".join(f"[{i+1}] {r}" for i, r in enumerate(new_reviews))
    verb = "praise" if sentiment == "positive" else "complaint"
    resp = client.messages.parse(
        model=EXTRACT_MODEL,
        max_tokens=4000,
        system=(
            "You are a senior product analyst maintaining a running view of player feedback. "
            "You are given the current top themes and a batch of newly collected reviews; produce "
            "an updated top 5 that reflects the combined picture."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Current top 5 {verb} themes for '{product}':\n"
                    f"{json.dumps(prev_themes, ensure_ascii=False, indent=2)}\n\n"
                    f"Below are NEW {sentiment} reviews collected since the last analysis. Update the "
                    f"top 5 {verb} themes for the full body of feedback: keep themes still prominent, "
                    f"refine descriptions/quotes, adjust mention estimates upward to reflect the new "
                    f"reviews, add any emerging theme, and drop any that has faded. Return exactly the "
                    f"top 5. All output — including quotes — must be in English; if a source review is "
                    f"in another language, translate the quote into natural English.\n\n{joined}"
                ),
            }
        ],
        output_format=ThemeList,
    )
    parsed = resp.parsed_output
    if not parsed or not parsed.themes:
        return prev_themes
    return [t.model_dump() for t in parsed.themes[:5]]


def _write_summary(client: anthropic.Anthropic, audience: str, product: str, scope_label: str,
                   stats: dict, themes_good: list[dict], themes_bad: list[dict],
                   low_confidence: bool = False) -> str:
    """Generate an executive summary tailored to a stakeholder audience."""
    if audience == "product":
        persona = (
            "You are a senior product analyst writing for product stakeholders (PM, design, "
            "leadership). Focus on player sentiment trajectory, what's driving love and churn risk, "
            "and the highest-leverage opportunities. Business-outcome oriented."
        )
    else:
        persona = (
            "You are a senior product analyst writing for the QA team. Focus on concrete defects, "
            "stability/performance complaints, reproducibility signals, and what to prioritize for "
            "testing and bug-fixing. Engineering-actionable, not marketing."
        )
    context = {
        "product": product,
        "scope": scope_label,
        "stats": stats,
        "top_positive_themes": themes_good,
        "top_negative_themes": themes_bad,
    }
    sample_note = ""
    if low_confidence:
        n = stats.get("total_reviews", 0)
        sample_note = (
            f"\n\nIMPORTANT — SMALL SAMPLE: this window contains only {n} reviews, which is "
            f"statistically thin. Open the summary with a one-line caveat that the sample is small "
            f"and the findings are directional, not conclusive. Hedge appropriately throughout "
            f"(say 'early signal'/'suggests' rather than 'proves'/'is'), and do not extrapolate "
            f"firm percentages or trends from so few data points."
        )
    resp = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=6000,  # headroom: adaptive thinking (when enabled) shares this budget
        **_thinking_kwargs(SUMMARY_MODEL),  # adaptive thinking/effort only on models that accept them
        system=persona,
        messages=[
            {
                "role": "user",
                "content": (
                    "Write a concise executive summary (about 200-300 words) based on the data below. "
                    "Use short paragraphs or tight bullets. Lead with the single most important takeaway, "
                    "then supporting detail, then a short prioritized recommendation list. Ground every "
                    "claim in the provided data.\n\n"
                    "IMPORTANT — be timeline-aware: the 'scope' field tells you exactly which period "
                    "these reviews cover. Do NOT describe a mid-life patch window as the game's launch "
                    "or a 'first-impression' moment unless the scope explicitly says it is the initial "
                    "release. Frame the takeaway relative to where this window sits in the product's "
                    "lifecycle (e.g. an established game reacting to a recent update, not a launch)."
                    f"{sample_note}\n\n"
                    f"{json.dumps(context, ensure_ascii=False, indent=2)}"
                ),
            }
        ],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _basic_stats(conn, appid: int, language_filter: str, ts_from, ts_to) -> dict:
    rows = db.sentiment_rows(conn, appid, language=language_filter, ts_from=ts_from, ts_to=ts_to)
    total = len(rows)
    positive = sum(1 for _, up in rows if up)
    pct = round(100 * positive / total, 1) if total else 0.0
    return {"total_reviews": total, "positive": positive, "negative": total - positive, "percent_positive": pct}


def get_analysis(conn, appid: int, *, scope: str, language_filter: str, ts_from, ts_to,
                 product_name: str, include_recent: bool, force: bool = False,
                 scope_label: str | None = None, enforce_minimums: bool = False) -> dict:
    """Return the full analysis bundle for a view, generating + caching as needed.

    Returns {themes_good, themes_bad, themes_recent, summary_product, summary_qa,
    stats, generated_at, stale}. Each LLM artifact is cached independently and
    regenerated only when missing, forced, or the review count has changed.

    scope_label is a human-readable, timeline-aware description of the window (e.g.
    "Reviews from the 'June 4 Update' release window (Apr 2024 launch + ~2 years;
    this patch shipped 2026-06-04, 9 days ago)"). It's handed to the summary model
    so it doesn't mistake a mid-life patch window for the game's launch.
    """
    stats = _basic_stats(conn, appid, language_filter, ts_from, ts_to)
    review_count = stats["total_reviews"]
    low_confidence = 0 < review_count < LOW_CONFIDENCE_REVIEWS
    if scope_label is None:
        scope_label = "All time" if scope == "all" else f"Release: {scope}"

    # Primary views (all-time / current release) enforce a hard floor: below it there
    # isn't enough signal to analyze, so we show a note instead of generating anything.
    if enforce_minimums and review_count < MIN_REVIEWS:
        return {
            "stats": stats,
            "themes_good": [], "themes_bad": [], "themes_recent": None,
            "summary_product": "", "summary_qa": "",
            "review_count": review_count,
            "low_confidence": False, "low_confidence_threshold": LOW_CONFIDENCE_REVIEWS,
            "insufficient": True, "min_reviews": MIN_REVIEWS,
        }

    # Per-sentiment floor (primary views only): skip a side that's too thin to theme
    # honestly. Past releases pass min_bucket=0 so they always attempt extraction.
    min_bucket = MIN_BUCKET_REVIEWS if enforce_minimums else 0
    client = None  # lazily created only if we actually need to generate something

    def cached_or_generate(kind: str, full_generator, incr_generator=None, max_age_seconds=None):
        """Serve from cache, refresh incrementally, or generate fresh.

        Staleness is decided one of two ways:
        - Time-based (max_age_seconds set): refresh once the cache is older than that.
          Used for the moving 7-day window, which turns over daily regardless of count.
        - Count-based (default): refresh once REFRESH_MIN_NEW new reviews have accrued;
          incrementally if an incr_generator is given, else a full rebuild.
        No cache (or force) → full generation.
        """
        nonlocal client
        hit = None if force else db.get_cached_analysis(conn, appid, scope, language_filter, kind)
        if hit:
            if max_age_seconds is not None:
                if (time.time() - hit["generated_at"]) < max_age_seconds:
                    return json.loads(hit["payload"])
            elif (review_count - hit["review_count"]) < REFRESH_MIN_NEW:
                return json.loads(hit["payload"])
        if client is None:
            client = _client()
        if hit and incr_generator is not None:
            value = incr_generator(json.loads(hit["payload"]), hit["generated_at"])
        else:
            value = full_generator()
        db.save_analysis(conn, appid, scope, language_filter, kind, review_count, json.dumps(value, ensure_ascii=False))
        conn.commit()  # commit each artifact so the write transaction never spans an LLM call
        return value

    def _full_themes(voted_up, sentiment, *, tf=ts_from, tt=ts_to, limit=150):
        """Full extraction for one sentiment side, honoring the per-bucket floor.

        Returns [] (no model call) when the side has fewer than min_bucket reviews,
        so primary views skip a too-thin side instead of inventing themes for it.
        """
        sample = db.sample_reviews(conn, appid, voted_up=voted_up, language=language_filter,
                                   ts_from=tf, ts_to=tt, limit=limit)
        if len(sample) < min_bucket:
            return []
        return _extract_themes(client, product_name, sample, sentiment)

    themes_good = cached_or_generate(
        "themes_good",
        lambda: _full_themes(1, "positive"),
        incr_generator=lambda prev, since: _update_themes(
            client, product_name, prev,
            db.sample_reviews(conn, appid, voted_up=1, language=language_filter, ts_from=ts_from, ts_to=ts_to, fetched_from=since, limit=100),
            "positive"),
    )
    themes_bad = cached_or_generate(
        "themes_bad",
        lambda: _full_themes(0, "negative"),
        incr_generator=lambda prev, since: _update_themes(
            client, product_name, prev,
            db.sample_reviews(conn, appid, voted_up=0, language=language_filter, ts_from=ts_from, ts_to=ts_to, fetched_from=since, limit=100),
            "negative"),
    )

    themes_recent = None
    recent_stats = None
    if include_recent:
        recent_from = int(time.time()) - WEEK_SECONDS
        themes_recent = cached_or_generate(
            "themes_recent",
            max_age_seconds=RECENT_REFRESH_SECONDS,
            full_generator=lambda: {
                "good": _full_themes(1, "positive", tf=recent_from, tt=None, limit=100),
                "bad": _full_themes(0, "negative", tf=recent_from, tt=None, limit=100),
            },
        )
        # Computed live (cheap) so the 7-day counts stay current even when the cached
        # themes are served — gives the recent mention estimates a denominator.
        recent_stats = _basic_stats(conn, appid, language_filter, recent_from, None)

    summary_product = cached_or_generate(
        "summary_product",
        lambda: {"text": _write_summary(client, "product", product_name, scope_label, stats, themes_good, themes_bad, low_confidence)},
    )
    summary_qa = cached_or_generate(
        "summary_qa",
        lambda: {"text": _write_summary(client, "qa", product_name, scope_label, stats, themes_good, themes_bad, low_confidence)},
    )

    return {
        "stats": stats,
        "themes_good": themes_good,
        "themes_bad": themes_bad,
        "themes_recent": themes_recent,
        "recent_stats": recent_stats,
        "summary_product": summary_product["text"],
        "summary_qa": summary_qa["text"],
        "review_count": review_count,
        "sample_limit": 150,  # mention estimates are drawn from up to this many reviews per side
        "low_confidence": low_confidence,
        "low_confidence_threshold": LOW_CONFIDENCE_REVIEWS,
        "insufficient": False, "min_reviews": MIN_REVIEWS,
    }
