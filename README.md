# Steam Review Collector

Collects all Steam reviews for one or more titles in selected languages, runs
automatically once a day, and stores everything in a single SQLite DB for
another program to ingest. Uses Steam's public endpoints only — **no API key
required**.

## Pieces

| File | Role |
|------|------|
| `steam_api.py` | Steam endpoint wrappers (store/reviews/news), public build-id lookup (PICS over WebSocket, then the steamcmd.net mirror), language name→code map, retry/backoff |
| `db.py` | SQLite schema (products + reviews + runs + releases + analyses) + upsert/dedup logic |
| `setup.py` | **Interactive**, run per title: pick app + languages → adds to `products` |
| `collect.py` | **Unattended** daily run: loops every tracked title, pulls only new reviews |
| `register-task.ps1` | Registers the daily Windows scheduled task (noon by default) |
| `export.py` | Optional JSON dump (filter by title/appid/today) for tools that can't read SQLite |
| `analyze.py` | LLM theme extraction + executive summaries (Anthropic API), cached in the DB |
| `scopes.py` | Flask-free scope windows, scope labels and the per-language analysis gate (shared by the dashboard and the daily job) |
| `app.py` + `templates/` | Flask dashboard: KPI strip, sentiment chart, release callouts, themes, summaries |
| `clear_cache.py` | Wipe cached summaries/themes from `analyses` so they regenerate (e.g. after a prompt change) |

## Setup

> **Requires Python 3.12** (not 3.13+). The PICS update-tracking path depends on
> the `steam` package, whose `gevent`/`protobuf` stack only works on 3.12 — 3.13/3.14
> removed stdlib modules it imports and lack compatible wheels. Run everything from a
> 3.12 `.venv`. If your default Python is something else, install 3.12 alongside it
> and use the `py -3.12` launcher (SETUP.md has the beginner walkthrough).

```powershell
git clone https://github.com/katgsanford/Steam_Aggregator.git
cd Steam_Aggregator                     # the folder the clone just created

py -3.12 -m venv .venv                  # create the 3.12 environment
.\.venv\Scripts\Activate.ps1            # activate it (prompt shows (.venv))
pip install -r requirements.txt         # core: requests, flask, anthropic, pydantic, python-dotenv
                                        #  + PICS: steam, gevent-eventemitter, protobuf<3.21

python setup.py                        # add a title: search, choose languages
python setup.py                        # run again to add more titles
python collect.py                      # first run = full backfill (test it)

# schedule daily at noon (override with -Time "09:30" if you want).
# register-task.ps1 auto-targets .venv\Scripts\pythonw.exe so the daily run has `steam`.
powershell -ExecutionPolicy Bypass -File .\register-task.ps1
```

`setup.py` walks you through, per title:
1. Type the product name → Steam search shows matches → pick the right one.
2. It reads the store page's supported languages → you pick one, several, or `all`.
3. Adds the title to the `products` table in `reviews.db` (re-adding updates it).

Tracked titles persist in the DB, so adding a second title never disturbs the
first. `config.json` just records the shared DB path.

## What runs daily

