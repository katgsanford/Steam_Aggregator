# Plan: turning Steam_Aggregator into an independent agent

Status: proposal, no code changed. Written 2026-10-22 against commit `8408ba3`.
Assumes the concurrent `models.py` work has landed (role-based model resolution: newest
Haiku/Opus via `client.models.list()`, env overrides, per-role effort).

Model names and prices quoted here come from the Anthropic `claude-api` reference as of
2026-10-06: Claude Haiku 5.5 (`claude-haiku-5-5`, $0.10 in / $0.50 out per million tokens
for prompts up to 100K) and Claude Opus 5.5 (`claude-opus-5-5`, $4 in / $20 out, cache
reads $0.20). Because `models.py` resolves the "newest" model at runtime, the model ID (and
its price) can change without a code change. Section 7 deals with that.

---

## 1. What "independent agent" means here

Today a person drives everything. Task Scheduler runs `collect.py` once a day
(`register-task.ps1:48-50`). Nothing is analyzed until someone opens the dashboard and a
view's `/api/analysis` call reaches `analyze.get_analysis` (`app.py:255-286`). Nothing
tells you a patch went badly. You only find out if you happen to look.

The working definition for this plan is a system that:

1. Decides when to collect: more often right after a new build, less often when things are quiet.
2. Decides when analysis is worth paying for. LLM spend happens only when the data changed meaningfully.
3. Notices specific kinds of change: a sentiment shift after a patch or build, an emerging
   complaint theme, a review bomb, a language whose sentiment diverges from the rest, and
   problems with its own data feed.
4. Reports these on its own, with evidence, and takes internal actions (re-collect,
   refresh cached analyses, schedule a follow-up check). Anything that reaches people other
   than the owner waits for a human to approve it.

### Goals

- G1. No-click operation. The owner gets a short message when something meaningful happens and a weekly digest otherwise.
- G2. Alerts are specific and backed by evidence: numbers with windows and sample sizes, linked release or build, and 3-5 quoted reviews with IDs.
- G3. Predictable cost: hard daily and monthly LLM spend ceilings, and near-zero spend on quiet days.
- G4. Polite to Steam: the request rate stays at or below today's (about 1 request/s while paging, `steam_api.py:275`), with a global cap.
- G5. Runs on the current Windows + Python 3.12 venv setup with no new always-on infrastructure for the MVP.
- G6. Auditable: every run, signal, LLM call, and outbound message is recorded in SQLite and visible in the dashboard.

### Non-goals (for now)

- Posting to Steam (developer responses, announcements). That needs Steamworks partner credentials and is high-risk.
- Auto-filing tickets in Jira, Linear, or GitHub without approval.
- Web research beyond Steam: no web search tool, no Reddit or Discord scraping.
- Multi-user hosting, auth, or a public dashboard. `app.py:290` runs Flask with `debug=True` and must stay on localhost.
- Replacing the dashboard. The agent adds a feed to it.
- Real-time (sub-hour) reaction. An hour or so of latency is fine.

---

## 2. Current system: what the agent can build on, and what gets in the way

| Area | What exists | Implication for the agent |
|---|---|---|
| Collection | `collect.main()` loops all titles: releases, then PICS, then reviews (`collect.py:201-235`). `collect_product` is incremental via `db.last_seen_ts` (`collect.py:165-198`). | Good base, but it only logs to `collect.log`. It returns a count and nothing structured. Errors on a language are logged and skipped with no DB record (`collect.py:190-193`). |
| Run log | `runs` table: `run_at, appid, language, new_count, max_ts_seen` (`db.py:51-57`). | No run id, no status, no error column. A failed language is invisible in the DB. |
| Release detection | News feed `patchnotes` filter (`collect.py:62-68`, `123-162`) plus PICS public build id (`collect.py:78-120`, `steam_api.py:216-266`), with 36h dedup (`collect.py:75`). | The best "something shipped" trigger already exists. PICS opens one anonymous CM login per title (`steam_api.py:244-248`). It could batch all appids into one call. |
| Incremental reviews | `iter_reviews` walks `filter=recent` newest-first and stops at the last seen `timestamp_created` (`steam_api.py:270-319`). | Edited reviews are never re-fetched. Players often flip a review after a patch, and that is exactly the signal we want. `upsert_review` would update them (`db.py:138-184`), but the walk never reaches them. See Phase 2. |
| Review metadata | `upsert_review` stores playtime, `voted_up`, votes, `steam_purchase` (`db.py:149-183`). | It drops `author.num_reviews` and `author.num_games_owned`, which are useful review-bomb markers. |
| Stats | `daily_sentiment`, `language_breakdown`, `count_reviews`, `cumulative_totals` (`db.py:280-381`). | Enough for every detector in Section 5. `analyze._basic_stats` pulls every row into Python to count (`analyze.py:241-246`). Replace it with SQL aggregates before the agent calls it often. |
| Analysis cache | `analyses` keyed by (appid, scope, language, kind) (`db.py:75-84`). Count-based refresh `REFRESH_MIN_NEW=100` (`analyze.py:63`). Time-based 7-day themes (`analyze.py:68`). Write is committed per artifact, never held across an LLM call (`analyze.py:311`). | Already idempotent and cheap to re-ask. The agent can call `get_analysis` to pre-warm primary views. |
| Scope logic | `_resolve_window`, `_scope_label`, `_language_eligibility` live in `app.py` (`app.py:54-174`), which imports Flask at module top (`app.py:19`). | The agent needs these without Flask. Move them (Phase 0). |
| Config | `_db_path()` is duplicated in `collect.py:41`, `app.py:44`, `clear_cache.py`, `export.py`, `setup.py`. | Consolidate before adding a sixth copy. |
| Thresholds | `LANG_MIN_VOLUME` defaults to 1000 in `app.py:40`, but `.env.example` documents 500 (commit `4a58c40`). | Fix the drift in Phase 0 so the detector and dashboard agree. |
| Tests | None. | Detectors need a fixture DB and unit tests from the start. |
| Platform | Windows Task Scheduler with `-StartWhenAvailable` (`register-task.ps1:50`), so missed runs fire after wake. Python 3.12 pinned by `steam`/`gevent`/`protobuf<3.21` (`steam_api.py` docstring, `requirements.txt`). | The agent must run in the same venv if it touches PICS, or isolate PICS in a subprocess. The machine has to be awake for patch-day monitoring. |

