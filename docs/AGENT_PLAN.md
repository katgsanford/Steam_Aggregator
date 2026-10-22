# Plan: a daily cloud agent that emails a Steam review report

Status: proposal. No code has changed. Written 2026-10-22 against commit `b61f323`.
Section 5 (build ids) was tested on 2026-10-22 and replaces an earlier news-feed fallback.
This replaces the earlier plan, which recommended a local Windows Task Scheduler agent.

The decisions this plan is built on:

- It runs in the cloud, not on your PC.
- It runs once per day.
- It emails a daily report.
- The runtime is **Claude Managed Agents with a scheduled deployment**.

Plain, deterministic Python does the collection, change detection, report rendering and
the email send. Claude is used for the judgment part: a short daily narrative (what
changed, why it matters, what to watch), through the Messages API and `models.py`.

Model IDs, prices and Managed Agents behavior below come from the Anthropic `claude-api`
reference (model table cached 2026-10-06). Managed Agents is a beta API. Re-check the
details marked "verify" before building.

---

## 1. Summary

| Question | Answer |
|---|---|
| Where it runs | A Managed Agents cloud sandbox, started by a scheduled deployment once per day (cron + IANA time zone). |
| What the agent does | Runs one command from this repo (`python -m daily_job run`), reads a one-line status, retries once or sends a failure email. It never reads review text. |
| Where data lives | Supabase Postgres. The sandbox is thrown away after each run. |
| How the job reaches the DB | A Python client in the sandbox calls Supabase over HTTPS (PostgREST and SQL functions). The key sits in a Managed Agents vault and is added to request headers at egress. The sandbox never sees the real key. |
| Patch detection | Build ids only: the public-branch `buildid` from Steam PICS, read over WebSocket on port 443 (tested, Section 5). Ranked fallbacks: `api.steamcmd.net` over HTTPS, then a GitHub Actions PICS job. The news feed is never a release signal. If every source fails, the report says "build id unavailable" and makes no claim about updates. |
| Email | Resend HTTPS API, called from Python. The API key and the recipient (`REPORT_TO`) are vault secrets. |
| Dashboard | Still runs locally (`python app.py`). It reads the cloud DB directly when `DATABASE_URL` points at Supabase, or a local SQLite copy made by a sync script. |
| Cost | About $0.10-$0.30 per day in Anthropic usage, so about $4-$9 per month. Supabase and Resend free tiers cover the rest. See Section 11. |

### Rejected alternatives

A GitHub Actions cron job would be cheaper and has unrestricted network egress, so PICS
would work over raw TCP. It is kept only as the last build-id fallback (Section 5). A Claude Code
scheduled cloud task would spend subscription tokens on a general coding agent, give
less predictable runs, and face the same state and network problems. Neither is the
main runtime.

---

## 2. Architecture

```
                         Anthropic (Managed Agents, beta)
  +--------------------------------------------------------------------+
  |  Scheduled deployment  "steam-daily"                               |
  |  cron "17 7 * * *", tz from config, budget $0.75 per session       |
  |        |                                                           |
  |        v  fires once per day                                       |
  |  Session (agent loop on Anthropic)  ---- bash ---->  Cloud sandbox  |
  |  model claude-opus-5-5, effort low                  (thrown away   |
  |  tools: bash, read only                              after run)    |
  |                                                     /workspace/    |
  |  Vault: ANTHROPIC_API_KEY, SUPABASE_KEY,              Steam_Aggregator
  |         RESEND_API_KEY, REPORT_TO                    (cloned, read-only PAT)
  |  (substituted into HTTP headers/body at egress)                    |
  +---------------------------------------------------|----------------+
                                                      | python -m daily_job run
            +-----------------------+-----------------+-----------------+
            |                       |                 |                 |
            v                       v                 v                 v
   store.steampowered.com    <ref>.supabase.co   api.anthropic.com   api.resend.com
   api.steampowered.com      (PostgREST + RPC)   (narrative call,    (send email to
   (reviews, news, details)        |              models.py)          REPORT_TO)
                                   |
                                   v
                     Supabase Postgres (system of record)
                     reviews, products, releases, runs, analyses,
                     job_runs, daily_snapshots, signals, reports, llm_usage
                        ^                         |
                        |                         | pg_cron watchdog at send time + 3h:
                        |                         | no "emailed" row today -> failure email
  Your PC:  python app.py  (DATABASE_URL=https://<ref>.supabase.co)
            python sync_down.py  (optional local SQLite copy)
            python migrate_to_supabase.py  (one-time upload of reviews.db)
```

Why this shape:

- **Custom tools are not used.** A scheduled run has no client attached. A custom tool
  call would wait for an orchestrator that is not running. Everything the agent needs runs
  inside the sandbox through `bash`.
- **The agent is a thin, hosted runner.** The schedule, the sandbox, the run records and the
  hard dollar cap are what Managed Agents adds here. The work itself is deterministic code.
- **Review text never enters the agent's context.** It goes only to a tool-less Messages API
  call with structured output (Section 9).

---

## 3. How the daily schedule works on Managed Agents

You create four objects once, ideally as version-controlled files under `agents/steam-daily/`
synced with `ant apply`. The `ant` CLI stores the IDs in `claude-lock.json`.

1. **Environment** (`config.type: "cloud"`):
   - `networking.type: "limited"` with `allow_package_managers: true` (PyPI) and
     `allowed_hosts`: `store.steampowered.com`, `api.steampowered.com`, `*.steamserver.net`
     (Steam CM WebSocket servers, port 443), `api.steamcmd.net` (build-id fallback),
     `<ref>.supabase.co`, `api.anthropic.com`, `api.resend.com`. Add the GitHub release hosts
     only if the spike shows the sandbox needs `uv` to install Python 3.12.
   - No `unrestricted` networking unless the Phase 0 spike shows that `limited` cannot pass the
     CM WebSocket upgrade, and you accept that trade.
2. **Vault** with `environment_variable` credentials. The sandbox sees only an opaque
   placeholder. The real value is substituted into outbound HTTP requests to the listed hosts.

   | `secret_name` | Allowed host | `injection_location` |
   |---|---|---|
   | `ANTHROPIC_API_KEY` (a job-only key) | `api.anthropic.com` | header |
   | `SUPABASE_SERVICE_KEY` | `<ref>.supabase.co` | header |
   | `RESEND_API_KEY` | `api.resend.com` | header |
   | `REPORT_TO` | `api.resend.com` | body |

   Substitution covers HTTP headers and body only. It does not work for SMTP or the
   Postgres wire protocol. This is the main reason the job talks to Supabase and Resend over
   HTTPS.