At **12:00 noon** Task Scheduler runs `collect.py` with the venv's `pythonw.exe`
(no window). It loops over **every** title in `products`, and for each language
walks reviews newest-first, stopping as soon as it reaches one already stored —
so each day only fetches what's new. It also refreshes the `releases` table: the
store launch date, plus one release per new public-branch **build id** (see
[Release data](#release-data)). Progress is printed and appended to `collect.log`.

## Output for the downstream program

A single SQLite file, `reviews.db`, holding all titles:

- **`products`** — one row per tracked title: `appid` (PK), `name` (indexed via
  `idx_products_name`), `languages`, `added_at`.
- **`reviews`** — one row per review, keyed on `recommendationid` (no dupes),
  indexed on `(appid, language)`. Columns: appid, language, author_steamid,
  playtime, voted_up, review_text, timestamp_created/updated, votes_up,
  votes_funny, weighted_vote_score, steam_purchase, fetched_at.
- **`runs`** — one row per (run, appid, language) with `new_count` and
  `max_ts_seen`, so the downstream program can detect what changed each day.
- **`releases`** — launch + one entry per new public build id per title
  (`gid`, `title`, `date`, `url`, `is_major`); builds use `gid = build_<id>`. Rows created
  from news posts by older versions stay as history. Drives the dashboard's scope selector
  and graph callouts.
- **`analyses`** — cached LLM output (themes + summaries) keyed by view, so the
  dashboard doesn't re-call the API on every load.

Ingest examples:
```sql
-- everything, with title name attached
SELECT p.name AS title, r.*
FROM reviews r JOIN products p ON p.appid = r.appid
ORDER BY r.timestamp_created DESC;

-- one title by name (name is indexed)
SELECT r.* FROM reviews r JOIN products p ON p.appid = r.appid
WHERE p.name = 'Hades';

-- only what today's run added
SELECT * FROM reviews WHERE fetched_at >= strftime('%s','now','start of day');
```

Or produce JSON: `python export.py --today`, `python export.py --title Hades`.

## Dashboard

A local Flask web app visualizes the collected data.

```powershell
# Put ANTHROPIC_API_KEY in a .env file (copy .env.example → .env) — app.py loads it
# automatically. Or set it for the session: $env:ANTHROPIC_API_KEY = "sk-ant-..."
python app.py                            # then open http://127.0.0.1:5000
```

**Selectors** (top of the page):
- **Title** — every tracked title from the `products` table.
- **Scope** — *All time*, or any individual release (★ marks the major ones). Per-release
  scope windows reviews between that release and the next.
- **Language** — *All collected*, a single language code, or the combined **Chinese** bucket
  (schinese+tchinese). Languages marked ⭐ get their **own** AI themes/summaries; the rest filter
  the chart but show the all-languages analysis (with a note), because they either track the
  blended sentiment or are too small to analyze reliably.

**KPI strip** — a row of headline cards at the top: total reviews and % positive (with Δ vs the
blended average) for the current Scope/Language, plus title-level metrics — languages collected,
latest release, and when data was last collected. A **light/dark toggle** (top-right, defaults to
dark, remembered per browser) re-themes the whole dashboard including the chart.

**Sentiment over time** — rolling **% positive** (from Steam's thumbs `voted_up`). Two lines: a
smoothed **cumulative** overall trend (always a *lifetime* running average — it carries history from
before the window so it never restarts/spikes) plus a **per-period** line. The chart is **zoomable**
— scroll/pinch on it, drag the **navigator** brush below, use the **1m/3m/6m/1y/All** presets, or
type a From/To range. As you zoom, the per-period line **re-buckets in the browser** (daily ≤60 days,
weekly ≤2 years, else monthly) with no re-query: the server sends a daily base series and the
cumulative seed once, and the browser aggregates. On the all-time view, **major releases appear as
dashed callouts** on the graph.

**Language breakdown** — a table of each language's review count, % positive, and divergence (Δ)
from the blended average, with a badge showing whether it gets its **own** AI analysis, folds into
the **chinese** bucket, or reuses the all-languages analysis. The table's **counts, % positive,
blended baseline, and divergence track the selected Scope** — switch to a release window and they
recompute over just that window. **Eligibility (which languages get their own analysis) stays
computed against all time**, so the ⭐/own-analysis badges don't flip as you change scope; a
language that qualifies overall keeps its own analysis in every scope. This is the at-a-glance "how
did each language do in this window" view.

**Common review themes** — top 5 liked + top 5 disliked for the selected scope, extracted by the
model. On the all-time view, an extra **Last 7 days** block shows the most recent themes.

**Executive summaries** — two AI-written reports in a single **tabbed panel**: one for **product
stakeholders** (sentiment trajectory, opportunities, churn risk) and one for the **QA team**
(defects, stability/perf, test priorities). The model's markdown is rendered (headings, bold,
bullets), not shown as raw text.

### How the AI parts work / cost control
- `analyze.py` calls the Anthropic API with a **per-task model split** (it samples the most-upvoted
  reviews per sentiment for theme extraction). No model ID is hardcoded at a call site — every one
  is resolved in `models.py`, which asks the Models API for the **newest model in each family** and
  caches the answer for a day, so a new Haiku or Opus is picked up without a code change:
  - **Theme extraction** reads ~150 reviews per call (the token-heavy step) → the current
    **Haiku**, at `effort: low`.
  - **Executive summaries** are tiny (stats + themes JSON) but quality-sensitive → the current
    **Opus**, with adaptive thinking at `effort: high`.
  - Pin a specific model with `STEAM_EXTRACT_MODEL` / `STEAM_SUMMARY_MODEL` / `STEAM_NARRATIVE_MODEL`
    (or `STEAM_REVIEW_MODEL` for every role, legacy); set `STEAM_MODEL_AUTO=0` to skip discovery and use the pinned fallbacks in
    `models.py`. Request params (adaptive thinking / effort) are gated on the model's version, so
    any model — including an older Haiku — is safe to select.
  - Tune effort per task with `STEAM_EXTRACT_EFFORT` / `STEAM_SUMMARY_EFFORT` / `STEAM_NARRATIVE_EFFORT`
    (`low|medium|high|xhigh|max`). Raising extraction effort costs on **every** review batch;
    raising summary effort costs one short call per view, so that's the cheap one to experiment
    with. An unrecognised value is ignored with a warning instead of failing the call.
- Every artifact is **cached in the `analyses` table** keyed by (title, scope, language). Saved
  analyses are **retained and reused** until at least `STEAM_ANALYSIS_MIN_NEW` reviews (default 100)
  have accumulated for that view — so a daily trickle of new reviews no longer forces a regenerate.
- When a view does cross that threshold, themes are **refreshed incrementally**: the model is given
  the previously saved themes plus only the *newly collected* reviews (not the whole history), so
  token spend scales with the delta. The two summaries are then rewritten from the updated themes
  (they're cheap — derived from stats + themes, not raw review text).
- The **Last 7 days** themes are the exception: that window is a moving one (~1/7 turns over
  daily), so they refresh on a **time** basis — once per day (`STEAM_RECENT_REFRESH_HOURS`,
  default 24) — not by the review-count threshold.
- Click **Regenerate** to force a full rebuild for the current view regardless of either rule.
- To rebuild cached output in bulk (e.g. after changing a prompt), run `python clear_cache.py`
  (summaries only by default; `--themes` or `--all`, optionally `--appid <id>`) — each cleared view
  regenerates the next time it's opened.
- **Small-sample views** (fewer than `STEAM_ANALYSIS_LOW_CONFIDENCE` reviews, default 100 — common
  for low-traffic late patches) are still analyzed, but the summary opens with a directional caveat
  and the dashboard shows a small-sample banner. Lower the threshold if your patch windows run thin.
- **Minimum-review floors apply only to the primary views** (all-time + the current/latest release):
  below `STEAM_ANALYSIS_MIN_REVIEWS` (default 25) the view shows a "not enough reviews" note instead
  of generating, and a sentiment side with fewer than `STEAM_ANALYSIS_MIN_BUCKET` (default 10) reviews
  is skipped rather than invented. **Past release windows bypass these floors** — they always generate
  (with the small-sample caveat when thin) so every historical patch shows something.

### Per-language analysis gate
A language only gets its own themes/summaries when it has **enough volume** *and* **diverges
enough in sentiment** from the blended average — otherwise it adds nothing over the all-languages
view or is too small to sample reliably. Thresholds: `STEAM_LANG_MIN_VOLUME` (default 500 reviews)
and `STEAM_LANG_MIN_DIVERGENCE` (default 8 percentage points). The two Chinese variants are pooled
into a single **chinese** bucket so their shared (and usually divergent) signal clears the volume
bar. Non-qualifying languages still filter the chart; they just reuse the all-languages analysis,
so you don't spend tokens generating near-duplicate or noisy themes.
- Without `ANTHROPIC_API_KEY` set, the charts still work fully; the theme/summary panels show a
  notice instead of erroring.

### Release data
**Build ids are the only release signal.** Each run `collect.py` records:

1. **The launch** — the store release date, as the `launch` release.
2. **A release per new public build id.** `steam_api.build_source` reads the **public** branch's
   current build id and push time (beta/experimental branches are ignored), trying two sources in
   order: Steam's **PICS** over a WebSocket CM connection on port 443 (the same data SteamDB reads;
   needs the 3.12 venv and the `steam` package), then the **api.steamcmd.net** community mirror over
   plain HTTPS (a fallback that needs no extra packages; an answer older than the newest build we
   already hold is treated as stale and ignored). A new build id is recorded as a release
   (`gid = build_<id>`, `is_major=1` — a build is an authoritative depot push). The first build seen
   for a title is stored as its baseline with no "update" claim. If both sources fail, nothing is
   recorded for that title that day.

News posts are **not** releases (the feed carries posts that are not patches), so they are no
longer stored. Release rows that an earlier version created from news posts are left in place as
history; nothing is deleted. **"Major"** drives the graph callouts; build rows are always flagged
major. Override which are major by editing the flag:
```sql
UPDATE releases SET is_major = 1 WHERE appid = <id> AND gid = '<gid>';
```

## Managing the schedule

```powershell
schtasks /Run    /TN "SteamReviewCollector"     # run now
schtasks /Query  /TN "SteamReviewCollector"     # status
schtasks /Delete /TN "SteamReviewCollector" /F  # remove
```

## Notes / limits

- First run backfills the **entire** review history for each language of each
  title — for a popular title that can be tens of thousands of reviews
  (one-time). With several titles the first run can be long; daily runs after
  that are small.
- A few age-/region-gated titles may not return store details; `setup.py` will
  say so if Steam refuses.
- To track more titles, just re-run `setup.py` — each title is appended to the
  `products` table and picked up by the next daily run automatically.
- To stop tracking a title: `DELETE FROM products WHERE appid = <id>;`
  (its existing reviews stay in the DB unless you also delete from `reviews`).