---

## 3. Architecture options

All options share the same core idea. **Deterministic code does the scheduling, collection,
and change detection. The model is used only where judgment or writing is needed:**
deciding whether a flagged change matters, pulling extra evidence, and writing the alert.
Letting an LLM decide when to call Steam, or compute percentages, would be slower, costlier,
and harder to trust than SQL. The options differ in where the LLM loop runs and who hosts it.

### Option A. Local scheduled Python agent using the Messages API tool runner (recommended)

How it works:

- A new `agent.py tick` entry point, registered with Task Scheduler hourly. Same venv, same
  `register-task.ps1` pattern, a second task or a replacement for the daily one.
- Each tick runs cheap probes, a collection decision, collection, the detectors, and then,
  only if a signal is new or escalated (or a digest is due), an LLM investigation. It
  finishes with dispatch.
- The investigation uses `client.beta.messages.tool_runner(...)` with `@beta_tool` functions
  that wrap existing `db`/`analyze` functions (Section 4). The model is Opus via
  `models.py` (role `agent`). Theme tagging uses Haiku (role `extract`).
- State lives in new SQLite tables (Section 8). Notifications go out over webhooks or SMTP from the same process.

Trade-offs:

- Cost: only model tokens. No hosting cost. Quiet ticks cost $0 in LLM spend: a few Steam
  HTTP calls plus one PICS login.
- Ops burden: lowest. The setup you already have, plus one scheduled task. You own the loop
  code, but the tool runner keeps it small. You add hard caps by counting iterations in the
  `for message in runner:` loop.
- Where it runs: your Windows PC, so it only watches while the PC is awake.
  `StartWhenAvailable` catches up after sleep, but a patch that ships overnight while the
  laptop is closed is seen in the morning. The code is portable, so moving to an always-on
  Linux box with a 3.12 venv later is a deployment change, not a rewrite.
- Python 3.12: no conflict. `anthropic` runs on 3.12. PICS (gevent) stays in the collection
  step. If gevent and the HTTP client misbehave in one process, run collection as a
  subprocess (`python collect.py --json`) and keep the agent process gevent-free. Check this
  in Phase 0.
- Risks: the tool runner is a beta helper in the Python SDK. Pin the `anthropic` version and
  keep a fallback manual loop. The process has no hosted observability, so you build the
  run log yourself (Section 9). It is small.

### Option B. Claude Agent SDK, run locally on the same schedule

How it works: the same tick and detectors, but the investigation step calls the Claude Agent
SDK (`claude-agent-sdk` on PyPI). That is the Claude Code harness packaged as a library. The
project functions are exposed as custom tools through an in-process MCP server, and the
built-in tools (Bash, Write, Edit, etc.) are restricted with the SDK's allowed-tools options
and permission hooks. (The `claude-api` reference does not cover this SDK. Verify specifics
at code.claude.com/docs/en/agent-sdk before building.)

Trade-offs:

- Cost: same per-token API pricing. It tends to use more tokens per run because the harness
  carries a larger system prompt and tool set.
- Ops burden: higher than A. It brings the Claude Code runtime along. On Windows that is
  supported, but it is one more moving part to keep updated next to a venv that is already
  pinned for `steam`.
- Fit: the Agent SDK is good at long, open-ended file and shell work, with subagents,
  sessions, resume, and context management. This agent's runs are short and bounded, and it
  should never need a shell. Most of what the SDK adds would have to be switched off for
  safety.
- When it would win: if you later want the agent to change the project itself, for example
  proposing prompt or threshold changes as a branch. That is a coding task, and that harness
  is built for it.

### Option C. Claude Managed Agents with a scheduled deployment

How it works: you define the agent (model, system prompt, tools) once as a versioned object.
A scheduled deployment (cron plus IANA timezone) creates a session for each firing. Anthropic
runs the agent loop. Each session can carry a hard dollar budget, which is copied from the
deployment. Every firing writes a deployment-run record. Memory stores give persistence
across sessions.

The problem is that your data and the Steam/PICS code live on your PC. There are two ways to bridge that:

- C1, cloud environment: tools run in an Anthropic-hosted container. The SQLite DB would
  have to move off your PC (a hosted DB, or uploading the DB each run). The PICS CM
  connection would need egress to Steam's CM servers, which is impractical to allow-list,
  so you would need unrestricted networking. The `steam`/3.12 stack would have to install in
  the sandbox. That is a large refactor for this project.
- C2, self-hosted environment: the loop stays at Anthropic, and tools run on your machine
  through an outbound-polling worker (`EnvironmentWorker`). No inbound ports, and the DB
  stays local. But the SDK worker requires `/bin/bash` at that exact path, and memory stores
  need a POSIX host. On Windows that means WSL2, with the 3.12 venv rebuilt inside it, plus
  an always-on worker process.

Trade-offs:

- Cost: model tokens. Check current Managed Agents pricing for any session or runtime
  charges on top. Hard per-session dollar budgets are a real plus.
- Ops burden: C1 means re-platforming the data layer. C2 means WSL plus an always-on worker
  plus several beta APIs (agents, environments, deployments, memory stores).
- Benefits: no client-side scheduler, versioned agent configs, platform-enforced budgets,
  and a Console session viewer for every run.
- Caveat: a scheduled run has no client attached. Any tool call that pauses for approval
  waits until something answers it, via a webhook handler that would need a reachable
  endpoint. Keep approvals out of the loop and in your own outbox (Section 7), which works
  for any option.

### Considered and not recommended as the runtime: Claude Code scheduled cloud tasks / routines