3. **Agent**: `model: claude-opus-5-5`, effort `low` (Opus 5.5 defaults to `medium`, so set it).
   Tools: the agent toolset with `bash` and `read` on, `write`/`edit` off, `web_search` and
   `web_fetch` off. Permission policy `always_allow` for `bash`. An `always_ask` or an
   undetermined `auto` call would stall the run, because no client is attached to answer it.
   The system prompt is the runbook in Section 6.4. Claude Haiku 5.5 would cut the agent's
   token cost by about 90%. It is a reasonable switch once runs are stable (your call).
4. **Deployment**:
   - `agent`, `environment_id`, `vault_ids`.
   - Resource `github_repository` for this repo, with a fine-grained PAT that has
     `Contents: Read` only. The token never enters the container. Pin `checkout` to a
     branch such as `agent-stable`, so a push to `main` does not change tonight's run.
   - `initial_events`: one `user.message`, "Run today's Steam report per the runbook."
   - `schedule`: `{type: "cron", expression: "17 7 * * *", timezone: "<your IANA zone>"}`.
   - `budget`: `{type: "limit", max_list_cost: {amount: "75", currency: "USD"}}` ($0.75).

   The `claude-api` reference defaults a report-producing run to `user.define_outcome`
   with a grader. This plan uses `user.message` instead. The report is built and checked by
   code, and a grader would add tokens and would read untrusted review quotes.

Schedule behavior to know:

- Execution is jittered: up to 15% of the interval, capped at 9 minutes. A daily run can
  start up to 9 minutes late.
- DST uses wall-clock matching. Times that do not exist on a spring-forward day are
  skipped, and times that repeat on a fall-back day fire twice. Keep the schedule outside
  01:00-03:00 local. The job's idempotency (Section 6.3) absorbs a double fire anyway.
- If a firing is rate-limited, it is recorded as a failed run and not retried. A paused
  deployment does not backfill missed days. The watchdog (Section 9.2) catches both.
- Every firing writes a `deployment_run` record (`drun_...`). Use
  `client.beta.deployment_runs.list(deployment_id=..., has_error=True)` to audit failures.
- `client.beta.deployments.run(id)` fires a manual run. It also works while the deployment
  is paused. Use it for testing.

---

## 4. Where state lives

The sandbox is not persistent, so state must live in a hosted store. Recommendation:
**Supabase Postgres**, reached from Python in the sandbox over HTTPS.

### 4.1 How the job reaches Supabase

| Option | Verdict |
|---|---|
| Python `psycopg` over TCP 5432/6543 with `DATABASE_URL` | Not for the sandbox. Vault substitution does not cover the Postgres protocol, so the password would have to sit in plain text in the sandbox. Raw TCP egress from the sandbox is also undocumented. Use this path from your PC only (migration, ad-hoc SQL). |
| Supabase MCP server through the vault | No. Every row would pass through the model's context. Upserting thousands of reviews that way is slow and costly, and it hands the model arbitrary SQL. |
| A custom tool answered by your own service | No. It needs an always-on orchestrator, because a scheduled run has no client. |
| **Python HTTPS client to PostgREST, with the key in a vault header** | **Yes.** It is the one path the documented vault mechanism supports. Bulk writes and all aggregates go through Postgres functions called as `POST /rest/v1/rpc/<fn>`. |

Hardening for later (Phase 2): replace the service-role key with a JWT for a dedicated
Postgres role (`steam_job`) that is allowed to execute only these functions. Enable row-level
security on every table with no policies, so the anon key reads nothing.

Free-tier facts to verify at signup: 500 MB database, no automatic daily backups, and the
project pauses after a week without activity. The daily run keeps it active.

### 4.2 The backend switch in `db.py`

Goal: local use keeps working exactly as today, with no new settings.

- `DATABASE_URL` unset, or `sqlite:///path`: SQLite, as now (`db.connect`, `db.py:88-103`,
  path from `config.json`).
- `DATABASE_URL=https://<ref>.supabase.co` plus `SUPABASE_SERVICE_KEY`: the REST backend.
- (Optional, later) `postgresql://...`: a `psycopg` backend for PC-side tools.

Design:

1. Move today's SQL into `db_sqlite.py` unchanged. Add `db_rest.py` with the same function
   names. `db.py` becomes a facade: `db.connect()` returns a connection object that knows
   its backend, and each `db.xxx(conn, ...)` dispatches on it. Callers do not change.
2. **Precondition: no SQL outside `db.py`.** Today there is raw SQL in:
   - `collect.py:106`, `112-115`, `151-153`, `158-161`, `162` (build dedup, release prune, count);
   - `clear_cache.py:51-57`;
   - `export.py:41-62`.

   Each becomes a named `db` function first (Phase 0).
3. `conn.commit()` calls (`collect.py:189`, `195`; `analyze.py:298`; `app.py:281`) become
   no-ops on the REST backend. Each RPC is its own transaction. Review writes go in
   batches of 500, which matches today's commit cadence (`collect.py:186-189`).
4. `analyze._basic_stats` (`analyze.py:228-233`) loads every review row through
   `db.sentiment_rows` (`db.py:320-332`) just to count them. Over HTTP that would download the
   whole table. Replace it with a `review_stats(appid, languages, ts_from, ts_to)` aggregate
   before switching backends.
5. Language buckets (`db.py:246-269`) stay in Python. The REST backend passes a resolved
   `languages text[]` to each function.

### 4.3 Schema mapping, SQLite to Postgres

