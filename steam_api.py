"""Thin wrappers around Steam's public store + review endpoints.

No API key is required for any of these. All calls go through a single
session with a browser-like User-Agent, a timeout, and bounded retry/backoff
so a transient hiccup doesn't kill an unattended run.
"""

from __future__ import annotations

import html
import re
import time
from typing import Iterator

import requests

STORE_SEARCH = "https://store.steampowered.com/api/storesearch/"
APP_DETAILS = "https://store.steampowered.com/api/appdetails"
APP_REVIEWS = "https://store.steampowered.com/appreviews/{appid}"
APP_NEWS = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"

_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "SteamReviewCollector/1.0"
        )
    }
)

# Steam's appdetails reports human-readable language *names*, but the review
# endpoint wants its own API *codes*. This bridges the two. Names are matched
# case-insensitively after stripping the trailing "*" / markup Steam adds.
LANG_NAME_TO_CODE = {
    "english": "english",
    "german": "german",
    "deutsch": "german",
    "french": "french",
    "français": "french",
    "italian": "italian",
    "spanish - spain": "spanish",
    "spanish": "spanish",
    "spanish - latin america": "latam",
    "latin american spanish": "latam",
    "portuguese - portugal": "portuguese",
    "portuguese": "portuguese",
    "portuguese - brazil": "brazilian",
    "brazilian portuguese": "brazilian",
    "russian": "russian",
    "polish": "polish",
    "dutch": "dutch",
    "danish": "danish",
    "finnish": "finnish",
    "norwegian": "norwegian",
    "swedish": "swedish",
    "czech": "czech",
    "hungarian": "hungarian",
    "romanian": "romanian",
    "bulgarian": "bulgarian",
    "greek": "greek",
    "turkish": "turkish",
    "ukrainian": "ukrainian",
    "japanese": "japanese",
    "korean": "koreana",
    "thai": "thai",
    "vietnamese": "vietnamese",
    "indonesian": "indonesian",
    "simplified chinese": "schinese",
    "traditional chinese": "tchinese",
    "arabic": "arabic",
}


class SteamError(RuntimeError):
    """Raised when Steam returns something we can't proceed with."""