These fire a Claude Code session in a cloud container on a schedule. The minimum interval is
normally hourly. The run checks out the GitHub repo. But `reviews.db` is gitignored
(`.gitignore`), so the run starts with no data and no memory unless the DB is moved to cloud
storage. The cloud container's network policy would need to allow Steam's endpoints and CM
servers. A general coding agent with a shell is also more power than a monitoring job should
have, and its spend per run is less predictable. This is a good fit for maintaining the
project (for example, a weekly "review the agent's false alarms and propose threshold
tweaks" task against an exported snapshot). It is not a good fit for the monitor itself.

### Comparison

| | A. Local tool runner | B. Agent SDK local | C1. Managed, cloud env | C2. Managed, self-hosted | D. Claude Code routines |
|---|---|---|---|---|---|
| Runs where | Your PC (later: any box) | Your PC | Anthropic | Loop: Anthropic; tools: your PC (WSL) | Anthropic cloud container |
| Data stays local | Yes | Yes | No | Yes | No |
| Works with 3.12 + PICS as-is | Yes | Yes | Hard | Yes, inside WSL | Hard |
| New infra | None | Claude Code runtime | Hosted DB | WSL + always-on worker | DB hosting |
| Scheduler | Task Scheduler | Task Scheduler | Platform cron | Platform cron | Platform schedule |
| Spend caps | Yours (code) | Yours (code) | Platform-enforced $ | Platform-enforced $ | Plan/usage limits |
| Build effort to MVP | Lowest | Low-medium | High | Medium-high | Medium, poor fit |
| Lock-in / beta surface | Tool runner (beta helper) | Agent SDK | Several betas | Several betas | Product feature |

### Recommendation

Go with Option A. The project is small, local, and pinned to a fragile Python stack, and A is
the only option that fits it without moving data or adding infrastructure. Keep the agent's
tools as plain Python functions with typed inputs. If you later move to an always-on host or
want hosted runs, budgets, and a session viewer, those same functions become custom tools in
Option C2 with little rework.

Start the LLM step as a bounded tool-runner loop: at most 12 model turns, read-only tools
plus three "record" tools. Also measure it against a no-tools baseline: one structured-output
call over a precomputed evidence bundle. If evals (Section 9) show the single call is just as
good, use it. It is cheaper and more predictable.

---

## 4. The agent's tools

Every tool takes `appid` and explicit windows (unix timestamps), never free-form SQL, and
returns compact JSON. The tool layer sits in a new `agent_tools.py` that wraps existing
functions. The model never imports `db` directly.

### Read-only tools (callable by the model at will)

| Tool | Wraps | Notes |
|---|---|---|
| `list_titles()` | `db.list_products` (`db.py:120`) | |
| `get_releases(appid, since=None)` | `db.list_releases` (`db.py:211`) | Includes PICS `build_*` rows. |
| `get_patch_notes(appid, gid)` | `steam_api.get_news` (`steam_api.py:188`) | Network call, read-only. `maxlength=600` (`steam_api.py:197`) truncates bodies. Make that a parameter. |
| `get_window_stats(appid, ts_from, ts_to, language)` | `db.count_reviews` + an SQL `SUM(voted_up)` | Replaces `analyze._basic_stats` (`analyze.py:241`), which loads every row. |
| `get_daily_sentiment(appid, ts_from, ts_to, language)` | `db.daily_sentiment` (`db.py:335`) | Cap at 120 rows and roll up to weekly beyond that. |
| `get_language_breakdown(appid, ts_from, ts_to)` | `db.language_breakdown` (`db.py:280`) + `_language_eligibility` (`app.py:130`, moved in Phase 0) | |
| `sample_reviews(appid, voted_up, ts_from, ts_to, language, limit<=40)` | `db.sample_reviews` (`db.py:353`) | Needs a variant that returns `recommendationid, language, playtime_at_review, votes_up, timestamp_created, text[:1200]`. The current one returns text only (`db.py:374`), so the model cannot cite. |
| `get_cached_analysis(appid, scope, language)` | `db.get_cached_analysis` (`db.py:219`) | Returns themes and summaries if cached. Never generates. |
| `get_signal_history(appid, detector=None, days=30)` | new `agent_signals` table | So the model knows what was already reported. |
| `get_collection_health(appid)` | `db.last_collected` (`db.py:312`) + `runs` | Needs the new status/error columns. |
| `get_theme_trend(appid, theme_id, days)` | new `theme_mentions` table (Phase 2) | |
| `get_notes(appid)` | new `agent_notes` table | Owner-supplied context, for example "known crash on AMD, fix in 1.4". |

### Side-effecting tools

Tier 0 (internal; the model may call them, and each is logged and capped):

| Tool | Wraps | Side effect / cap |
|---|---|---|
| `record_finding(appid, severity, title, body_md, evidence)` | new `agent_findings` | Writes to the dashboard feed. Idempotent on (run_id, signal fingerprint). |
| `request_notification(finding_id, channel)` | new `outbox` | Queues a message. Owner-only channels are auto-sent by the dispatcher (Tier 1). Team or public channels are created as `draft` and need approval (Tier 2). The model cannot pick arbitrary URLs. Channels are named keys from config. |
| `schedule_followup(appid, reason, after_hours<=72)` | new `agent_state` | Asks the next ticks to re-check. At most 3 open per title. |
| `refresh_analysis(appid, scope, language)` | `analyze.get_analysis` (`analyze.py:249`) | Spends tokens (Haiku themes, Opus summaries) and writes `analyses`. Allowed only for primary views (all-time and latest release, matching `app.py:273`), at most 2 per tick, and only if the budget allows. |

Not exposed to the model; the orchestrator calls these deterministically:

| Function | Side effect | Why not a model tool |
|---|---|---|
| `collect.collect_all(...)` (refactor of `collect.main`, `collect.py:201`) | Steam HTTP, writes `reviews`, `runs` | Rate-limited I/O. The orchestrator owns cadence. |
| `collect.refresh_releases` (`collect.py:123`) | Writes and deletes `releases` rows (prune at `collect.py:157-161`) | Destructive prune. Keep it deterministic. |
| `collect.record_public_build` (`collect.py:78`) | PICS login, writes `releases` | gevent; batch it. |
| Notification dispatch | Outbound HTTP/SMTP | Sends only from the outbox, after policy checks. |
| `db.upsert_release(..., is_major=...)` (`db.py:187`) | Changes graph callouts | User-curated flag. The agent can suggest in a finding, not set. |

Tool definitions should use `strict: true` schemas. Opus 5.5 rejects forced `tool_choice`
(`any`/`tool`) with a 400, so use `auto` and steer from the prompt.

---

## 5. Triggers and decision logic

### 5.1 What wakes it

- Task Scheduler runs `agent.py tick` hourly (jittered minute, `StartWhenAvailable`, `RunOnlyIfNetworkAvailable` like `register-task.ps1:50`).
- A single-instance lock file means a tick that overlaps the previous one (or a manual `collect.py`) exits immediately.
- `agent.py tick --force-collect` and `agent.py digest` for manual use.
- Kill switch: `AGENT_ENABLED=0` in `.env`, or a row in `agent_state`. Shadow mode: `AGENT_MODE=shadow` runs everything but never dispatches.

### 5.2 Each tick

1. Preflight: take the lock, load state, check the budget. If today's or this month's spend ceiling is hit, run without LLM steps.
2. Cheap probes per title, 2-3 requests each:
   - PICS public build id. One anonymous login for all appids, since `get_product_info(apps=[...])` takes a list (`steam_api.py:248`).
   - Latest news gid (`steam_api.get_news(count=5)`).
   - Review totals from the first page of the review endpoint's `query_summary` (one request with `language=all`). Add `steam_api.get_review_summary`. Confirm the exact parameters Steam returns `query_summary` for.
3. Collection decision. Collect a title if any of these is true:
   - The daily baseline is due (24h since last successful collect, default noon as today).
   - The PICS build id changed, or a new `patchnotes` gid appeared. This opens a patch watch for that title: 72h, collecting every 3h.
   - The title is in a patch watch and 3h have passed since its last collect.
   - The `query_summary` total grew by at least `max(50, 2% of the trailing-7-day weekly volume)` since the last collect.
   - A `schedule_followup` is due.
   Otherwise skip. Most ticks end here.
4. Collect with `collect_all(appids=..., max_pages_per_language=50)`. First-time backfills (where `last_seen_ts == 0`) run only on the daily-baseline tick, never during a watch.
5. Run the detectors (5.3) on titles that got new data. Upsert `agent_signals` by fingerprint.
6. Analysis decision. Pre-warm the all-time and latest-release analyses only when a primary view crossed `REFRESH_MIN_NEW` (`analyze.py:63`) and a signal is open for that title. Otherwise leave generation lazy, as today.
7. Investigation. If any signal is new, or its severity went up, run the LLM investigator once per title with all of that title's open signals. Skip it in degraded mode (no API key, budget hit, API down). In that case write a plain templated finding from the detector metrics so alerts still go out.
8. Dispatch the outbox (5.5 and Section 6).
9. Write the `agent_runs` row, release the lock.

Digest: on Mondays at the first tick after 09:00 local, the investigator writes a weekly
digest per title, even with no signals.

### 5.3 Detectors and default thresholds

All of these are SQL over `reviews` and `releases`, and all thresholds are env-tunable. They
use `timestamp_created` windows. Each detector also records `n`, the windows, and the metric
values for the evidence.

| Detector | Compares | Fires (feed only) | Escalates (notify) |
|---|---|---|---|
| `post_release_shift` | % positive since the latest release/build vs. the 14 days before it (same languages) | n_post >= 50, abs delta >= 5 pts, two-proportion z >= 2 | n_post >= 100, abs delta >= 8 pts, z >= 3 |
| `trend_shift` (no release) | Last 7d vs. prior 28d | n_7d >= 100, abs delta >= 6 pts, z >= 2.5 | abs delta >= 10 pts, z >= 3.5 |
| `review_bomb` | Daily volume and negative share vs. trailing-28-day median | volume >= max(3x median, 30) and negative share >= baseline + 20 pts | Same, plus at least 2 markers: median `playtime_at_review` < 120 min; non-purchase share > 2x baseline; one language > 60% of the spike; median text length < 80 chars; (Phase 2) `author_num_reviews <= 1` share > 2x baseline |
| `language_divergence` | Per language: 14-day delta vs. blended, minus that language's 90-day delta | n_lang_14d >= 50 and change in divergence >= 8 pts (`LANG_MIN_DIVERGENCE`, `app.py:41`) | >= 12 pts and n >= 100 |
| `emerging_theme` (Phase 2) | Share of last-7d negative reviews tagged with theme T vs. prior 28d | Share up >= 10 pts and >= 15 mentions | New theme (not in the catalog) reaches top 3, or share up >= 20 pts |
| `data_health` | Collection runs | Language errors on 2 consecutive collects; zero new reviews in 72h where the trailing average is >= 5/day | PICS returns None for every title on 3 consecutive ticks (venv or `steam` broken); no successful collect in 48h |

Notes:

- `language_divergence` measures a change in divergence, not divergence itself. Chinese
  often diverges permanently, and the dashboard already shows that.
- The sample-size floors follow the reasoning in `analyze.py:70-74`: about plus or minus 10
  points at n=100. Below them, a detector only writes to the feed and never notifies.
- A positive spike is reported too (for example, a fix landing). It gets the same detector
  and a lower severity.
- Every signal has a fingerprint: `detector|appid|scope_key|language|window_bucket`, where
  `scope_key` is the release gid or ISO week. Re-detection updates `last_seen_at` and the
  metrics. A signal only re-notifies if its severity goes up. Signals auto-resolve when the
  metric falls back under the fire threshold for 48h.

### 5.4 What the LLM investigator decides

Input: the open signals with their metrics, the title's release timeline (via
`_scope_label`-style text, `app.py:71-114`, so it does not mistake a patch for a launch),
recent findings, and owner notes. Tools: Section 4.