| SQLite today | Postgres | Notes |
|---|---|---|
| `INTEGER` timestamps (`timestamp_created`, `fetched_at`, `date`, `run_at`) | `bigint` | Unix seconds, unchanged. |
| `voted_up`, `steam_purchase`, `is_major` as 0/1 `INTEGER` | `smallint` 0/1 | Keep 0/1, not `boolean`, so `SUM(voted_up)` (`db.py:290`, `306`, `346`) works unchanged. |
| `weighted_vote_score REAL` | `double precision` | |
| `analyses.payload TEXT` (JSON) | `text` (later `jsonb`) | Keep `text` for the MVP so `analyze.py` is untouched. |
| `runs` (no key) | add `id bigserial primary key`, `run_id`, `status`, `error`, `trigger`, `pages` | Fixes the missing error rows (Section 12). |
| `PRAGMA journal_mode=WAL`, `busy_timeout` (`db.py:100-101`) | dropped | Postgres MVCC handles concurrent dashboard reads. |
| `executescript(SCHEMA)` on every connect (`db.py:102`) | numbered migration files applied once (`db/postgres/001_init.sql`, ...) | Apply via the Supabase SQL editor, `supabase db push`, or your Supabase connector. |
| `INSERT ... ON CONFLICT(...) DO UPDATE SET x=excluded.x` (`db.py:108-117`, `150-166`, `191-207`, `232-239`) | Same syntax | Wrapped in functions such as `upsert_reviews(p_appid, p_language, p_fetched_at, p_rows jsonb)`. Exact column semantics kept: `db.py:158-165` updates only the mutable columns. |
| `(timestamp_created / 86400) * 86400` (`db.py:346`) | Same | Integer division on `bigint`. |
| `?` placeholders | Function parameters | |
| `idx_reviews_appid_lang` (`db.py:48`) | add `(appid, language, timestamp_created)` | Serves `last_seen_ts` (`db.py:129-135`) and every window query. |
| Full-text search | n/a | Not used today. Nothing to port. |

New tables:

```sql
create table job_runs (                -- one row per report date; the idempotency key
  report_date date primary key,        -- local date in REPORT_TZ
  status text not null,                -- running | collected | reported | emailed | failed
  attempt int not null default 1,
  started_at bigint, heartbeat_at bigint, finished_at bigint,
  session_id text,                     -- Managed Agents session, for tracing
  build_source text,                   -- pics_ws | steamcmd_net | github_job | none
  build_status text,                   -- ok | partial | unavailable
  summary jsonb, error text, email_id text
);
create table daily_snapshots (         -- per title/language/day; makes "since yesterday" cheap
  report_date date, appid int, language text,
  total bigint, positive bigint, new_24h int, positive_24h int,
  pct_7d numeric, n_7d int, pct_28d numeric, n_28d int,
  primary key (report_date, appid, language)
);
create table signals (
  id bigserial primary key, fingerprint text unique, report_date date, appid int,
  detector text, language text, severity text, metrics jsonb,
  first_seen date, last_seen date, status text   -- open | resolved
);
create table reports (
  report_date date primary key, kind text,       -- full | quiet | failure
  subject text, html text, text_body text, narrative jsonb, created_at bigint
);
create table llm_usage (
  id bigserial primary key, ts bigint, report_date date, purpose text, model text,
  input_tokens int, output_tokens int, cache_read_tokens int, cost_usd numeric
);
```

### 4.4 The dashboard

Two supported modes. Both keep Flask on `127.0.0.1` (`app.py:290`), because the REST key
has full database access.

1. **Live cloud DB (recommended).** Put `DATABASE_URL=https://<ref>.supabase.co` and
   `SUPABASE_SERVICE_KEY` in `.env`. `app.py` reads the same data the job writes.
   Analyses generated from the dashboard (`/api/analysis`, `app.py:255-286`) go into the
   shared `analyses` cache, so the job and the dashboard reuse each other's work. The
   chart endpoints already return aggregated daily rows (`db.daily_sentiment`,
   `db.py:335-350`), so HTTP latency stays small.
2. **Local copy.** `python sync_down.py` pulls every table into a local `reviews.db`, using
   `max(fetched_at)` and the newest `generated_at` for incremental sync. Unset
   `DATABASE_URL` to use it. Useful offline and as a free backup on the free tier.

The existing per-view analyses (`analyze.get_analysis`, `analyze.py:236-368`) keep working
in both modes, because they call only `db.*` functions.

---

## 5. Build ids: how the sandbox gets the public-branch build id

The release signal is the `depots.branches.public.buildid` that Steam PICS reports. A new
value means a real patch reached every player. The news feed is not a release signal: it
carries posts that are not patches. News stays only as context (Section 6.1, step 1).

### 5.1 What was tested (2026-10-22)

Tests ran in a Claude Code cloud sandbox. Its network is HTTPS through a proxy, and direct
TCP is blocked. That is similar to the Managed Agents `limited` setup, but it is not the same
product, so the Phase 0 spike must repeat the first two rows there.

| Test | Result |
|---|---|
| Raw TCP to a CM server (`cmp1-lax1.steamserver.net:27017`) | Blocked (timed out). The stock `steam` client (TCP only) cannot work in a sandbox like this. |
| WebSocket upgrade to a CM on port 443 through the proxy | Works. The CM answers `101 Switching Protocols` and replies to a ping. The CM list comes from `ISteamDirectory/GetCMListForConnect` (`cmtype=websockets`) over plain HTTPS. |
| `steam` 1.4.4 (ValvePython) websocket support | None. `CMClient` raises "Only TCP is supported" for any other protocol. |
| `steam` 1.4.4 plus a transport shim (about 100 lines, Python 3.12) | Works. Anonymous login 0.7-0.8 s, PICS for 10 apps 0.5 s. The stock `anonymous_login` and `get_product_info` run unchanged. |
| Node `steam-user` 5.3.0 (`webCompatibilityMode: true`, `httpProxy` set) | Works. Login 1.0 s, PICS for 4 apps in 1.3 s total. Without `webCompatibilityMode` it picks non-443 ports, and those time out. |
| `https://api.steamcmd.net/v1/info/<appid>` | Works. `depots.branches.public` has `buildid` and `timeupdated`. For 10 apps, every build id and timestamp matched the direct PICS read, including an app whose build was pushed 5 hours earlier. One app per request (a comma list returns 422). 25 requests in a row: no throttling. |
| First-party Steam Web API | No endpoint returns a build id. `ISteamApps/UpToDateCheck` returns a `required_version` that is not the build id, and it works for dedicated-server style apps only (3 of 5 test apps failed). `GetAppBuilds` needs a publisher key for your own apps and answers 403 otherwise. |

The shim's design, for whoever writes it into `steam_api.py`:

- Subclass `steam.core.connection.Connection`. Open TLS, send the upgrade to `/cmsocket/`,
  and send each Steam message as one masked binary frame. There is no `VT01` length header
  and no `ChannelEncrypt` handshake, because TLS secures the channel. Set `channel_secured`
  after connect.
- Use `gevent.ssl` and a `gevent.ssl.SSLContext`. The stdlib `ssl` module blocks gevent's
  hub, and login hangs until the CM times out.
- Honor `HTTPS_PROXY` with an HTTP `CONNECT` when it is set.
- Ignore the `ClientCMList` push (it lists TCP addresses). Keep the 443 list from the directory.

### 5.2 Recommendation and fallback chain

| Rank | Source | Needs | Use when |
|---|---|---|---|
| 1 | **PICS over WebSocket, port 443**, through the existing `steam` code plus the shim. First-party data, one login for all titles. | `*.steamserver.net` and `api.steampowered.com` in `allowed_hosts`. Python 3.12 plus `steam`, `gevent`, `protobuf<3.21` (Section 5.3). | Always, first. |
| 2 | **`api.steamcmd.net/v1/info/<appid>`** (HTTPS). Same PICS data, read through a free community mirror. | `api.steamcmd.net` in `allowed_hosts`. No library. | Source 1 fails the whole batch or a single app. Also useful as a cross-check on days when a build changes. |
| 3 | **GitHub Actions PICS job** (Phase 2). Runs the same code on a runner, which has raw TCP and WebSocket, and writes build rows to Supabase. | A workflow, a repo secret for the Supabase key, and about 30 minutes of lead time before the deployment. | Sources 1 and 2 both fail, or Managed Agents cannot pass the WebSocket upgrade at all. |

The job tries the sources in this order for each title and records the one that answered in
`job_runs.build_source`. If all three fail for a title, the title's build status is
`unavailable`. The report says so, and it does not claim that nothing changed.

Why this order:

- Source 1 is Steam's own answer. It needs no third party, and it is the only source that
  does not depend on someone else staying online.
- Source 2 is the cheapest to run (one HTTPS call per title, no gevent). It is third-party, so
  it ranks second. Terms and limits: the site says the service is free "without specific
  restrictions", needs no key, has no SLA, and is run by one maintainer. The code is open
  source (so you could self-host it) and has a public status page. It is not affiliated with Valve.
- Source 3 is the most work. It adds a workflow, a schedule and a second place that can fail.
  Cost: a public repo is free; a private repo uses roughly 30 of the 2,000 free minutes per
  month. This was not tested here, but raw TCP is known to work on GitHub runners.
- Node `steam-user` is the maintained alternative to the Python shim. It is tested and works,
  but adds a second runtime to the sandbox. Switch to it only if the shim breaks.

### 5.3 What is still unverified

- **Managed Agents networking.** The `claude-api` reference defines `unrestricted` as full
  egress except a safety blocklist, and `limited` as a deny-by-default host list (bare
  hostnames and `*.domain` wildcards). It says nothing about raw TCP, other ports or
  WebSocket upgrades. So raw TCP to CM servers is unproven, and the plan does not rely on it.
  Whether the egress proxy forwards a WebSocket upgrade to a wildcard-allowed host is the one
  thing to test in Phase 0.
- **Python 3.12.** The reference does not document the sandbox's Python version. If the image
  is not 3.12, `uv python install 3.12` needs the GitHub release hosts in `allowed_hosts`.
  The `steam` stack installs from PyPI once 3.12 exists (`requirements.txt`). Put it in
  `requirements-cloud.txt`.
- **Two pushes in one day.** PICS reports only the current build. Two builds between two daily
  reads look like one change. The `timeupdated` value is stored, so the report shows when the
  latest build landed.

### 5.4 Risks

| Risk | Effect | Mitigation |
|---|---|---|
| The sandbox egress proxy blocks WebSocket upgrades | Source 1 is dead. | Phase 0 test. Source 2 takes over with no code change. |
| Steam changes the websocket CM protocol or retires the old `steam` package | The shim stops working. | Source 2 covers the gap. Switch to `steam-user` if needed. Pin versions. |
| `api.steamcmd.net` goes down, throttles, or changes its response | Source 2 is dead. | It is a fallback. Source 3 covers a double failure. |
| One tracked app has no public branch, or PICS needs an access token for it | `buildid` is missing. | Report `unavailable` for that title. Never fall back to news. |
| A stale mirror answers with an old build id | A real patch is reported a day late. | The mirror's `timeupdated` must be at least as new as the last stored build. Otherwise treat the answer as stale and try the next source. |
| First run for a title | There is no earlier build to compare with. | Store the current build as the baseline with no "update" claim. |

## 6. The daily run

### 6.1 Steps (`python -m daily_job run`)

`daily_job.py` is a new module. All times are UTC except `report_date`, which is the local
date in `REPORT_TZ`.

