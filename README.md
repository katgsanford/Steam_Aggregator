# Steam Review Collector

Collects all Steam reviews for one or more titles in selected languages, runs
automatically once a day, and stores everything in a single SQLite DB for
another program to ingest. Uses Steam's public endpoints only — **no API key
required**.

## Pieces

| File | Role |
|------|------|
| `steam_api.py` | Steam endpoint wrappers, language name→code map, retry/backoff |
| `db.py` | SQLite schema (products + reviews + runs) + upsert/dedup logic |
| `setup.py` | **Interactive**, run per title: pick app + languages → adds to `products` |
| `collect.py` | **Unattended** daily run: loops every tracked title, pulls only new reviews |
| `register-task.ps1` | Registers the daily Windows scheduled task (noon by default) |
| `export.py` | Optional JSON dump (filter by title/appid/today) for tools that can't read SQLite |
| `analyze.py` | LLM theme extraction + executive summaries (Anthropic API), cached in the DB |
| `app.py` + `templates/` | Flask dashboard: sentiment chart, release callouts, themes, summaries |

## Setup

```powershell
cd C:\Users\katgs\Documents\WardrobeAI\steam_reviews
pip install -r requirements.txt        # requests (already installed here)

python setup.py                        # add a title: search, choose languages
python setup.py                        # run again to add more titles
python collect.py                      # first run = full backfill (test it)

# schedule daily at noon (override with -Time "09:30" if you want)
powershell -ExecutionPolicy Bypass -File .\register-task.ps1
```

`setup.py` walks you through, per title:
1. Type the product name → Steam search shows matches → pick the right one.
2. It reads the store page's supported languages → you pick one, several, or `all`.
3. Adds the title to the `products` table in `reviews.db` (re-adding updates it).

Tracked titles persist in the DB, so adding a second title never disturbs the
first. `config.json` just records the shared DB path.

## What runs daily

At **12:00 noon** Task Scheduler runs `collect.py` with `pythonw.exe` (no
window). It loops over **every** title in `products`, and for each language
walks reviews newest-first, stopping as soon as it reaches one already stored —
so each day only fetches what's new. It also refreshes the `releases` table from
Steam's news feed. Progress is appended to `collect.log`.

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
- **`releases`** — launch + patch/news entries per title (`gid`, `title`, `date`,
  `url`, `is_major`); drives the dashboard's scope selector and graph callouts.
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
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # required for the AI parts
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

**Sentiment over time** — rolling **% positive** (from Steam's thumbs `voted_up`), bucketed
weekly (or monthly for long histories). On the all-time view, **major releases appear as dashed
callouts** on the graph. Charts refilter instantly client-side; no API calls.

**Language breakdown** — a per-title table of each language's review count, % positive, and
divergence (Δ) from the blended average, with a badge showing whether it gets its **own** AI
analysis, folds into the **chinese** bucket, or reuses the all-languages analysis. This is the
at-a-glance "why did this language (not) qualify" view.

**Common review themes** — top 5 liked + top 5 disliked for the selected scope, extracted by the
model. On the all-time view, an extra **Last 7 days** block shows the most recent themes.

**Executive summaries** — two AI-written reports, each in its **own panel**: one for **product
stakeholders** (sentiment trajectory, opportunities, churn risk) and one for the **QA team**
(defects, stability/perf, test priorities).

### How the AI parts work / cost control
- `analyze.py` calls the Anthropic API with a **per-task model split** (it samples the most-upvoted
  reviews per sentiment for theme extraction):
  - **Theme extraction** reads ~150 reviews per call (the token-heavy step) → defaults to
    `claude-haiku-4-5` (`STEAM_EXTRACT_MODEL`).
  - **Executive summaries** are tiny (stats + themes JSON) but quality-sensitive → default to
    `claude-sonnet-4-6` (`STEAM_SUMMARY_MODEL`).
  - Set `STEAM_REVIEW_MODEL` to force a single model for both (legacy). Request params (adaptive
    thinking / effort) are chosen per model, so any model — including Haiku — is safe to select.
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
- **Small-sample views** (fewer than `STEAM_ANALYSIS_LOW_CONFIDENCE` reviews, default 100 — common
  for low-traffic late patches) are still analyzed, but the summary opens with a directional caveat
  and the dashboard shows a small-sample banner. Lower the threshold if your patch windows run thin.

### Per-language analysis gate
A language only gets its own themes/summaries when it has **enough volume** *and* **diverges
enough in sentiment** from the blended average — otherwise it adds nothing over the all-languages
view or is too small to sample reliably. Thresholds: `STEAM_LANG_MIN_VOLUME` (default 1000 reviews)
and `STEAM_LANG_MIN_DIVERGENCE` (default 8 percentage points). The two Chinese variants are pooled
into a single **chinese** bucket so their shared (and usually divergent) signal clears the volume
bar. Non-qualifying languages still filter the chart; they just reuse the all-languages analysis,
so you don't spend tokens generating near-duplicate or noisy themes.
- Without `ANTHROPIC_API_KEY` set, the charts still work fully; the theme/summary panels show a
  notice instead of erroring.

### Release data
`collect.py` refreshes releases each run from the store launch date plus the Steam news feed —
but **only posts Steam tags `patchnotes`** (i.e. actual update/patch notes) count as releases.
Store, community, marketing, and third-party press posts share the same feed and are excluded;
any that were stored before this filter are pruned automatically on the next refresh.

Among the patch notes, **"major"** is a heuristic (launch + update/version/season titles, excluding
hotfixes/minor) and drives the graph callouts. Override which are major by editing the flag:
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