It must output, through `record_finding`:

- Severity: `info` (feed only), `notable` (notify the owner), or `urgent` (notify the owner now, and offer a Tier-2 draft).
- What changed, with numbers taken from tool output.
- Likely cause (a linked release or build, with patch-note lines if relevant).
- 3-5 quoted reviews with IDs and language, translated into English as `analyze.py` already does.
- Suggested next steps for product and QA (reuse the two personas, `analyze.py:185-196`).
- Confidence and caveats.

It may downgrade a detector's escalation. For example, a review bomb about an off-topic
controversy matters to marketing, not QA. It may not upgrade past what the detector allowed
without stating its reason in the finding.

### 5.5 Notification policy

- Owner channels (Tier 1): auto-send for `notable`/`urgent`. At most 1 message per (title, detector) per 24h, and at most 6 messages per day in total. Anything over the limit is folded into the next digest.
- Team or public channels (Tier 2): always a `draft` in the outbox. The owner approves in the dashboard or with `agent.py approve <id>`. Drafts expire after 72h.

---

## 6. Outputs and channels

| Channel | Use | Setup | Notes |
|---|---|---|---|
| Dashboard "Agent feed" | Every finding, signal, digest, outbox draft, and run status | New `/api/agent/feed`, `/api/agent/outbox/<id>/approve`, `/api/agent/feedback`. New panel under the KPI strip (`templates/index.html:176-181`) | Source of truth. Approve/reject buttons and Useful/Noise feedback. Works only while `app.py` runs, which is fine because the agent does not depend on it. |
| Discord webhook | Owner alerts (recommended default) | One webhook URL in `.env` | Simplest option. Send with `allowed_mentions: {"parse": []}` so review text can never ping `@everyone`. Keep under Discord's content limit by linking to the dashboard for detail. Honor 429 `retry_after`. |
| Slack incoming webhook | Owner or team alerts | Webhook URL per channel | Same pattern. Interactive buttons would need a public endpoint, so approvals stay in the dashboard. |
| Email (SMTP) | Weekly digest, and alerts if preferred | SMTP host/user/app password in `.env` | Good for digests. Markdown-to-HTML for the body. |
| Windows toast (optional) | Local nudge when the PC is in use | None | Optional nice-to-have. |