def _get(url: str, params: dict, *, retries: int = 4, backoff: float = 1.5) -> requests.Response:
    """GET with exponential backoff. 429s get a longer pause than network errors.

    429 sleep uses attempt+1 (so the first 429 already waits 1.5^2*2=4.5 s) to
    avoid hammering Steam when it's actively rate-limiting us. Network/5xx errors
    use attempt (shorter initial pause) since they're usually transient.
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = _SESSION.get(url, params=params, timeout=30)
            if resp.status_code == 429:  # rate limited
                time.sleep(backoff ** (attempt + 1) * 2)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:  # network / 5xx
            last_exc = exc
            time.sleep(backoff ** attempt)
    raise SteamError(f"Request to {url} failed after {retries} attempts: {last_exc}")


def search_apps(term: str, *, country: str = "us") -> list[dict]:
    """Resolve a product name to candidate apps. Each item has id + name."""
    resp = _get(STORE_SEARCH, {"term": term, "cc": country, "l": "english"})
    data = resp.json()
    return data.get("items", []) or []


def get_supported_languages(appid: int) -> list[tuple[str, str]]:
    """Return [(display_name, review_code), ...] for languages we can map.

    Languages not in LANG_NAME_TO_CODE are silently skipped — the review
    endpoint would reject them anyway. The `seen` set deduplicates regional
    variants that map to the same code (e.g. both "Spanish - Spain" and
    "Spanish" → "spanish").
    """
    resp = _get(APP_DETAILS, {"appids": appid, "l": "english"})
    payload = resp.json().get(str(appid), {})
    if not payload.get("success"):
        raise SteamError(f"Steam returned no details for appid {appid} (gated or invalid?).")
    raw = payload["data"].get("supported_languages", "")
    # Steam returns an HTML snippet like:
    #   "English, French<strong>*</strong>, ... *languages with full audio support"
    # Unescape entities, strip all tags, then drop the footnote before splitting.
    raw = re.sub(r"<[^>]+>", "", html.unescape(raw))
    raw = raw.split("languages with full audio")[0]
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in raw.split(","):
        name = chunk.replace("*", "").strip()
        if not name:
            continue
        code = LANG_NAME_TO_CODE.get(name.lower())
        if code and code not in seen:
            seen.add(code)
            out.append((name, code))
    return out


def get_app_name(appid: int) -> str:
    """Return the canonical store name for an appid, falling back to the id string."""
    resp = _get(APP_DETAILS, {"appids": appid, "l": "english"})
    payload = resp.json().get(str(appid), {})
    if payload.get("success"):
        return payload["data"].get("name", str(appid))
    return str(appid)


def get_release_date(appid: int) -> int | None:
    """Return the store release date as a unix timestamp, or None if unparseable.

    Steam reports release_date.date as a localized string like "17 Sep, 2020";
    we try a few common formats and give up gracefully if none match.
    """
    import datetime as _dt

    resp = _get(APP_DETAILS, {"appids": appid, "l": "english"})
    payload = resp.json().get(str(appid), {})
    if not payload.get("success"):
        return None
    rd = payload["data"].get("release_date", {}) or {}
    date_str = rd.get("date", "").strip()
    if not date_str:
        return None
    for fmt in ("%d %b, %Y", "%b %d, %Y", "%d %B, %Y", "%B %d, %Y", "%Y"):
        try:
            return int(_dt.datetime.strptime(date_str, fmt).replace(tzinfo=_dt.timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def get_news(appid: int, *, count: int = 50) -> list[dict]:
    """Return patch-note / announcement posts for an app, newest-first.

    Each item: {gid, title, url, date (unix), feedlabel, tags, contents}.
    Steam has no authoritative "major release" flag, so callers decide which
    of these to mark major (see db.upsert_release / the dashboard).
    """
    resp = _get(
        APP_NEWS,
        {"appid": appid, "count": count, "maxlength": 600, "format": "json"},
    )
    items = resp.json().get("appnews", {}).get("newsitems", []) or []
    out = []
    for it in items:
        out.append(
            {
                "gid": str(it.get("gid")),
                "title": it.get("title", ""),
                "url": it.get("url", ""),
                "date": int(it.get("date", 0)),
                "feedlabel": it.get("feedlabel", ""),
                "tags": it.get("tags", []) or [],
                "contents": it.get("contents", ""),
            }
        )
    return out


def iter_reviews(
    appid: int,
    language: str,
    *,
    stop_before_ts: int = 0,
    page_pause: float = 1.0,
) -> Iterator[dict]:
    """Yield reviews newest-first for one language.

    Stops early once a review's timestamp_created is <= stop_before_ts, which
    is how the daily run stays incremental. With stop_before_ts=0 it walks the
    entire history (used for the first backfill).
    """
    # "*" is Steam's sentinel meaning "start from the first page".
    # Each response returns the cursor for the next page; an unchanged or
    # empty cursor means we've reached the end.
    cursor = "*"
    # Guard against Steam returning a cursor we've already visited, which
    # would cause an infinite loop if the API behaves unexpectedly.
    seen_cursors: set[str] = set()
    while True:
        resp = _get(
            APP_REVIEWS.format(appid=appid),
            {
                "json": 1,
                "language": language,
                "filter": "recent",      # newest-first; required for incremental cut-off to work
                "review_type": "all",
                "purchase_type": "all",
                "num_per_page": 100,
                "cursor": cursor,
            },
        )
        data = resp.json()
        if data.get("success") != 1:
            raise SteamError(f"Review fetch failed for appid {appid} ({language}).")
        reviews = data.get("reviews", []) or []
        if not reviews:
            return
        for review in reviews:
            if int(review.get("timestamp_created", 0)) <= stop_before_ts:
                # Reviews are ordered newest-first, so anything at or before
                # stop_before_ts is already stored — we're done with this language.
                return
            yield review
        cursor = data.get("cursor", "")
        if not cursor or cursor in seen_cursors:
            return
        seen_cursors.add(cursor)
        time.sleep(page_pause)