| # | Step | Timeout | On failure |
|---|---|---|---|
| 0 | Preflight: check that the vault variables exist; claim `job_runs[report_date]` (see 6.3); load today's LLM spend from `llm_usage`. | 30 s | Exit 1. The agent sends a failure email. |
| 1 | Release anchors per title: launch date from the store API. Patch-note posts from the news feed are fetched only as context (title and link), and they never create a release row. | 60 s per title | Log it, mark the title's release feed `error`, continue. This step is best-effort (`collect.py:217-222`). |
| 2 | Build ids: the Section 5.2 chain. One anonymous login for all appids over WebSocket, run in a subprocess with a hard timeout (gevent can hang), then `api.steamcmd.net` for any title still missing. Each new public build id becomes a `build_<id>` release row. | 90 s total | Set `build_source` and `build_status`, continue. An `unavailable` title is flagged in feed health. |
| 3 | Review collection: a new `collect.collect_all()` returning a structured result. Each language is incremental (`collect.py:165-198`). | 30 min total, 5 min per language | Write a `runs` row with `status='error'`. Continue with the next language. |
| 4 | Snapshot: SQL aggregates into `daily_snapshots` for `report_date`. | 60 s | Exit 1. Without data there is no report. |
| 5 | Detectors (Section 7) write `signals`. | 60 s | Report without signals, flagged in feed health. |
| 6 | Evidence: for each notable signal, sample at most 8 reviews (ID, language, playtime, first 600 characters). | 30 s | Narrative runs without quotes. |
| 7 | Narrative: only when at least one signal is `notable` or higher, or on the weekly day. One Opus call with structured output (6.2). | 180 s, SDK `max_retries=2` | Fall back to the templated text. The report still goes out. |
| 8 | Render HTML and plain text from Jinja templates. Validate: required sections exist, and every number in the narrative appears in the facts bundle. Store the result in `reports`. | 30 s | On a validation failure, drop the narrative and use the template. |
| 9 | Send through Resend with `Idempotency-Key: steam-report-<report_date>` (verify Resend's current idempotency support). Mark `emailed`. | 30 s, 3 tries | Exit 1. The agent retries once. The watchdog covers the rest. |
| 10 | Print one status line of JSON for the agent: counts, statuses, no review text. | | |

Exit codes: `0` ok; `2` sent with warnings (degraded feeds); `3` another run is active or
today was already sent; `1` failed.

### 6.2 The narrative call

- New role in `models.py` `ROLES` (`models.py:45-60`): `"narrative"`, family `claude-opus`,
  pinned `claude-opus-5-5`, effort `medium`, env `STEAM_NARRATIVE_MODEL` /
  `STEAM_NARRATIVE_EFFORT`. `models.request_params` (`models.py:188-201`) already adds
  adaptive thinking and effort.
- `client.messages.parse(...)` with a Pydantic schema:
  `{headline, titles: [{appid, what_changed, why_it_matters, watch}], top_watch_items}`.
  Use `output_config.format` rather than the deprecated `output_format` that
  `analyze.py:114` and `158` still use.
- Input: the facts bundle (snapshots, deltas, signals with n and windows, release
  timeline as `_scope_label`-style text so a patch is not framed as a launch, cached
  themes from `analyses`) plus delimited review excerpts. About 8-15K input tokens.
- `max_tokens` 8000 (adaptive thinking shares the budget). Thinking cannot be turned off
  on Opus 5.5; effort is the control.
- Add the server-side refusal fallback (`betas=["server-side-fallback-2026-07-01"]`,
  `fallbacks="default"`), as the `claude-api` reference recommends for Opus 5.5. Check that
  it combines with structured output in the spike. If the final `stop_reason` is
  `refusal`, use the templated text.
- Log `usage` to `llm_usage`. Do the same for every `analyze.py` call site
  (`analyze.py:92`, `134`, `200`), so dashboard spend counts against the same caps.

### 6.3 Idempotency and reruns on the same day

- `job_runs.report_date` is the key. The claim is one upsert:
  - no row: insert `running`, attempt 1;
  - `emailed`: exit 3, "already sent" (unless `--force-resend`);
  - `running` with `heartbeat_at` under 90 minutes old: exit 3, "another run is active";
  - `failed`, or `running` with a stale heartbeat: take over and increment `attempt`.
- Collection is naturally idempotent. It stops at the newest stored timestamp
  (`steam_api.py:310-314`), and review upserts are keyed on `recommendationid` (`db.py:150-166`).
- Snapshots, signals and reports are upserts keyed on `report_date`, so a rerun overwrites them.
- The Resend idempotency key, plus the `emailed` check, prevents a duplicate email when a
  DST fall-back day fires twice or you trigger a manual run.

### 6.4 The agent's runbook (system prompt, abridged)

```
You run a fixed daily job. Do only this:
1. cd /workspace/Steam_Aggregator && bash ops/daily.sh
   (installs pinned deps if missing, then runs: timeout 2700 python -m daily_job run)
2. Read the last line of output (status JSON).
   exit 0 or 2 or 3: reply with the status line and stop.
   exit 1: if status.retryable is true and this is your first retry, wait 120 s and rerun once.
           Otherwise run: python -m daily_job notify-failure --reason "<status.error>" and stop.
Never open files under reports/ or print review text. Never edit files, install other
packages, or contact hosts other than through these commands.
```

The agent sees only exit codes and status JSON. Expected cost: 3-6 turns.

### 6.5 Steam API failures

- Keep the existing retry and backoff (`steam_api._get`, `steam_api.py:95-114`).
- Add a circuit breaker: after 3 consecutive 429s, stop Steam calls for the rest of the
  run and mark the remaining titles `skipped (rate limited)`.
- Write a `runs` row on the error path too (Section 12). The report's feed-health section
  lists every failed title and language.
- If every title fails collection, send the failure form of the report, not "nothing
  notable". Data from a failed collection must never read as a quiet day.
- First-time backfills are excluded from the daily path. A newly added title is backfilled
  from your PC (`python setup.py` then `python collect.py` with `DATABASE_URL` set), or by
  a manual run with `--backfill <appid>` and no time cap.

---

## 7. Detection (deterministic SQL)

All thresholds are env-tunable. All use `timestamp_created` windows. Each signal stores n,
the windows and its metric values as evidence.

| Detector | Compares | Notable when | Phase |
|---|---|---|---|
| `day_over_day` | Today's snapshot vs yesterday's: new reviews, 7-day % positive | 7-day % moves at least 3 points with n_7d at least 100 | 1 |
| `post_release_shift` | % positive since the latest release vs the 14 days before it | n_post at least 50, change at least 5 points, two-proportion z at least 2 | 1 |
| `review_bomb` | Daily volume and negative share vs the trailing 28-day median | volume at least max(3x median, 30) and negative share at least baseline + 20 points, plus 2 markers: median playtime under 120 min, non-purchase share over 2x baseline, one language over 60% of the spike, median text under 80 characters | 1 (author markers in 2) |
| `language_divergence` | Per language: 14-day gap vs the blend, minus that language's 90-day gap | change at least `LANG_MIN_DIVERGENCE` (8) points with n at least 50 | 1 |
| `emerging_theme` | Share of new negative reviews tagged with a theme, 7 days vs prior 28 | share up at least 10 points with at least 15 mentions | 2 |
| `data_health` | Collection results, build-id source and status, feed freshness | any title or language failed; zero new reviews in 72 h where the average is at least 5 per day | 1 |

Fingerprint `detector|appid|scope|language|week` dedupes repeats. A signal re-notifies only
when its severity rises. Below the sample-size floors (the reasoning at `analyze.py:51-55`),
a signal appears in the report but never drives the headline.

---

## 8. Report contents

### 8.1 Sections (full form)

1. **Headline**: one sentence, the most important change across all titles.
2. **Sentiment by title**: one row per title. New reviews in 24 h, 7-day % positive, change
   vs the previous 7 days, lifetime % positive.
3. **Changes since yesterday**: the `day_over_day` deltas that moved.
4. **Post-release shifts**: latest release or build per title, before vs after, n, z.
5. **Review-bomb flags**: spike size, markers met, top language. "None" when clear.
6. **Language divergence**: languages whose gap vs the blend changed, with n.
7. **Top new complaint and praise themes**: from the 7-day themes (`themes_recent`) in
   Phase 1, from tagged themes with trend arrows in Phase 2. Each has one quoted review ID.
8. **What to watch**: 1-3 items from the narrative.
9. **Data-feed health**: per title, the collection status, release feed status, build-id
   source and status, last successful collect and run duration. Today's LLM spend vs the cap.

Numbers in every section come from SQL. The model writes only the headline, the
per-title "what changed / why it matters / watch" text and the watch list.

### 8.2 Formats

- One Jinja2 template each for HTML and plain text, sent as a `multipart/alternative`
  email by Resend. Jinja2 already ships with Flask.
- HTML is inline-styled for email clients: a single 600 px column, a table per section,
  no images, no remote fonts.
- Model text is inserted escaped. The model never writes HTML.
- Links point to Steam store and news URLs only, plus `http://127.0.0.1:5000/?appid=...`
  for the local dashboard.
- Subject: `[Steam daily] Oct 23: <headline, max 70 chars>`.
- Recipient: `REPORT_TO` from the vault (Section 3). Nothing in the repo names an address.

### 8.3 Example (plain-text part, mock data)

```
Subject: [Steam daily] Oct 23: Hollow Harbor down 9 pts since patch 1.4.2

STEAM REVIEW DAILY - Thu 23 Oct 2026 (data through 07:05 UTC)

HEADLINE
Hollow Harbor's 1.4.2 patch is landing badly: 7-day positive fell from 84% to 75%
(n=412), driven by crash reports on AMD GPUs. Other titles are steady.

SENTIMENT BY TITLE
  Title              New 24h   7d positive   vs prev 7d   Lifetime
  Hollow Harbor        143        75.0%        -9.1 pts     86.2%
  Pinecrest Rally       38        91.3%        +0.4 pts     89.7%

CHANGES SINCE YESTERDAY
  Hollow Harbor: 24h positive 68% (97/143), lowest day since launch week.

POST-RELEASE SHIFTS
  Hollow Harbor, build 26087496, patch 1.4.2 notes linked (2026-10-20): 74.1% after (n=301) vs 85.3% before (n=880),
  -11.2 pts, z=4.6. Notable.

REVIEW-BOMB FLAGS
  None. Volume is 2.1x the 28-day median, but playtime and purchase markers are normal.

LANGUAGE DIVERGENCE
  schinese: gap vs blend widened from -6 to -15 pts over 14 days (n=88).

TOP NEW THEMES (7 days)
  Complaints: crash on launch after 1.4.2 (AMD); save sync lost; new UI hides map.
  Praise: new harbor district; co-op stability (pre-patch players).
  Example: "Crashes to desktop every time since the update, RX 6700" (#178233901).

WHAT TO WATCH
  1. AMD crash reports: if the hotfix ships, watch whether 24h positive recovers above 80%.
  2. schinese gap: check whether the complaints match the crash theme or are translation-related.

WHY IT MATTERS
  Hollow Harbor: the drop is concentrated in post-patch reviews from long-time players
  (median playtime 41 h), so it reads as a regression, not a review bomb.

DATA-FEED HEALTH
  Hollow Harbor   reviews ok (8 langs)   build id ok (PICS websocket)   notes linked
  Pinecrest Rally reviews ok (3 langs)   build id ok (PICS websocket)   no notes
  Run: 6 min 12 s. LLM spend today: $0.12 of $0.50 cap.
```

### 8.4 "Nothing notable today" (short form, no Opus call)

```
Subject: [Steam daily] Oct 24: nothing notable

Nothing crossed a threshold today.
  Hollow Harbor     +121 reviews   7d positive 76.2% (+1.2)
  Pinecrest Rally    +41 reviews   7d positive 91.0% (-0.3)
Data feeds: all ok (build ids via PICS websocket). Run 5 min 40 s. LLM spend $0.00.
```

There is also a **failure form**: what failed, which step, the `drun_` or session ID, and
whether yesterday's data is still the latest.

---

## 9. Guardrails

### 9.1 Spend caps

- **Session cap (platform-enforced):** the deployment `budget` of $0.75 is copied onto each
  session. It covers the agent's tokens and session runtime at list price. At the cap the
  session pauses (`budget_reached`). It does not cover API calls that Python makes with the
  vaulted key, because those are separate Messages API requests.
- **Job cap (code-enforced):** `DAILY_LLM_CAP_USD` (default $0.50) and
  `MONTHLY_LLM_CAP_USD` (default $15), checked against `llm_usage` before every call.
  Over the cap, the job sends the templated report and says so in feed health.
- **Price table:** keyed by exact model ID. `models.resolve` (`models.py:145-162`) can pick
  a newer model at runtime. If the resolved ID has no price entry, fail closed (no LLM
  calls) rather than spend at an unknown rate.
- **Account backstop:** use a dedicated Console workspace and API key for the job, with a
  monthly spend limit.

### 9.2 When a run fails

| Failure | Who notices | What you get |
|---|---|---|
| A step fails inside the sandbox | `daily_job` (exit 1), then the agent | Failure email via `notify-failure` |
| The session never starts (`session_rate_limited`, `vault_not_found`, archived environment) | Watchdog | "No report today" email |
| The session hits its budget or hangs | Watchdog | Same |
| Every build-id source fails for a title | `daily_job` (`build_status = unavailable`) | The report still goes out. Feed health lists the title, and the report does not claim "no update". |
| Supabase is down | Nobody, from inside | The agent's failure email still goes through Resend. The watchdog runs in Supabase, so a full outage is silent. Accept this for a personal tool, or add a Phase 2 webhook handler. |

The watchdog is a Supabase `pg_cron` job at the scheduled time plus 3 hours. If
`job_runs[today]` is not `emailed`, it calls Resend through `pg_net` with a key and
recipient stored in Supabase Vault. It needs no extra service.
Optional (Phase 2): a Supabase Edge Function subscribed to Managed Agents webhooks
(`deployment_run.failed`, `session.status_terminated`) for faster alerts.

### 9.3 Secrets

| Secret | Lives in | Seen by sandbox? |
|---|---|---|
| Job `ANTHROPIC_API_KEY`, `SUPABASE_SERVICE_KEY`, `RESEND_API_KEY`, `REPORT_TO` | Managed Agents vault, host-limited | Placeholder only |
| GitHub PAT (Contents: Read) | Deployment `github_repository` resource | No (git proxy) |
| Resend key and recipient for the watchdog | Supabase Vault | n/a |
| Your admin Anthropic key, Supabase DB password | Your PC (`.env`, `ant auth login`) | n/a |

Nothing secret goes in the repo, the agent prompt or `initial_events`. `.env` stays
gitignored (`.gitignore`). `REPORT_TO` is set in two places (the vault and Supabase Vault).
The checklist lists both.

### 9.4 Review text is untrusted input

- Review text reaches exactly one place: the narrative call. That call has no tools and
  uses structured output. Excerpts are wrapped in delimited data blocks, and the system
  prompt says they are data, not instructions.
- The agent with `bash` never sees review text. The status JSON carries counts only.
- Model output is validated: every number must appear in the facts bundle, every quoted
  review ID must exist in the window, and text length is capped. Output is escaped into
  the template. On a failed check, the job drops the narrative and keeps the numbers.
- The recipient is not a model output. It is substituted at egress from the vault.

---

## 10. Data migration (one time, from your PC)

1. Create the Supabase project. Apply `db/postgres/001_init.sql` (tables, indexes, RLS on,
   functions). Your Supabase connector can do this step from a Claude session with your
   approval.
2. Check the size: `reviews.db` on disk vs the 500 MB free tier. Postgres with indexes is
   usually larger than the SQLite file. If it is close, plan for Supabase Pro.
3. Run `python migrate_to_supabase.py --sqlite reviews.db`:
   - copies `products`, `releases`, `runs`, `analyses`, then `reviews` in batches of 500 through
     the same RPC upserts the job uses;
   - is resumable: it skips batches whose `recommendationid`s already exist;
   - verifies per `(appid, language)`: row count, `SUM(voted_up)`, `MAX(timestamp_created)`,
     and prints a diff. Any mismatch fails the run.
4. Keep `reviews.db` as a cold backup. Stop the Windows task
   (`schtasks /Delete /TN "SteamReviewCollector" /F`) after the first good cloud run, so
   two collectors do not write.

---

## 11. Cost estimate

Prices from the `claude-api` reference: Claude Opus 5.5 $4 input, $20 output, $0.20 cache read
per million tokens; Claude Haiku 5.5 $0.10 / $0.50 (prompts up to 100K); Managed Agents
session runtime $0.08 per hour. Token counts below are estimates. Check them against
`llm_usage` and the session's `usage.list_cost` after the first week.

| Item | Per day | Per month (30 days) |
|---|---|---|
| Session runtime, about 10 min | $0.014 | $0.40 |
| Agent loop, Opus 5.5 effort low, 3-6 turns (about 30K input mostly cached, 3K output with thinking) | $0.08-$0.15 | $2.40-$4.50 |
| Narrative, Opus 5.5 effort medium (about 12K in, 3K out), only on days with signals | $0.00-$0.11 | $1.00-$3.30 |
| Theme tagging, Haiku 5.5 (Phase 2, about 500 new reviews per day) | $0.01 | $0.30 |
| **Anthropic total** | **about $0.10-$0.30** | **about $4-$9** |
| Supabase | free tier $0 (Pro $25 if over 500 MB or you want backups) | $0-$25 |
| Resend | free tier $0 (verify current limits) | $0 |
| GitHub | $0 | $0 |

Options that change the number:

- Agent on Haiku 5.5: saves about $2-$4 per month.
- Pre-warming the dashboard's primary views (`PREWARM_PRIMARY_VIEWS=1`) adds the Opus
  summary cost (`analyze.py:346-353`) each time a view crosses `REFRESH_MIN_NEW`
  (`analyze.py:44`). Roughly $0.20-$0.50 per busy title per refresh. Off by default.

Anthropic API usage is billed to your Console account. It does not come out of a claude.ai
subscription.

---

## 12. Findings from the old plan that still apply

- **Edited reviews are never re-fetched.** `iter_reviews` walks `filter=recent` newest-first
  and stops at the last stored `timestamp_created` (`steam_api.py:270-319`, stop at
  `310-314`). A player who flips a review after a patch is missed, and that is exactly the
  post-patch signal. Phase 2 adds a periodic `filter=updated` sweep (confirm the parameter).
- **Failed collections leave no `runs` row.** On `SteamError`, `collect_product` commits,
  logs and `continue`s past `db.log_run` (`collect.py:190-193`). Add `status` and `error`
  columns and write the row on both paths. Feed health depends on it.
- **`LANG_MIN_VOLUME` drift.** `app.py:40` defaults to 1000, `README.md:194` says 1000, and
  `.env.example:43` documents 500. Pick one value when the helpers move to `scopes.py`, so
  the detector and the dashboard agree.
- **Five copies of the DB path lookup:** `collect.py:41`, `app.py:44`, `clear_cache.py:30`,
  `export.py:25`, `setup.py:30`. Replace them with one `config.py` that also reads
  `DATABASE_URL`.
- **`author.num_reviews` and `num_games_owned` are dropped** in `upsert_review`
  (`db.py:149-183`). They are useful review-bomb markers. Add the columns in Phase 2.
- **News posts act as releases today.** `refresh_releases` stores every `patchnotes` post as a
  release, and `record_public_build` skips a build that falls within 36 hours of one
  (`collect.py:62-76`, `108-116`, `123-162`). Both rules go away once build ids are the only
  release signal (Phase 0, item 7).
- Dropped from the old plan as local-only: Task Scheduler cadence, lock files, the
  sleeping-PC heartbeat, Windows toasts, the hourly patch watch, and the dashboard outbox.

---

## 13. Phased rollout

Effort assumes one developer who knows this codebase.

### Phase 0: spike and refactors (about 4-6 days)

1. **Spike first (about 1 day, a decision gate).** Create the environment, vault, agent and a
   paused deployment. Fire a manual run with a throwaway script that reports:
   - the sandbox Python version, and whether `uv python install 3.12` works;
   - whether `pip install steam gevent-eventemitter "protobuf<3.21"` works;
   - whether the CM WebSocket upgrade passes under `limited` with `*.steamserver.net` allowed,
     and whether the Section 5 shim logs in anonymously and reads appid 730's public `buildid`
     (compare with `api.steamcmd.net/v1/info/730`). Only if that fails, repeat under `unrestricted`
     as a test, and try raw TCP to a CM server there;
   - whether `api.steamcmd.net/v1/info/730` is reachable under `limited`;
   - whether vault placeholders get substituted on HTTPS calls to Supabase, Resend and
     `api.anthropic.com` from Python `requests` and the `anthropic` SDK;
   - whether `fallbacks="default"` combines with structured output.

   Write the results into this doc.
2. `config.py`: one DB path and env loader, with the `DATABASE_URL` switch.
3. Move `_resolve_window`, `_scope_label` and `_language_eligibility` plus `LANG_MIN_*` out
   of `app.py:40-41`, `54-174` into `scopes.py` with no Flask import. `app.py` imports them.
   Fix the default drift.
4. Move all raw SQL into `db.py` (Section 4.2), and replace `_basic_stats` with an aggregate.
5. Split `db.py` into a facade, `db_sqlite.py` and `db_rest.py`. Write `001_init.sql` with
   the functions.
6. `runs` status and error columns, and the error-path row (`collect.py:190-193`).
7. Build ids become the only release signal. `collect.refresh_releases` (`collect.py:123-162`)
   keeps the launch date and stops inserting `patchnotes` news posts as releases (the cloud
   path keeps news as context only). Drop the 36-hour news de-dup in `record_public_build`
   (`collect.py:75`, `108-116`), which skips a build whenever a news release is near it.
   Add the shim to `steam_api.get_public_build_id`, and add `steam_api.get_public_build_id_mirror`
   for `api.steamcmd.net`. A `build_source` chain function tries them in order.
   `collect.collect_all()` returning per-title results. `record_public_build` returns build
   info. `collect.main()` stays as a thin wrapper for local use. Logging goes to stdout as
   well as `collect.log` (`collect.py:31-38`), since the sandbox file is thrown away.
8. `models.py`: add the `narrative` role. Usage logging at every call site. Move
   `analyze.py:114` and `158` from `output_format` to `output_config.format`.
9. `tests/` with a synthetic SQLite fixture and `pytest`. Run the same function tests against
   a Supabase branch or a local Postgres.

Exit criteria: local SQLite use behaves exactly as before; tests pass on both backends.

### Phase 1: MVP daily email (about 4-6 days)

- `migrate_to_supabase.py`, run once, with verification.
- `daily_job.py` (steps in 6.1), `ops/daily.sh`, `requirements-cloud.txt`.
- Detectors: `day_over_day`, `post_release_shift`, `review_bomb` (existing columns),
  `language_divergence`, `data_health`.
- Narrative call, validation, Jinja templates (full, quiet and failure forms), Resend send.
- `job_runs` idempotency, spend caps, `pg_cron` watchdog.
- `agents/steam-daily/` files for `ant apply`: environment, agent, deployment.
- Dashboard on `DATABASE_URL`, plus `sync_down.py`.
- One week of runs with the subject prefixed `[TEST]`. Then tune thresholds and drop the prefix.

### Phase 2: anomaly depth and themes (about 4-6 days)

- Haiku 5.5 theme tagging of new reviews into `theme_catalog` and `theme_mentions`,
  seeded from the cached `themes_bad` entries. Adds `emerging_theme`.
- `filter=updated` sweep for edited reviews.
- Author markers in `review_bomb`.
- GitHub Actions PICS job (chain source 3), only if the Phase 0 spike shows that neither
  WebSocket PICS nor `api.steamcmd.net` is dependable from the sandbox, or you want a
  third path anyway.
- A dedicated `steam_job` DB role instead of the service key.
- Webhook-based failure alerts (optional).
- Detector backtest over history ("as of day D") to tune false alarms.

### What you need to set up yourself

- [ ] Anthropic Console: a workspace for this job with a monthly spend limit, and an API
      key in it. A separate key for the dashboard is optional.
- [ ] The `ant` CLI, then `ant auth login` on your PC (used to create the agent and deployment).
- [ ] Supabase: a project (note the region). Save the project URL and the service-role key.
      Enable `pg_cron` and `pg_net`.
- [ ] Resend: an account and an API key. Verify a sending domain, or confirm you only
      send to the address you signed up with.
- [ ] GitHub: a fine-grained PAT with `Contents: Read` on this repo only, and a branch
      `agent-stable` for the deployment.
- [ ] Managed Agents vault credentials: `ANTHROPIC_API_KEY`, `SUPABASE_SERVICE_KEY`,
      `RESEND_API_KEY` (header), `REPORT_TO` (body), each limited to its host.
- [ ] Supabase Vault: `resend_api_key` and `report_to` for the watchdog.
- [ ] Managed Agents environment `allowed_hosts` includes `*.steamserver.net` and
      `api.steamcmd.net`, next to the Steam, Supabase, Anthropic and Resend hosts.
- [ ] Only if the chain reaches source 3: a GitHub Actions workflow and a repo secret for the
      Supabase key (no other sign-up is needed for build ids).
- [ ] Choose the send time and your IANA time zone (outside 01:00-03:00).
- [ ] Run the one-time migration from your PC. Delete the Windows scheduled task after
      the first good cloud run.

---

## 14. Open questions

1. What time should the email arrive, and in which time zone?
2. Do you have a domain for sending email, or is the report only ever going to your own
   signup address? That decides whether Resend needs domain verification.
3. If the spike shows the sandbox cannot pass the WebSocket upgrade, are you fine with
   `api.steamcmd.net` (a free community mirror, no SLA) as the build-id source, or do you want
   the GitHub Actions PICS job as the primary path? News is not an option.
3a. Existing release rows that came from news posts: keep them as history (the default), or
    remove them so the release list holds build ids only?
4. How big is `reviews.db` today, and are you fine on Supabase's free tier (no automatic
   backups), or do you want Pro at $25 per month?
5. Are the default caps right: $0.75 per session, $0.50 per day and $15 per month for the
   job's own calls? Do you want the dashboard views pre-warmed each day at extra cost?