Message shape for owner alerts: one-line headline (title, detector, delta, n), 3 bullets,
and a link to `http://127.0.0.1:5000/?appid=...&scope=...`. Full detail stays in the feed.

---

## 7. Guardrails

### Cost

- Price table in config, keyed by exact model ID. `models.py` may resolve a newer model at
  runtime. If the resolved ID has no known price, the budget guard fails closed: no LLM
  calls, and a `data_health` finding is written. This stops a silent price change from
  going unnoticed.
- `llm_usage` row for every call: purpose, model, input, output, cache-read tokens, and
  estimated cost. Instrument `analyze.py`'s three call sites too (`analyze.py:110`, `150`,
  `214`), so dashboard-triggered spend counts against the same ceilings.
- Ceilings (defaults, all env-tunable): $1.50/day and $20/month for agent work. Per-run caps:
  12 tool-runner turns, `max_tokens` 8000 per turn, 3 investigations per tick, 2
  `refresh_analysis` per tick. Consider Opus 5.5's beta `task_budget` (advisory, minimum
  20K tokens) on the investigator so it paces itself, but the hard counters stay the
  enforcement.
- Effort: Opus 5.5 cannot disable thinking, and its default effort is `medium`. Set it
  explicitly per role in `models.py`. Start the investigator at `low`, raise it to `medium`
  only if evals show a gain, and use `medium` for the digest.
- Prompt caching: keep the system prompt and tool definitions static and first, and put
  signals in the user message. Then repeated investigations in one tick reuse the cache.
  Verify with `usage.cache_read_input_tokens`.
- Rough expectations, to be checked against `llm_usage`:
  - Haiku theme tagging: about $0.01 per title per day.
  - One Opus investigation: about $0.25-$1.00.
  - Weekly digest: about $0.20-$0.50 per title.
  - Quiet month with 3 titles: a few dollars. Busy month: under the $20 ceiling.
- Non-urgent bulk work (the nightly theme tagging backfill) can go through the Message
  Batches API at half price. Never use it for patch-watch work.

### Steam rate limits

- A token bucket in `steam_api._get` (`steam_api.py:95`): a global 1 req/s sustained and a
  burst of 5, shared by every caller. Today's pacing is `page_pause=1.0` (`steam_api.py:275`)
  per stream only.
- Circuit breaker: after 3 consecutive 429s (`steam_api.py:106`), abort Steam work for the
  rest of the tick and record `data_health`.
- Per-tick caps: 50 pages per language per title, 600 requests per tick.
- PICS: one CM login per tick for all titles.

### Idempotency and concurrency

- Lock file per tick.
- Review upserts are already idempotent (`db.py:138`), and so are analysis saves (`db.py:230`).
- Signals are unique on fingerprint. Outbox rows are unique on
  `idempotency_key = finding_id|channel`. The dispatcher marks `sending` and then `sent`
  with attempts and the last error. A crash between send and mark can produce at most one
  duplicate, which is acceptable for owner alerts.
- Never hold a write transaction across an LLM call. `analyze.py:311` already follows this
  rule; keep it for agent writes.

### Untrusted input (prompt injection)

Review text is written by strangers and goes into the model's context.

- Wrap sampled reviews in clearly delimited data blocks, and tell the system prompt they are data, not instructions.
- The model has no tool that sends to an arbitrary destination. Channels are config keys.
  Tier-2 sends need a human.
- Outbound text is post-processed: strip mentions, cap length, and allow only links to the local dashboard and Steam store/news URLs.

### Human approval

- Tier 2 (anything seen by someone other than the owner): draft, approve, send. No exceptions in the MVP.
- Changing curated data (`releases.is_major`, deleting rows) is never automatic. The agent proposes it in a finding.

### Model API robustness

- Handle `stop_reason == "refusal"` (Opus 5.5 has safety classifiers, and reviews can be
  profane or violent). Enable the server-side `fallbacks` parameter on Opus requests, as
  the `claude-api` reference recommends. If the whole chain refuses, write a templated
  finding instead.
- Use the SDK's typed exceptions. Retry only on 429, 5xx, and connection errors. Anything
  else goes into degraded mode for the tick.

---

## 8. State, memory, and schema changes

`db.SCHEMA` uses `CREATE TABLE IF NOT EXISTS` (`db.py:22-85`). That handles new tables but
cannot add columns. Add a small migration step in `db.connect` (`db.py:88-103`) driven by
`PRAGMA user_version`, with ordered `ALTER TABLE ... ADD COLUMN` statements.

Column additions:

- `runs`: `run_id TEXT`, `status TEXT` (`ok`/`error`/`partial`), `error TEXT`, `trigger TEXT`
  (`daily`/`patch_watch`/`followup`/`manual`), `pages INTEGER`. Write a row on the error
  path too (`collect.py:190-193` currently skips it).
- `reviews`: `author_num_reviews INTEGER`, `author_num_games_owned INTEGER`,
  `received_for_free INTEGER`. Capture them in `upsert_review` (`db.py:149`). They are
  backfilled only as reviews are re-seen, so detectors must treat NULL as unknown.

New tables (sketch):

```sql
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY, started_at INTEGER, finished_at INTEGER,
  mode TEXT,            -- live | shadow | degraded
  collected_json TEXT,  -- per title: new reviews, errors, builds seen
  signals_new INTEGER, signals_updated INTEGER, findings INTEGER,
  cost_usd_est REAL, status TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS agent_signals (
  id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE, appid INTEGER, detector TEXT,
  scope_key TEXT, language TEXT, window_from INTEGER, window_to INTEGER,
  severity TEXT, metrics_json TEXT, status TEXT,   -- open | resolved | suppressed
  first_seen_at INTEGER, last_seen_at INTEGER, last_notified_severity TEXT
);
CREATE TABLE IF NOT EXISTS agent_findings (
  id INTEGER PRIMARY KEY, run_id TEXT, appid INTEGER, kind TEXT,  -- alert | digest | health
  severity TEXT, title TEXT, body_md TEXT, evidence_json TEXT, signal_ids TEXT,
  model TEXT, created_at INTEGER, feedback TEXT, feedback_at INTEGER
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY, finding_id INTEGER, channel TEXT, tier INTEGER,
  payload TEXT, status TEXT,  -- draft | approved | sending | sent | failed | rejected | expired
  idempotency_key TEXT UNIQUE, created_at INTEGER, approved_at INTEGER,
  sent_at INTEGER, attempts INTEGER DEFAULT 0, last_error TEXT
);
CREATE TABLE IF NOT EXISTS agent_state (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER);
  -- per-title cadence/watch_until, last build id, followups, daily/monthly spend, kill switch
CREATE TABLE IF NOT EXISTS llm_usage (
  ts INTEGER, run_id TEXT, purpose TEXT, model TEXT, input_tokens INTEGER,
  output_tokens INTEGER, cache_read_tokens INTEGER, cost_usd_est REAL
);
CREATE TABLE IF NOT EXISTS agent_notes (
  appid INTEGER, key TEXT, text TEXT, source TEXT, updated_at INTEGER,
  PRIMARY KEY (appid, key)
);  -- owner context + "already reported" memory; capped at ~20 rows per title
-- Phase 2:
CREATE TABLE IF NOT EXISTS theme_catalog (
  appid INTEGER, theme_id TEXT, label TEXT, description TEXT, polarity TEXT,
  created_at INTEGER, retired_at INTEGER, PRIMARY KEY (appid, theme_id)
);
CREATE TABLE IF NOT EXISTS theme_mentions (
  recommendationid TEXT, appid INTEGER, theme_id TEXT, model TEXT, tagged_at INTEGER,
  PRIMARY KEY (recommendationid, theme_id)
);
```

"Memory" is deliberately structured and small: open and resolved signals, past findings, and
owner notes. Nothing free-form accumulates in a prompt. The investigator reads only the last
30 days of findings for its title.

---

## 9. Observability and evals

### Observability

- `agent_runs`, `agent_signals`, `agent_findings`, `outbox`, and `llm_usage` together form the audit trail.
- Add an "Agent" tab or panel to the dashboard with: the last 50 runs (status, duration,
  titles collected, cost), open signals, the spend chart for today and this month against
  the ceilings, and the outbox.
- `agent.log` alongside `collect.log`, plus a per-run JSONL trace of tool calls with inputs
  and truncated outputs, so a bad finding can be replayed.
- Heartbeat: if no successful tick in 6h (checked by the next tick, or by the dashboard on
  load), show a banner. A missing heartbeat is the most likely failure on a sleeping PC.

### Evals (how to tell it is making good calls)

1. Detector backtest (deterministic, free). Reviews carry `timestamp_created`, so you can
   replay "as of day D" for every day in history. Run the detectors and list what would have
   fired. The owner labels past releases and episodes (real shift / noise / bomb). Measure
   precision and recall per detector, then tune thresholds to roughly at most 1 notify-level
   false alarm per title per month. Put this in `tests/` with a fixture DB, and run it in CI.
2. Investigator eval (costs money; get approval before running). Freeze 20-40 scenarios
   (signal bundle plus DB snapshot) from the backtest, including null cases where the right
   answer is `info`. Grade with code first:
   - severity class matches the label;
   - every quoted review ID exists and is inside the window;
   - every number in the finding appears in that run's tool outputs (to rounding);
   - no claim of launch framing for a mid-life patch.
   Then use a rubric-based model judge for usefulness and clarity. Compare tool-runner and
   single-call variants, and effort levels, on the same set. The `claude-api` skill's
   `build-eval` flow covers this setup.
3. Online feedback. Useful/Noise buttons on each feed item. Track the useful rate per
   detector, and review it weekly in the digest. A detector under 50% useful over 4 weeks
   gets its thresholds raised.
4. Shadow period. The MVP runs at least 2 weeks with `AGENT_MODE=shadow` (feed only, no
   sends) before any notification goes out.

---

## 10. Phased rollout

Effort estimates assume one developer who knows this codebase, working in focused days.

### Phase 0: refactors needed first (about 2-4 days)

1. `config.py`: a single `db_path()` and `.env` loading, replacing the five copies (`collect.py:41`, `app.py:44`, `clear_cache.py`, `export.py`, `setup.py`).
2. `collect.py`: add `collect_all(conn, appids=None, *, max_pages=None, trigger="daily") -> CollectResult`.
   - It returns per-title/per-language new counts, errors, and builds/releases added.
   - Write `runs` rows with status and error, including on failure.
   - Keep `main()` as a thin wrapper so Task Scheduler and README commands still work.
   - `record_public_build` returns the build info, not a bool.
   - Add `--json` output for subprocess use.
3. `steam_api.py`:
   - global rate limiter and 429 circuit breaker in `_get`;
   - `get_review_summary`;
   - batched `get_public_build_ids(appids)` (one login);
   - `maxlength` parameter on `get_news`.
4. Move `_resolve_window`, `_scope_label`, `_language_eligibility` from `app.py` into `scopes.py` (no Flask import). `app.py` imports them.
5. `analyze.py`:
   - SQL-aggregate `_basic_stats`;
   - accept an injected client;
   - return or log usage for every call;
   - done in this PR: `models.py` replaced `EXTRACT_MODEL`/`SUMMARY_MODEL`/`_RICH_PARAM_MODELS` in `analyze.py`;
   - check that the `messages.parse(..., output_format=...)` calls (`analyze.py:110-132`) still match the pinned SDK, because the `claude-api` reference marks `output_format` as deprecated in favor of `output_config.format`.
6. `db.py`: the `user_version` migration mechanism, the `runs`/`reviews` columns, and the new tables.
7. Pin `anthropic` to a version that has the beta tool runner and whatever `models.py` needs. Smoke-test on the 3.12 venv with `steam` imported in the same process.
8. Fix the drift: `LANG_MIN_VOLUME` default (`app.py:40` vs `.env.example`), and the README model defaults (`README.md:152-159`).
9. `tests/` with a synthetic fixture DB and `pytest`.

Exit criteria: `collect.py` behaves exactly as before; the dashboard is unchanged; tests pass.

### Phase 1: MVP agent (about 5-8 days)

- `agent.py` with `tick`, `digest`, `approve`, `status`.
- Lock, kill switch, and shadow mode.
- Probes, collection decision, patch watch.
- Detectors: `post_release_shift`, `trend_shift`, `review_bomb` (markers from existing columns only), `language_divergence`, `data_health`.
- Investigator via tool runner with read-only tools plus `record_finding`, `request_notification`, and `schedule_followup`.
- Degraded templated findings.
- Budget guard and `llm_usage`.
- Outbox with Tier 1 auto-send to one owner channel (Discord or Slack webhook) and an SMTP weekly digest.
- Dashboard agent feed panel with feedback buttons.
- `register-agent-task.ps1` (hourly).
- Detector backtest harness.
- Two weeks in shadow mode, then turn on Tier 1.

### Phase 2: extensions (about 5-8 days)

- Theme catalog and Haiku tagging of new negative reviews, giving `emerging_theme`. Seed the catalog from the existing `themes_bad` cache entries.
- Updated-review sweep: periodically walk `filter=updated` (confirm the parameter) back to the last sweep. This catches players who flipped their review after a patch, which the `filter=recent` walk misses.
- Author markers in `review_bomb`.
- Tier 2 drafts to a team channel with dashboard approval.
- `refresh_analysis` tool, so the agent pre-warms primary views when it alerts.
- Investigator eval set and an effort/variant comparison.
- Owner notes UI.

### Phase 3: optional (about 3-10 days depending on choices)

- Move to an always-on host: a small Linux box or VPS with a 3.12 venv and cron. Nothing in Option A is Windows-specific apart from the `.ps1` registration.
- Or move the investigator to Managed Agents (Option C2), with the same tool functions as custom tools, if hosted budgets and session history are worth WSL plus a worker.
- Ticket drafting (GitHub/Jira/Linear) via the approval outbox.
- A Claude Code scheduled task that reviews a weekly export of findings and feedback and proposes threshold changes as a PR. This is maintenance, not runtime.

---

## 11. Open questions for you

1. Who should hear from the agent: just you, or a team? Which channel (Discord, Slack, email)? Is a team channel "outward-facing" (approval needed) or not?
2. Is the Windows PC on most of the time? Is patch-day latency of "next time the PC wakes" acceptable, or do you want an always-on host sooner?
3. What monthly LLM spend ceiling are you comfortable with? Is the $20/month default about right?
4. Are the tracked titles your own games or competitors'? For your own games, do you have Steamworks partner access? That would open sales, wishlist, and developer-response data later.
5. How many false alarms per month would make you start ignoring it? This sets the thresholds.
6. Can you label a handful of past releases or episodes (good patch, bad patch, review bomb, noise) for the backtest?
7. Which languages or markets matter for divergence alerts: all collected, or a short list?
8. Should the agent ever change curated data, such as marking a PICS build as minor, or only suggest it?
9. Keep the existing daily noon collect task alongside the agent, or let the agent's tick replace it?
10. Is the hourly cadence fine, and what quiet hours, if any, should suppress notifications (they would go to the digest instead)?
