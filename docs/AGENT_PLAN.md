# Plan: a daily cloud agent that emails a Steam review report

Status: proposal. No code has changed. Written 2026-10-22 against commit `b61f323`.
Section 5 (build ids) was tested on 2026-10-22 and replaces an earlier news-feed fallback.
Sections 4 (data store), 7 (backfill) and 10 (delivery) were researched on 2026-10-22. Code
line references in Section 7 were taken at commit `705e075`.
This replaces the earlier plan, which recommended a local Windows Task Scheduler agent.

## Decisions so far

- **Where it runs:** in the cloud on Claude Managed Agents, not on your PC.
- **How often and when:** once per day, starting at 08:31 America/Los_Angeles so the email
  arrives at 09:00 (Section 3).
- **Release signal:** build ids only (Steam PICS). The news feed is rejected as a signal,
  because it includes posts that are not patches. If the Phase 0 WebSocket test fails,
  `api.steamcmd.net` (a community mirror) becomes the primary build-id source.
- **Data store:** Turso (libSQL) Free plan. Backups: 1-day restore plus weekly dumps (decided).
  Runner-up: a `steam` schema in an existing Supabase project (Section 4.9).
- **Delivery:** Resend to the owner's address now; a Google Group plus verified domain when
  recipients are added. Phase 1 uses Resend's test sender, so no domain is needed yet
  (Section 10.7). A Slack bot (`chat.postMessage`) is the alternative if every recipient
  shares one Slack workspace.
- **Caps:** two separate meters. The agent session cap ($0.75, platform-enforced: the runner
  agent's own tokens and sandbox runtime) and the job API cap ($0.50/day, code-enforced: the
  Python job's own Claude calls). Section 11.1.
- **Watchdog:** Healthchecks.io, outside both Anthropic and the data store.
- **Backfill:** a separate mode that you trigger. It has its own dollar ceiling, and it is
  not part of the daily run or the daily cap.

Plain, deterministic Python does the collection, change detection, report rendering and
the send. Claude is used for the judgment part: a short daily narrative (what changed, why
it matters, what to watch), through the Messages API and `models.py`.

Model IDs, prices and Managed Agents behavior below come from the Anthropic `claude-api`
reference (model table cached 2026-10-06). Managed Agents is a beta API. Re-check the
details marked "verify" before building.

---

## 1. Summary

| Question | Answer |
|---|---|
| Where it runs | A Managed Agents cloud sandbox, started by a scheduled deployment once per day (cron + IANA time zone). |
| What the agent does | Runs one command from this repo (`python -m daily_job run`), reads a one-line status, retries once or sends a failure email. It never reads review text. |
| Where data lives | Turso (libSQL, SQLite dialect), one database named `steam`. The sandbox is thrown away after each run. |
| How the job reaches the DB | A small adapter in the sandbox (`db_turso.py`) posts SQL to Turso's HTTP API over HTTPS. The database token sits in a Managed Agents vault and is added to the `Authorization` header at egress. The sandbox never sees the real token. |
| Patch detection | Build ids only: the public-branch `buildid` from Steam PICS, read over WebSocket on port 443 (tested, Section 5). Fallback: `api.steamcmd.net` over HTTPS (community mirror), and the primary source if the Phase 0 WebSocket test fails. A GitHub Actions PICS job is an optional later option. The news feed is never a release signal. If every source fails, the report says "build id unavailable" and makes no claim about updates. |
| Delivery | Resend HTTPS API. Resend to the owner's address now; a Google Group plus verified domain when recipients are added (Section 10.7). Slack bot is the alternative (Section 10). |
| New titles | Not part of the daily run. `python -m daily_job backfill <appid>` collects history and builds the first analyses, with its own ceiling ($3.00 and 80 calls per title by default). The daily run skips an unbaselined title and says so (Section 7). |
| Watchdog | Healthchecks.io check with a 3-hour grace period. It emails you when no success ping arrives. |
| Dashboard | Still runs locally (`python app.py`). It reads the cloud DB when `DATABASE_URL` points at Turso, or a local SQLite copy. |
| Cost | About $0.10-$0.30 per day in Anthropic usage, so about $4-$9 per month. Turso, Resend and Healthchecks free tiers cover the rest. The sending domain is $0 now (Resend test sender) and about $10-$15 per year once recipients are added. Backfill is a one-time $0.5-$7 per title. See Section 13. |

### Rejected alternatives

A GitHub Actions cron job would be cheaper and has unrestricted network egress, so PICS
would work over raw TCP. It is kept only as an optional later build-id source (Section 5). A Claude Code
scheduled cloud task would spend subscription tokens on a general coding agent, give
less predictable runs, and face the same state and network problems. Neither is the
main runtime.

---

## 2. Architecture

```
                         Anthropic (Managed Agents, beta)
  +--------------------------------------------------------------------+
  |  Scheduled deployment  "steam-daily"                               |
  |  cron "31 8 * * *", America/Los_Angeles, agent session cap $0.75   |
  |        |                                                           |
  |        v  fires once per day                                       |
  |  Session (agent loop on Anthropic)  ---- bash ---->  Cloud sandbox  |
  |  model claude-opus-5-5, effort low                  (thrown away   |
  |  tools: bash, read only                              after run)    |
  |                                                     /workspace/    |
  |  Vault: ANTHROPIC_API_KEY, TURSO_AUTH_TOKEN,          Steam_Aggregator
  |         RESEND_API_KEY, REPORT_TO, ALERT_TO          (cloned, read-only PAT)
  |  (substituted into HTTP headers/body at egress)                    |
  +---------------------------------------------------|----------------+
                                                      | python -m daily_job run
        +----------------+----------------+-----------+-----+------------------+
        |                |                |                 |                  |
        v                v                v                 v                  v
 store.steampowered.com  steam-<org>.   api.anthropic.com  api.resend.com   hc-ping.com
 api.steampowered.com    turso.io       (narrative call,   (one email to    (start /
 *.steamserver.net       (HTTP API,     models.py)         REPORT_TO)       success /
 api.steamcmd.net        libSQL)                                |            fail pings)
 (reviews, details,         |                                   v                 |
  PICS build ids)           v                            Owner inbox now        v
                  Turso database "steam"                 Group later      Healthchecks.io
                  reviews, products, releases, runs,                      no success ping by
                  analyses, job_runs, daily_snapshots,                    send time + 3 h ->
                  signals, reports, llm_usage,                            email to you
                  backfill_state

  Your PC:  python app.py  (DATABASE_URL=libsql://steam-<org>.turso.io)
            python sync_down.py  (optional local SQLite copy / backup)
            python migrate_to_turso.py  (one-time upload of reviews.db)
            python -m daily_job backfill <appid>  (new titles; Section 7)
```

Why this shape:

- **Custom tools are not used.** A scheduled run has no client attached. A custom tool
  call would wait for an orchestrator that is not running. Everything the agent needs runs
  inside the sandbox through `bash`.
- **The agent is a thin, hosted runner.** The schedule, the sandbox, the run records and the
  hard dollar cap are what Managed Agents adds here. The work itself is deterministic code.
- **Review text never enters the agent's context.** It goes only to a tool-less Messages API
  call with structured output (Section 11.4).
- **Every secret travels in an HTTP header or body.** That is the only place the vault can
  fill one in. It is why the store, the mail service and the alert path are all HTTPS APIs.

---

## 3. How the daily schedule works on Managed Agents

You create four objects once, ideally as version-controlled files under `agents/steam-daily/`
synced with `ant apply`. The `ant` CLI stores the IDs in `claude-lock.json`.

1. **Environment** (`config.type: "cloud"`):
   - `networking.type: "limited"` with `allow_package_managers: true` (PyPI) and
     `allowed_hosts`:
     - `store.steampowered.com`, `api.steampowered.com`
     - `*.steamserver.net` (Steam CM WebSocket servers, port 443)
     - `api.steamcmd.net` (build-id source; primary if the Phase 0 WebSocket test fails)
     - `steam-<org>.turso.io` (the data store)
     - `api.anthropic.com`
     - `api.resend.com`
     - `hc-ping.com` (Healthchecks.io pings; verify the host at signup)
     - `slack.com`, only if Slack delivery is on.

     Add the GitHub release hosts only if the spike shows the sandbox needs `uv` to install
     Python 3.12.
   - No `unrestricted` networking unless the Phase 0 spike shows that `limited` cannot pass the
     CM WebSocket upgrade, and you accept that trade.
2. **Vault** with `environment_variable` credentials. The sandbox sees only an opaque
   placeholder. The real value is substituted into outbound HTTP requests to the listed hosts.
   Both the credential's `allowed_hosts` and the environment's `allowed_hosts` must permit the host.

   | `secret_name` | Allowed host | `injection_location` |
   |---|---|---|
   | `ANTHROPIC_API_KEY` (a job-only key) | `api.anthropic.com` | header |
   | `TURSO_AUTH_TOKEN` (a token scoped to the `steam` database) | `steam-<org>.turso.io` | header |
   | `RESEND_API_KEY` | `api.resend.com` | header |
   | `REPORT_TO` (the owner's address now; the Google Group address later) | `api.resend.com` | body |
   | `ALERT_TO` (your own address, for failure alerts) | `api.resend.com` | body |
   | `SLACK_BOT_TOKEN` (optional) | `slack.com` | header |

   Substitution covers HTTP headers and body only. A secret in a URL path is never
   substituted. It does not work for SMTP, the Postgres wire protocol or S3 request signing,
   because those compute with the secret inside the sandbox. The vault never puts the raw
   secret in the sandbox, and no setting changes that. This is the main reason the job talks
   to Turso, Resend and Slack over HTTPS with plain `requests`. A client library that checks
   a key's format locally would see the placeholder and might fail before it sends anything.
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
   - `initial_events`: one `user.message`, "Run today's Steam report per the runbook.
     Healthcheck URL: <ping url>". The ping URL is the one value that cannot be vaulted
     (Section 11.2).
   - `schedule`: `{type: "cron", expression: "31 8 * * *", timezone: "America/Los_Angeles"}`.
     The run starts at 08:31 so the email arrives before 09:00 even with one retry, and the
     IANA zone follows daylight saving time.
   - `budget`: `{type: "limit", max_list_cost: {amount: "75", currency: "USD"}}` ($0.75, the agent session cap, platform-enforced: the runner agent's own tokens and sandbox runtime).

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
  deployment does not backfill missed days. The Healthchecks check (Section 11.2) catches both.
- Every firing writes a `deployment_run` record (`drun_...`). Use
  `client.beta.deployment_runs.list(deployment_id=..., has_error=True)` to audit failures.
- `client.beta.deployments.run(id)` fires a manual run. It also works while the deployment
  is paused. Use it for testing.

---

## 4. Data store: Turso

The sandbox is not persistent, so state must live in a hosted store. Prices and free-tier
limits were checked on 2026-10-22 (Section 4.10).

### 4.1 Decision

Use **one Turso (libSQL) database named `steam`, on the Free plan.** The job reaches it over
Turso's documented HTTP API with a database-scoped token in the `Authorization` header.

Why not Supabase: a new free project is not possible. Supabase allows two active free
projects across every organization where you are Owner or Admin, and you already run two.
Paused projects do not count.

Why Turso:

- It costs $0 and has no tie to your Supabase quota or your other apps. Free allows 100
  databases and 5 GB, ten times Supabase Free's 500 MB.
- It is the exact case the vault was built for: a bearer token, a header, one host.
- The SQL stays SQLite, the dialect `db.py` already uses. The 18 query functions and the raw
  SQL in `collect.py`, `export.py` and `clear_cache.py` run unchanged. That saves about 3
  developer-days against a Postgres rewrite.
- Leaving is cheap. A dump gives back a plain SQLite database for D1, object storage or a
  local file.

### 4.2 Options compared

| # | Option | Cost per month | Code change in `db.py` | Reliability | Vault fit |
|---|---|---|---|---|---|
| 1 | Existing Supabase project, `steam` schema, restricted access | $0 | Full second backend: REST/RPC version of every query function, about 300 lines of Postgres functions, and the raw SQL in three files moves into `db.py` first. | Good platform. Shared fate with the other app: its quota, its migrations, its deletion. No backups on Free. | Good with a narrow header key. Never the secret key. |
| 2 | Supabase Pro in a new paid org | $25 | Same as option 1. | Best of the six: daily backups kept 7 days, no pausing, 8 GB disk. | Good. |
| 3 | Neon Free Postgres | $0 | Same rewrite as option 1. Neon documents its HTTP SQL protocol only for its JavaScript driver. | Good vendor. Compute sleeps after 5 minutes idle. 6-hour restore window. | Poor. `psycopg` needs the password in the sandbox and raw TCP. |
| 4 | **Turso (libSQL), HTTP API** | **$0** (5 GB, 500M rows read, 10M rows written per month). $4.99 for 10-day restore. | **About 10-15 of 394 lines** (`connect()` dispatch, skip the WAL PRAGMAs), plus one adapter module of about 150 lines. | Managed, 1-day point-in-time restore on Free. Smaller vendor, mid product transition. | **Best.** `Authorization: Bearer <token>`, header-only, scoped to one database. |
| 5 | SQLite file in object storage (R2 or B2), downloaded at start, uploaded at end | $0 | None in `db.py`. A sync script of about 80 lines. | Durable storage, but you build the consistency. A failed upload loses that day's writes. Two concurrent runs: last writer wins. | Poor through S3 APIs (SigV4 signs with the secret inside the sandbox). |
| 6 | Cloudflare D1, REST API | $0 (500 MB per database, 5M rows read and 100K rows written per day) | Small adapter like Turso's, for D1's JSON shape. REST parameters are typed as strings. | Strong platform, 7-day Time Travel. The REST API is positioned for management, not app traffic. A few dashboard page views on a large title can use the daily read limit. | Good. Bearer token. The token's D1 permission covers the account, not one database (verify). |

What decided the ranking:

- **Option 1 puts the other app at risk, and the other app puts this one at risk.** On Free,
  if the shared project passes 500 MB, Supabase's Fair Use Policy can make it read-only or
  return 402 for every API call, which takes down both apps. A store inside an app project
  also goes away when that project is reset or deleted. You replaced `fitness-app` with
  `fitness-app-v2` within the last few days.
- **Option 2 is the most robust, and the most expensive line in the plan.** $25 is 3-6 times
  the job's Anthropic usage, and it still needs the full REST rewrite.
- **Option 3 has no advantage here.** Same rewrite, weaker vault path.
- **Option 5 keeps `db.py` untouched but moves the hard parts into a sync script.** Every run
  moves the whole file twice. The idempotency row (`job_runs`) lives inside the file it
  protects, so two concurrent runs can both start from the same copy.
- **Option 6 is close to Turso on code, but its free limits are per day and small.** Consider
  it only if you move more of the system onto Cloudflare.

### 4.3 How big the data is

`reviews.db` is not in the repo, so the schema was measured instead. The `db.py` schema was
built in SQLite 3.45, filled with 50,000 synthetic reviews per text length, vacuumed, and the
file size divided by the row count:

| Average `review_text` | Bytes per review row (table + 3 indexes) |
|---|---|
| 0 bytes | 135 |
| 200 bytes | 351 |
| 400 bytes | 589 |
| 800 bytes | 1,109 |

A review row costs about 150-300 bytes plus its text. Use **about 0.6 KB per review** as the
planning figure (about 400 bytes of text on average; CJK text uses 3 bytes per character in
UTF-8). `products`, `releases`, `runs` and `analyses` stay under 1 MB per title. The new
`reports` table adds about 10 MB per year if it keeps the HTML.

| Title profile | Reviews stored | SQLite / Turso / D1 | Postgres (for comparison) | New reviews per day | Growth per year |
|---|---|---|---|---|---|
| Small indie | 5,000 | 3 MB | 4 MB | 20 | 4 MB |
| Mid-size | 50,000 | 30 MB | 40 MB | 150 | 33 MB |
| Large | 300,000 | 180 MB | 240 MB | 1,000 | 215 MB |
| Blockbuster | 1,000,000 | 600 MB | 800 MB | 3,000 | 650 MB |

On Turso Free (5 GB), size is not a constraint for any plausible set of titles. It would
have been one on Supabase Free or D1 Free (500 MB). Measure the real file from the project
folder on your PC before migrating:

```
python -c "import os; print(os.path.getsize('reviews.db') / 1e6, 'MB')"
sqlite3 reviews.db "SELECT appid, COUNT(*), AVG(LENGTH(review_text)) FROM reviews GROUP BY appid"
```

### 4.4 How the job reaches Turso

- Python `requests` posts to `POST https://steam-<org>.turso.io/v2/pipeline` with
  `Authorization: Bearer <placeholder>`. Use plain `requests` rather than a native client, so
  you control the headers and nothing checks the token's format.
- Create two database tokens with `turso db tokens create steam`: one for the vault, one for
  your PC (`.env`). Never use an organization-level Platform API token. It can manage every
  database.
- Limits to watch: the daily job reads a few million rows per month, and the dashboard reads
  about 0.3-0.5M rows per page view on a 100,000-review title. Both fit inside 500M reads per
  month. Writes are a few thousand rows per day plus one-time backfills, well under 10M.

Weaknesses and what covers them:

- **Backups (decided):** Turso Free, which keeps 1 day of point-in-time restore, plus a weekly
  dump from your PC (checklist, Section 15). The Developer plan ($4.99 per month, 10 days,
  9 GB) is the paid option if that changes.
- **Inactivity:** Turso's CLI docs say free databases are archived after 10 days of
  inactivity. A March 2025 Turso post says free databases no longer sleep. The two sources
  conflict (verify). The daily run keeps the database active either way.
- **Vendor risk:** Turso is a smaller company, and its new engine is in preview. Use the
  documented HTTP v2 API against a standard libSQL database, not the preview engine.
- **No scheduler in the database:** the watchdog lives outside it (Section 11.2). That also
  closes a gap in the earlier Supabase design, where a store outage silenced the alert.

### 4.5 The backend switch in `db.py`

Goal: local use keeps working exactly as today, with no new settings.

- `DATABASE_URL` unset, or `sqlite:///path`: SQLite, as now (`db.connect`, `db.py:88-103`,
  path from `config.json`).
- `DATABASE_URL=libsql://steam-<org>.turso.io` (or `https://...turso.io`) plus
  `TURSO_AUTH_TOKEN`: the Turso backend.

Design:

1. `db.connect()` checks `DATABASE_URL`. For Turso it returns a `TursoConnection` from a new
   module, `db_turso.py`, and skips the two `PRAGMA` lines (`db.py:100-101`). Nothing else in
   `db.py` changes.
2. `TursoConnection` copies the small part of `sqlite3.Connection` that the code uses:
   `execute`, `executemany`, `executescript`, `commit`, `close`, plus rows that support
   `r["col"]`, `r[0]` and `dict(r)`. Turso returns integers as strings in JSON, so the
   adapter converts them back.
3. Writes are buffered, and the buffer is flushed before any read, so a read sees earlier
   writes. `commit()` sends the buffer as one pipeline request wrapped in `BEGIN`/`COMMIT`, so
   a flush is all or nothing. `collect.py` already commits every 500 reviews
   (`collect.py:184-189`), so a normal day is 2-4 HTTP requests.
4. `executescript(SCHEMA)` goes over as one multi-statement request. The new tables
   (Section 4.6) are added to `db.SCHEMA`, so local and cloud share one schema.
5. Replace `analyze._basic_stats` (`analyze.py:228-233`) before switching backends. It loads
   every review row through `db.sentiment_rows` (`db.py:320-332`) only to count them. Over HTTP
   that would download the whole table. Use a `review_stats(appid, languages, ts_from, ts_to)`
   aggregate.
6. Language buckets (`db.py:246-269`) and the raw SQL in `collect.py`, `clear_cache.py` and
   `export.py` stay where they are.

The earlier Postgres design (a facade, `db_rest.py`, a SQLite-to-Postgres type map and a
`steam_job` database role) is dropped.

### 4.6 Schema additions

Add these to `db.SCHEMA`, in SQLite types: dates as `TEXT 'YYYY-MM-DD'`, JSON as `TEXT`,
auto ids as `INTEGER PRIMARY KEY AUTOINCREMENT`, decimals as `REAL`. `INSERT ... ON CONFLICT
DO UPDATE` already works in SQLite (`db.py:108-117`).

```sql
create table if not exists job_runs (      -- one row per report date; the idempotency key
  report_date text primary key,            -- local date in REPORT_TZ
  status text not null,                    -- running | collected | reported | emailed | failed
  attempt integer not null default 1,
  started_at integer, heartbeat_at integer, finished_at integer,
  session_id text,                         -- Managed Agents session, for tracing
  build_source text,                       -- pics_ws | steamcmd_net | github_job | none
  build_status text,                       -- ok | partial | unavailable
  summary text, error text,                -- summary is JSON
  email_id text, slack_ts text             -- one result per delivery channel
);
create table if not exists daily_snapshots (   -- per title/language/day; makes "since yesterday" cheap
  report_date text, appid integer, language text,
  total integer, positive integer, new_24h integer, positive_24h integer,
  pct_7d real, n_7d integer, pct_28d real, n_28d integer,
  primary key (report_date, appid, language)
);
create table if not exists signals (
  id integer primary key autoincrement, fingerprint text unique, report_date text, appid integer,
  detector text, language text, severity text, metrics text,   -- metrics is JSON
  first_seen text, last_seen text, status text                 -- open | resolved
);
create table if not exists reports (
  report_date text primary key, kind text,                     -- full | quiet | failure
  subject text, html text, text_body text, narrative text, created_at integer
);
create table if not exists llm_usage (
  id integer primary key autoincrement, ts integer, report_date text,
  purpose text,          -- narrative | backfill | backfill_lazy | dashboard | theme_tagging
  model text, input_tokens integer, output_tokens integer, cache_read_tokens integer,
  cost_usd real
);
create table if not exists backfill_state (    -- Section 7
  appid integer primary key,
  status text not null,        -- none | collecting | analyzing | baselined | insufficient
  collected_at integer, priority_done_at integer,
  spent_usd real default 0, calls integer default 0,
  last_scope text, batch_ids text, updated_at integer   -- batch_ids is JSON
);
```

Also:

- `runs` gains `status` (`complete` or `error`), `error`, `trigger` and `pages` columns through
  guarded `ALTER TABLE ... ADD COLUMN` statements. It fixes the missing error rows (Section 14).
- Add an index on `reviews (appid, language, timestamp_created)`. It serves `last_seen_ts`
  (`db.py:129-135`) and every window query, and it cuts rows read, which Turso bills.

### 4.7 The dashboard

Two supported modes. Both keep Flask on `127.0.0.1` (`app.py:290`), because the Turso token
has full access to the database.

1. **Live cloud DB (recommended).** Put `DATABASE_URL` and `TURSO_AUTH_TOKEN` in `.env`.
   `app.py` reads the same data the job writes. Analyses generated from the dashboard
   (`/api/analysis`, `app.py:255-286`) go into the shared `analyses` cache, so the job and the
   dashboard reuse each other's work. The chart endpoints already return aggregated daily rows
   (`db.daily_sentiment`, `db.py:335-350`), so HTTP latency stays small. For a title with no
   baseline, `/api/analysis` returns a backfill prompt instead of generating (Section 7.7).
2. **Local copy.** `python sync_down.py` dumps the Turso database and loads it into a local
   `reviews.db` (it is one SQLite database, so this is a dump-and-load). Unset `DATABASE_URL`
   to use it. Useful offline, and as a free backup.

The per-view analyses (`analyze.get_analysis`, `analyze.py:236-368`) work in both modes,
because they call only `db.*` functions.

### 4.8 Data store watchdog

Turso has no scheduler. The dead-man's switch is Healthchecks.io (Section 11.2), which sits
outside both Anthropic and the store.

### 4.9 Runner-up: a `steam` schema in an existing Supabase project

Use this if you want to stay on Postgres. Checked read-only through the Supabase connector
on 2026-10-22:

| Project | Region | Status | Database size | `pg_cron` / `pg_net` installed |
|---|---|---|---|---|
| `fitness-app-v2` | us-east-1 | active, created 2026-10-19 | 13 MB | no |
| `WardrobeAI` | us-west-2 | active, created 2026-04-18 | 14 MB | no |
| `fitness-app` | us-west-1 | paused | n/a | n/a |

Pick the project you are least likely to reset or delete. That is `WardrobeAI`, which is six
months old. `fitness-app-v2` is three days old and replaced its predecessor.

How to keep it narrow:

- Create schema `steam`. Turn RLS on for every table with no policies. Revoke all privileges on
  the schema from `anon` and `authenticated`.
- Expose only `security definer` functions in `steam`. Each one first checks that
  `current_setting('request.headers', true)::json->>'x-steam-key'` matches a hash stored in
  `steam.job_key`. Grant `execute` on those functions alone to `anon`.
- Add `steam` to the Data API's exposed schemas. The job sends the publishable key as `apikey`,
  `Content-Profile: steam`, and the vaulted secret as `x-steam-key` (header-only). That secret
  can call the steam functions and nothing else. Never give the job the secret (service-role)
  key. It reaches every exposed schema.
- A custom-role JWT is an alternative only if the project can still sign tokens with a shared
  secret. Supabase's newer signing keys are asymmetric, so check first.
- Add a size check: when `pg_total_relation_size` over the `steam` tables passes 300 MB, put a
  warning in feed health. Move to Pro or to Turso before 450 MB.
- The daily run keeps `WardrobeAI` from pausing.

Cost of this path: a full second backend in `db.py` (REST/RPC calls to about 300 lines of
Postgres functions, a SQLite-to-Postgres type map, the raw SQL in three files moved into
`db.py` first), about 3 more developer-days, and a migration script for Postgres. The
watchdog stays Healthchecks.io, so `pg_cron` and `pg_net` are not needed.

### 4.10 Free-tier facts (checked 2026-10-22)

| Service | Free tier | Paid step |
|---|---|---|
| Turso | 100 databases, 5 GB, 500M rows read and 10M rows written per month, 3 GB syncs, 1-day point-in-time restore. | Developer $4.99: 9 GB, 2.5B reads, 25M writes, 10-day restore. |
| Supabase | 2 active free projects across orgs where you are Owner/Admin (paused ones do not count). 500 MB database per project, 5 GB egress, no automatic backups, pauses after 1 week of low activity. | Pro from $25 per org: 8 GB disk per project, daily backups kept 7 days, $10 compute credit covers one Micro project; each extra project is about $10. |
| Neon | 1 GB storage per project on Neon's pricing page (some third-party pages say 0.5 GB), 100 CU-hours per project, compute suspends after 5 minutes, 6-hour restore window. | Launch: pay as you go, $0.106 per CU-hour. |
| Cloudflare D1 | 10 databases, 500 MB per database, 5M rows read and 100K rows written per day, 7-day Time Travel. | Workers Paid: 10 GB per database, 30-day Time Travel. |
| Cloudflare R2 / Backblaze B2 | R2: 10 GB-month, free egress. B2: first 10 GB free. Third-party summaries disagree on B2 transaction pricing. | R2 about $0.015 per GB-month (verify). B2 $6.95 per TB-month. |
| Healthchecks.io | 20 checks, 100 log entries per check. | Supporter $5 (same limits). |

Sources: supabase.com/pricing; Supabase docs ("Billing FAQ", "About billing on Supabase",
"Project Pausing"); neon.com/pricing; turso.tech/pricing; docs.turso.tech
(`/sdk/http/reference`, `/cli/group/unarchive`, `/cli/db/create`, `/cloud/migrate-to-turso`);
turso.tech/blog/turso-cloud-debuts-the-new-developer-plan; developers.cloudflare.com (D1
pricing and limits, D1 query API, R2 pricing); healthchecks.io/pricing.

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

The tested prototype is `docs/spikes/ws_pics.py` (about 190 lines, including the shim). It is a
Phase 0 spike, not production code. Run it with `python docs/spikes/ws_pics.py 730 570 440`
on Python 3.12 with `steam`, `gevent-eventemitter` and `protobuf<3.21`. Use it as the
Phase 0 networking test inside the Managed Agents sandbox.

### 5.2 Recommendation and fallback chain

| Rank | Source | Needs | Use when |
|---|---|---|---|
| 1 | **PICS over WebSocket, port 443**, through the existing `steam` code plus the shim. First-party data, one login for all titles. | `*.steamserver.net` and `api.steampowered.com` in `allowed_hosts`. Python 3.12 plus `steam`, `gevent`, `protobuf<3.21` (Section 5.3). | Always first, if the Phase 0 WebSocket test passes. |
| 2 | **`api.steamcmd.net/v1/info/<appid>`** (HTTPS). Same PICS data, read through a free community mirror. | `api.steamcmd.net` in `allowed_hosts`. No library. | The primary source if the Phase 0 WebSocket test fails. Otherwise the fallback when source 1 fails the whole batch or a single app. Also a cross-check on days when a build changes. |

The job tries the sources in this order for each title and records the one that answered in
`job_runs.build_source`. If both sources fail for a title, the title's build status is
`unavailable`. The report says so, and it does not claim that nothing changed.

Why this order:

- Source 1 is Steam's own answer. It needs no third party, and it is the only source that
  does not depend on someone else staying online.
- Source 2 is the cheapest to run (one HTTPS call per title, no gevent). It is third-party, so
  it ranks second. Terms and limits: the site says the service is free "without specific
  restrictions", needs no key, has no SLA, and is run by one maintainer. The code is open
  source (so you could self-host it) and has a public status page. It is not affiliated with Valve.
- The GitHub Actions PICS job is not in the chain. It is the most work (a workflow, a schedule,
  and a second place that can fail), so it is an optional later option (Phase 2) for a third
  path. Cost if built: a public repo is free; a private repo uses roughly 30 of the 2,000 free
  minutes per month. Raw TCP is known to work on GitHub runners, but this was not tested here.
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
| `api.steamcmd.net` goes down, throttles, or changes its response | Source 2 is dead. | If source 1 also fails, the title is `unavailable` and the report makes no update claim. |
| One tracked app has no public branch, or PICS needs an access token for it | `buildid` is missing. | Report `unavailable` for that title. Never fall back to news. |
| A stale mirror answers with an old build id | A real patch is reported a day late. | The mirror's `timeupdated` must be at least as new as the last stored build. Otherwise treat the answer as stale and try the next source. |
| First run for a title | There is no earlier build to compare with. | Store the current build as the baseline with no "update" claim. |

---

## 6. The daily run

### 6.1 Steps (`python -m daily_job run`)

`daily_job.py` is a new module. All times are UTC except `report_date`, which is the local
date in `REPORT_TZ`.

| # | Step | Timeout | On failure |
|---|---|---|---|
| 0 | Preflight: check that the vault variables exist; ping Healthchecks `/start`; claim `job_runs[report_date]` (see 6.3); load today's LLM spend from `llm_usage`; list titles with no baseline and drop them from steps 1-8 (Section 7.2). | 30 s | Exit 1. The agent sends a failure email. |
| 1 | Release anchors per title: launch date from the store API. Patch-note posts from the news feed are fetched only as context (title and link), and they never create a release row. | 60 s per title | Log it, mark the title's release feed `error`, continue. This step is best-effort (`collect.py:217-222`). |
| 2 | Build ids: the Section 5.2 chain. One anonymous login for all appids over WebSocket, run in a subprocess with a hard timeout (gevent can hang), then `api.steamcmd.net` for any title still missing. Each new public build id becomes a `build_<id>` release row. | 90 s total | Set `build_source` and `build_status`, continue. An `unavailable` title is flagged in feed health. |
| 3 | Review collection: a new `collect.collect_all()` returning a structured result. Each language is incremental (`collect.py:165-198`). | 30 min total, 5 min per language | Write a `runs` row with `status='error'`. Continue with the next language. |
| 4 | Snapshot: SQL aggregates into `daily_snapshots` for `report_date`. | 60 s | Exit 1. Without data there is no report. |
| 5 | Detectors (Section 8) write `signals`. | 60 s | Report without signals, flagged in feed health. |
| 6 | Evidence: for each notable signal, sample at most 8 reviews (ID, language, playtime, first 600 characters). | 30 s | Narrative runs without quotes. |
| 7 | Narrative: only when at least one signal is `notable` or higher, or on the weekly day. One Opus call with structured output (6.2). | 180 s, SDK `max_retries=2` | Fall back to the templated text. The report still goes out. |
| 8 | Lazy backfill windows: after the narrative, generate up to `STEAM_BACKFILL_LAZY_WINDOWS_PER_RUN` missing older windows for baselined titles, if the daily cap has headroom (Section 7.6). | 5 min | Skip, and say "backfill paused" in feed health. |
| 9 | Render HTML and plain text from Jinja templates. Validate: required sections exist, and every number in the narrative appears in the facts bundle. Store the result in `reports`. | 30 s | On a validation failure, drop the narrative and use the template. |
| 10 | Deliver (Section 10): send through Resend to `REPORT_TO` with `Idempotency-Key: steam-report-<report_date>` (verify Resend's current idempotency support), and post to Slack if `DELIVERY_CHANNELS` includes it. The report counts as sent when at least one channel succeeds. Mark `emailed`, then ping Healthchecks success. | 30 s, 3 tries | Exit 1, and ping `/fail`. The agent retries once. Healthchecks covers the rest. |
| 11 | Print one status line of JSON for the agent: counts, statuses, no review text. | | |

Exit codes: `0` ok; `2` sent with warnings (degraded feeds, or a skipped unbaselined title);
`3` another run is active or today was already sent; `1` failed.

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
- Log `usage` to `llm_usage` with a `purpose`. Do the same for every `analyze.py` call site
  (`analyze.py:92`, `134`, `200`). Dashboard spend is logged as `purpose='dashboard'`: it counts
  toward the monthly cap and not the daily cap (Section 7.6).

### 6.3 Idempotency and reruns on the same day

- `job_runs.report_date` is the key. The claim is one upsert, sent as a single `BEGIN`/`COMMIT`
  pipeline request so it is atomic:
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
1. cd /workspace/Steam_Aggregator && HEALTHCHECK_URL=<url from the user message> bash ops/daily.sh
   (installs pinned deps if missing, then runs: timeout 2700 python -m daily_job run)
2. Read the last line of output (status JSON).
   exit 0 or 2 or 3: reply with the status line and stop.
   exit 1: if status.retryable is true and this is your first retry, wait 120 s and rerun once.
           Otherwise run: python -m daily_job notify-failure --reason "<status.error>" and stop.
Never open files under reports/ or print review text. Never edit files, install other
packages, or contact hosts other than through these commands.
```

The agent sees only exit codes and status JSON. Expected cost: 3-6 turns. `notify-failure`
needs only Resend, not the data store, so it still works during a Turso outage.

### 6.5 Steam API failures

- Keep the existing retry and backoff (`steam_api._get`, `steam_api.py:95-114`).
- Add a circuit breaker: after 3 consecutive 429s, stop Steam calls for the rest of the
  run and mark the remaining titles `skipped (rate limited)`.
- Write a `runs` row on the error path too (Section 14). The report's feed-health section
  lists every failed title and language.
- If every title fails collection, send the failure form of the report (to `ALERT_TO`), not
  "nothing notable". Data from a failed collection must never read as a quiet day.
- First-time backfills are excluded from the daily path. A new title is backfilled by a
  separate command you run (Section 7).

---

## 7. Backfill (first run on a title)

A new title has no history, and its first collection is too slow and too expensive for the
daily run. Backfill is its own mode. You trigger it, it has its own ceiling, and the daily
run never starts it.

### 7.1 Headline numbers

Prices come from the `claude-api` reference (model table cached 2026-10-06). The token counts
and thinking-token counts are estimates. Section 7.4 lists every assumption, and Section 7.8
says how to replace them with measured numbers after the first run.

- One view (one scope and one language filter) costs about **$0.14** typical, $0.06 low,
  $0.34 high. Two Opus summaries make up 93-96% of that. The Haiku theme calls are noise.
- The default backfill for one title (all-time and current release, eligible language views on
  those two scopes, plus the 3 releases before the current one) costs, typical:
  **small $0.53, medium $0.94, large $1.63**, at 18, 34 and 66 calls.
- A full backfill of every view the dashboard can show costs, typical: **small $0.53,
  medium $1.74, large $6.85**, at 18, 58 and 222 calls. The high case for large is $16.74.
- The Batch API would halve those numbers. It is recommended only for the older tiers (7.5).
- Recommended defaults: `STEAM_BACKFILL_MAX_USD=3.00` and `STEAM_BACKFILL_MAX_CALLS=80` per title.

### 7.2 Design

1. **Backfill is its own command, and it runs only when you trigger it.**
   `python -m daily_job backfill <appid>` (also `--all-unbaselined`). The daily runbook
   (Section 6.1) has no backfill step, and the deployment schedule never calls it. Run it from
   your PC. A Managed Agents session adds nothing here: the session budget does not cover the
   Python process's own API calls (Section 11.1), the collection stage can outlast the sandbox
   timeouts, and the Batch API option (7.5) needs a process that can wait for results.
2. **The daily run skips titles with no baseline, and says so.** A title has a baseline when
   `backfill_state.status` is `baselined` (table in Section 4.6). In preflight (Section 6.1,
   step 0) the job lists unbaselined titles and drops them from steps 1-8: no collection, no
   snapshots, no detectors, no narrative. Without history the detectors have nothing to
   compare. The email's data-feed section gets one line per skipped title, and `data_health`
   raises a signal for it:

   ```
   SKIPPED  Hollow Harbor (appid 123456): no baseline yet.
            Run: python -m daily_job backfill 123456   (est. $0.94, cap $3.00)
   ```
3. **Backfill has its own dollar ceiling and call cap, and stops cleanly.**
   - Before each Claude call, a guard checks `calls_made < STEAM_BACKFILL_MAX_CALLS` and
     `spent + worst_case(call) <= STEAM_BACKFILL_MAX_USD`. The worst case is `max_tokens`
     times the output price plus the estimated input. Per call that is $0.249 for a summary
     (`analyze.py:202`, 12,000 tokens at Opus 5.5 prices) and $0.0075 for a theme call
     (`analyze.py:94`, 8,000 tokens at Haiku 5.5 prices). Spend therefore never exceeds the
     ceiling.
   - The guard needs real usage numbers. Each call site reads `resp.usage`, adds it to a
     `Budget` object, and logs a row to `llm_usage` with `purpose='backfill'`. `get_analysis`
     takes the budget as an optional argument. It raises `BudgetExceeded` before the call,
     not after.
   - A stop is clean because each artifact is saved on its own the moment it finishes
     (`analyze.py:297-298`). A view that stops after its themes has the themes cached. The
     dashboard, or the next backfill, fills in the missing summary and pays only for that call.
   - On a stop, the command prints what was done, what remains, what it spent, and the exact
     command to continue. It sets `status='analyzing'`, not `baselined`.
4. **Priority order, with older windows generated lazily.**

   | Tier | Views | Runs in backfill | Typical cost, medium title |
   |---|---|---|---|
   | P0 | All-time and current release, all languages blended | Always first | $0.27 |
   | P1 | Language views (eligible per `app.py:130-174`) on those two scopes | Then | +$0.27 |
   | P2 | The 3 releases before the current one, blended (`STEAM_BACKFILL_RECENT_RELEASES`) | Then, until a limit hits | +$0.40 |
   | P3 | Every older window, and language views inside windows | Not in the default run | $0.134 per window |

   P3 is lazy. Two triggers fill it in:
   - **On demand from the dashboard.** `/api/analysis` (`app.py:255-286`) already generates a
     missing view when you open it. Keep that for baselined titles.
   - **In small batches in later daily runs** (Section 6.1, step 8). The daily job generates up
     to `STEAM_BACKFILL_LAZY_WINDOWS_PER_RUN` (default 2) missing blended windows, newest
     first, if the daily cap has headroom (7.6). Two windows are about 8 calls and $0.27. A
     large title's 26 remaining windows drain in about 13 days.
5. **Existing titles migrate their saved analyses, so they need no backfill.** The migration
   script (Section 12) copies the `analyses` table. After the copy it sets
   `backfill_state.status='baselined'` for every appid that has a
   `(appid, 'all', 'all', 'summary_product')` row, and prints the titles it left unbaselined.

#### Backfill stages for one title

| Stage | What it does | Cost | Notes |
|---|---|---|---|
| A. Collect history | Walks every review back to launch, per language (`steam_api.iter_reviews`, `steam_api.py:268-319`) | $0 | 100 reviews per page and a 1 s pause (`steam_api.py:299`, `319`). 200k reviews is about 2,000 pages, so 35-60 minutes. The daily step has a 30-minute cap (Section 6.1, step 3). |
| B. Releases | `collect.refresh_releases` (`collect.py:123-162`) | $0 | Windows depend on it, so it runs before stage C. |
| C. Analyses | `analyze.get_analysis` per view, in tier order | 7.4 | Skips a primary view with fewer than `MIN_REVIEWS` (25) reviews (`analyze.py:258-266`). It writes `status='insufficient'` for the all-time view so the title is not skipped forever. |

Two gaps in today's code that backfill has to fix:

- **Collection is not resumable.** `collect_product` stops at `db.last_seen_ts`, which is
  `MAX(timestamp_created)` (`collect.py:175`, `db.py:129-135`). A first run that dies after
  storing the newest 5,000 reviews never fetches the older ones, because the next run stops
  at the first stored review. Fix: stage A sets `stop_before_ts=0` for any language whose
  `runs` row is not marked `complete` (the `status` column in Section 4.6). Review upserts
  are idempotent (`db.py:150-166`), so the rerun costs time only.
- **Non-primary windows have no review floor.** Past releases always generate, even with
  3 reviews (`analyze.py:57-61`, `270`). At about $0.13 a window, thirty patch windows with
  a few reviews each waste money. Backfill skips a window under
  `STEAM_BACKFILL_MIN_WINDOW_REVIEWS` (default 25, the same value as `MIN_REVIEWS`) and
  leaves it for on-demand. The cost table does not model this saving.

### 7.3 What one full first run generates

#### Views

A view is one `(scope, language filter)` pair. Its cache key is `(appid, scope, language_filter, kind)`
(`db.py:75-84`).

- **Scopes:** `all`, plus one per release. The `releases` table includes the `launch` row
  (`db.py:59-70`). Windows run from one release date to the next (`app.py:54-68`), so
  S scopes = 1 + R releases. The latest release is the "current" one (`app.py:269`).
- **Primary scopes** are `all` and the latest release (`app.py:273`). Only they get the
  last-7-days block (`include_recent`, `app.py:278`, `analyze.py:330-340`) and the review
  floors (`enforce_minimums`, `app.py:279`, `analyze.py:268-270`).
- **Language views.** `/api/breakdown` computes eligibility inside each scope window
  (`app.py:198-215`). A language earns its own analysis when it has at least
  `LANG_MIN_VOLUME` (1,000, `app.py:40`) reviews in that window and its percent positive
  differs from the window's blend by at least `LANG_MIN_DIVERGENCE` (8 points,
  `app.py:41`, `149`). The two Chinese variants merge into a `chinese` bucket when the bucket
  qualifies (`app.py:152-171`, `db.py:246`). A language view is a view of its own: it has its
  own cache rows, and its own 7-day block when its scope is primary.
- **Sides:** positive and negative, one theme call each (`analyze.py:313-328`).
- **Audiences:** `product` and `qa`, one summary call each (`analyze.py:346-353`).

#### Calls per view

| Call | Code | Role, model | Effort | `max_tokens` | Sample |
|---|---|---|---|---|---|
| Extract themes, positive side | `_extract_themes`, `analyze.py:85-119` | `extract`, Haiku 5.5 (`models.py:46-52`) | low | 8,000 (`analyze.py:94`) | Up to 150 reviews, top by `votes_up` (`analyze.py:301`, `db.py:353-374`) |
| Extract themes, negative side | same | same | low | 8,000 | Up to 150 |
| Extract 7-day themes, positive (primary views only) | same function, `analyze.py:338` | same | low | 8,000 | Up to 100 from the last 7 days |
| Extract 7-day themes, negative (primary views only) | same, `analyze.py:339` | same | low | 8,000 | Up to 100 |
| Summary, `product` audience | `_write_summary`, `analyze.py:166-225` | `summary`, Opus 5.5 (`models.py:53-59`) | high | 12,000 (`analyze.py:202`) | No reviews. Input is the stats and the 10 themes. |
| Summary, `qa` audience | same, `analyze.py:350-353` | same | high | 12,000 | Same input, different persona |

`_update_themes` (`analyze.py:122-163`, `max_tokens` 8,000) runs only when a cached analysis
exists and `REFRESH_MIN_NEW` (100) new reviews have arrived (`analyze.py:289-294`). A first run
has no cache, so it never calls it. It is a daily-cost item, not a backfill one.

So a **primary view is 6 calls** (4 Haiku, 2 Opus) and a **non-primary view is 4 calls**
(2 Haiku, 2 Opus). A primary view makes fewer than 4 theme calls when a side falls under
`MIN_BUCKET_REVIEWS` (10, `analyze.py:63`, `309-310`), which happens often in 7-day blocks
of small titles and small languages.

The 7-day themes are regenerated once a day anyway (`analyze.py:49`, `334-341`). Backfill
generates them only for the two blended primary views, because the daily report reads them.
For language views it can skip them and save 2 calls each. That saves calls, not dollars:
the Haiku calls cost about $0.002 each.

### 7.4 Cost estimate

#### Prices

| Model | Input $/MTok | Output $/MTok | Notes |
|---|---|---|---|
| Claude Haiku 5.5 (`claude-haiku-5-5`, the `extract` role) | 0.10 | 0.50 | For prompts up to 100K tokens. The largest theme prompt is about 36K. |
| Claude Opus 5.5 (`claude-opus-5-5`, the `summary` role) | 4.00 | 20.00 | Cache reads $0.20. |
| Batch API, either model | half price | half price | Asynchronous. |

`models.resolve` picks the newest `claude-haiku-*` and `claude-opus-*` the API lists
(`models.py:145-162`). Today those are the two models above, which are also the pinned
fallbacks (`models.py:48`, `55`). Output tokens include adaptive-thinking tokens, so they are
billed as output. If a newer Opus appears at a different price, this table goes stale. The
price table in Section 11.1 fails closed on unknown models.

#### Assumptions per call

None of these could be measured, because the repo ships no review data and no API calls were
made. Treat them as the main uncertainty.

| Item | Low | Typical | High |
|---|---|---|---|
| Tokens per sampled review, including the `[n]` and `---` wrapper | 90 (about 300 characters) | 135 (about 500 characters) | 235 (about 900 characters) |
| Why | Short reviews, and thin windows where the sample is not the top-voted reviews | Mixed. The sample is the most-upvoted reviews (`db.py:371`), which skew longer than the median Steam review. | Long reviews, or Haiku 5.5's newer tokenizer, which counts about 30% more tokens for the same text |
| Theme call: fixed input (system plus instructions, `analyze.py:96-111`) | 170 | 170 | 170 |
| Theme call: visible output (5 themes with label, description, count, quote) | 400 | 475 | 600 |
| Theme call: thinking tokens at effort low | 200 | 800 | 2,500 |
| Summary call: input (persona, instructions, scope label, stats, 10 themes as indented JSON; `analyze.py:166-225`, `app.py:71-114`) | 1,300 | 1,550 | 2,100 |
| Summary call: visible output (200-300 words) | 330 | 400 | 500 |
| Summary call: thinking tokens at effort high | 800 | 2,500 | 7,000 |
| Share of reviews that are positive, and share with non-empty text | 90%, 85% | 85%, 90% | 75%, 100% |
| How review volume spreads across release windows | Skewed (launch largest) | Skewed | Even |

No review text is truncated before it goes to the model (`analyze.py:89`, `131`). Steam's own
cap is 8,000 characters (about 2,000 tokens), so one very long review can push a theme call
past the high case, but not past the model's limit.

Title assumptions:

| | Small | Medium | Large |
|---|---|---|---|
| Reviews | 2,000 | 20,000 | 200,000 |
| Releases (R, including `launch`) | 3 | 10 | 30 |
| Scopes (1 + R) | 4 | 11 | 31 |
| Reviews per release window (typical case) | Skewed: 1,092 / 546 / 364 | Skewed: 6,800 down to 680 | Skewed: 50,000 down to 1,700 |
| Reviews in the last 7 days | 20 | 150 | 1,500 |
| Eligible languages, all-time (`app.py:149`) | 0 | 2 | 5 |
| Language views inside release windows | 0 | 0 | 13 old windows plus 2 for the current release |
| Why | A single language of 2,000 reviews cannot diverge from the blend it dominates. | A language needs 1,000 reviews in a window. The average window has 2,000. | The average window has 6,700, so about one of the 5 languages clears both gates in half the windows. |

#### Per-call and per-view cost

| Call | Low | Typical | High |
|---|---|---|---|
| Theme call, 150 reviews: input tokens | 13,700 | 20,400 | 35,400 |
| Theme call, 150 reviews: output tokens | 600 | 1,275 | 3,100 |
| Theme call, 150 reviews: dollars | $0.0017 | $0.0027 | $0.0051 |
| Summary call: input tokens | 1,300 | 1,550 | 2,100 |
| Summary call: output tokens | 1,130 | 2,900 | 7,500 |
| Summary call: dollars | $0.028 | $0.064 | $0.158 |
| Primary view, 6 calls | about $0.06 | about $0.14 | about $0.34 |
| Non-primary view, 4 calls | $0.059 | $0.134 | $0.327 |
| Worst case if every call hits `max_tokens` | | | $0.53 per primary view |

Summary thinking dominates. Each 1,000 summary thinking tokens move a view by $0.04. The theme
calls move a view by less than $0.01 in any case. That is why the ceiling is a dollar ceiling
and the call cap is only a runaway guard.

#### Cost table

Sync means ordinary Messages calls. Batch is the same calls at 50%. Low and high change all
the assumptions above together. Token counts are typical-case totals for the run.

| Title | Run | Views | Calls | Input tokens | Output tokens | Low | **Typical** | High | Typical on Batch |
|---|---|---|---|---|---|---|---|---|---|
| **Small** (2k, R=3) | Priority only: all-time and current | 2 | 10 | 78.6K | 19.3K | $0.12 | **$0.27** | $0.66 | $0.13 |
| | Default run (P0-P2) = full backfill | 4 | 18 | 155.7K | 36.0K | $0.23 | **$0.53** | $1.31 | $0.27 |
| **Medium** (20k, R=10, 2 languages) | Priority only, blended | 2 | 12 | 113.1K | 21.8K | $0.12 | **$0.27** | $0.67 | $0.14 |
| | Priority plus language views (P0+P1) | 4 | 22 | 208.9K | 41.1K | $0.24 | **$0.54** | $1.33 | $0.27 |
| | Default run (P0-P2) | 7 | 34 | 327.0K | 66.1K | $0.41 | **$0.94** | $2.31 | $0.47 |
| | Full backfill | 13 | 58 | 590.6K | 116.2K | $0.77 | **$1.74** | $4.27 | $0.87 |
| **Large** (200k, R=30, 5 languages) | Priority only, blended | 2 | 12 | 142.6K | 21.8K | $0.12 | **$0.28** | $0.67 | $0.14 |
| | Priority plus language views (P0+P1) | 9 | 54 | 571.3K | 98.1K | $0.55 | **$1.23** | $3.01 | $0.62 |
| | Default run (P0-P2) | 12 | 66 | 703.1K | 123.2K | $0.72 | **$1.63** | $3.99 | $0.82 |
| | Full backfill | 51 | 222 | 2.39M | 448.8K | $3.01 | **$6.85** | $16.74 | $3.42 |

How to read the view counts:

- Small: 4 scopes, no language views. The default run already covers everything, because
  only 2 windows precede the current one. The negative 7-day side has about 3 reviews, under
  the floor of 10, so the primary views make 5 calls and not 6.
- Medium: 11 blended scopes (2 primary, 9 older), plus 2 all-time language views. Language
  7-day blocks have only a positive side (about 28 reviews) and skip the negative one, so
  those views make 5 calls.
- Large: 31 blended scopes (2 primary, 29 older), plus 5 all-time language views, 2 language
  views on the current release, and 13 on older windows (51 views total).

Sanity check against the dashboard refresh estimate in Section 13: a primary-view refresh is
"roughly $0.20-$0.50 per busy title". Two primary views with their summaries come to $0.27
typical here, which matches.

#### Options that change the number

- **Run older windows at summary effort `medium`.** Opus 5.5 thinks less at medium than at
  high. A `STEAM_BACKFILL_SUMMARY_EFFORT` override (default: same as `STEAM_SUMMARY_EFFORT`)
  would let P2 and P3 trade quality for cost without touching the dashboard's quality. The
  saving is unmeasured, and quality should not change without your say.
- **One summary call per view for both audiences.** That cuts the Opus bill by about 40%, but it
  changes the output format. Not proposed.

### 7.5 Batch API and prompt caching

**Prompt caching: no.** It would cut under 3% of the bill.

- Theme calls: the cacheable part is the system prompt plus instructions, about 170 tokens. The
  minimum cacheable prefix for Haiku 5.5 is 512 tokens (`claude-api` reference,
  `shared/prompt-caching.md:135`), and the reviews that follow are different on every call.
- Summary calls: `product` and `qa` share a user message of about 1,300 tokens, but the persona
  differs and sits in `system`, which comes first (`analyze.py:205`). They share no prefix. If
  the persona moved to the end of the user message, the second call would read about 1,300
  tokens at $0.20 per MTok instead of $4. That saves about $0.0036 per view after the write
  premium. In a batch, cache hits are best-effort.
- Thinking tokens are about three-quarters of the typical bill and cannot be cached.

**Batch API: yes for the older tiers, no for the priority tiers.**

- It halves all token costs. Backfill is not time-sensitive. The Batch API takes up to
  100,000 requests, finishes most batches within an hour, and has a 24-hour maximum.
- In dollars the saving is small per title: $0.14 on a priority-only run, $0.81 on a large
  default run, $3.42 on a full large backfill. Titles are added rarely, so this is a one-time
  saving.
- It costs real work. The prompts are built inline inside `_extract_themes` and `_write_summary`
  (`analyze.py:92`, `200`), so each must become a function that builds request parameters.
  Summaries depend on themes, so a run is two batches in sequence (Haiku themes, then Opus
  summaries). `messages.parse` is not available, so the code uses `output_config.format` and
  parses the result itself (already planned in Section 6.2). The server-side refusal fallback is
  rejected on the Batches API, so a refused summary needs a sync retry. The command has to
  persist batch IDs in `backfill_state.batch_ids` so a restart resumes polling.
- Waiting up to an hour defeats the point of a priority tier, because P0 is what makes the
  title usable. A P0 run is $0.27 typical.

Recommendation: ship sync first, with the ceilings. Add a batch path later (Phase 2) for P2 and
P3 only, behind `STEAM_BACKFILL_BATCH=1`, and use it only when the run has more than 24 calls
(`STEAM_BACKFILL_BATCH_MIN_CALLS`). Lazy dashboard windows and lazy daily windows stay sync:
they are small and a person or the daily report is waiting on them.

In batch mode the dollar ceiling is enforced on estimates, not on actuals. The command picks
requests in priority order until their estimated cost, with each summary priced at 1.5 times the
typical cost, reaches the ceiling. It then submits them. Actual spend is logged when the results
arrive. It can overshoot only if every summary runs much longer than typical.

### 7.6 Defaults, env vars and the daily cap

#### Recommended defaults

| Env var | Default | Why |
|---|---|---|
| `STEAM_BACKFILL_MAX_USD` | `3.00` | Per title, per backfill invocation. The default run is $1.63 typical for a large title and $3.99 high. $3.00 finishes P0 and P1 in the high case for small and medium titles (a large title needs $3.01, so it stops in P1's last view), and finishes P0-P2 in every typical case with 1.8x margin. A high-case large title stops during P2 and the rest goes lazy. The floor is $1.10: two primary views at the worst case are $1.06, so the command warns below that and cannot promise P0 completes. |
| `STEAM_BACKFILL_MAX_CALLS` | `80` | Per title, per invocation. Covers the large default run (66 calls). It exists to stop a retry loop or an unexpectedly high view count, because dollars bind first in normal use. Count every `messages` call the code makes, including retries it adds itself, not the SDK's internal retries. |
| `STEAM_BACKFILL_RECENT_RELEASES` | `3` | How many releases before the current one P2 covers. |
| `STEAM_BACKFILL_MIN_WINDOW_REVIEWS` | `25` | Skip a window below this many reviews. Same value as `MIN_REVIEWS` (`analyze.py:62`). |
| `STEAM_BACKFILL_LAZY_WINDOWS_PER_RUN` | `2` | Windows the daily job generates per run. About $0.27 typical. |
| `STEAM_BACKFILL_LAZY_MAX_USD` | `0.30` | Dollar cap on lazy generation in one daily run. |
| `STEAM_BACKFILL_BATCH` | `0` | Set to `1` once the batch path exists. |
| `STEAM_BACKFILL_BATCH_MIN_CALLS` | `24` | Smaller runs stay sync. |

Command-line flags override the first two: `--max-usd` and `--max-calls`. `--dry-run` prints
the cost table for the real title using its actual review counts and window sizes, spends
nothing, and exits. Without `--yes`, the command asks for confirmation when the typical
estimate for the selected tiers exceeds the ceiling. `--scope priority|recent|all` picks how
far to go (P0-P1, P0-P2 which is the default, or everything).

These names follow the existing style (`STEAM_ANALYSIS_MIN_NEW`, `analyze.py:44`;
`STEAM_RECENT_REFRESH_HOURS`, `analyze.py:49`). The job caps in Section 11.1 use the same
`STEAM_` prefix.

#### How it interacts with the daily cap

The job API cap limits the job's own Claude calls: `STEAM_DAILY_LLM_CAP_USD` ($0.50/day, code-enforced) and
`STEAM_MONTHLY_LLM_CAP_USD` ($15), checked against `llm_usage`. Spend types sort as follows:

| Spend | `llm_usage.purpose` | Job API daily cap ($0.50) | Job API monthly cap ($15) | Own limit |
|---|---|---|---|---|
| `backfill` command | `backfill` | **Excluded** | **Excluded** | `STEAM_BACKFILL_MAX_USD` and `_MAX_CALLS` per title |
| Lazy windows in the daily run | `backfill_lazy` | Counts | Counts | `STEAM_BACKFILL_LAZY_MAX_USD` per run |
| Dashboard on-demand window | `dashboard` | **Excluded** | Counts | The view's worst case, $0.53 |
| Daily narrative | `narrative` | Counts | Counts | Unchanged |

Why:

- **Backfill is excluded from the daily and monthly caps.** One large backfill is $1.63-$6.85. If
  it counted, it would take the whole $15 month on a medium-to-large title and cut off the
  daily narrative until the next month. The ceiling plus your decision to trigger it are the
  bound. The account backstop still applies: the dedicated Console workspace with a monthly
  limit (Section 11.1). Set that limit to cover $15 plus the backfills you plan.
- **Lazy daily windows count against the daily cap.** They run unattended, so they need the same
  hard bound as everything else the job does on its own. The job runs the narrative first, then
  lazy windows only if the remaining daily headroom is at least $0.30. If it is not, it skips
  them and feed health says "backfill paused: daily cap". A day with a $0.11 narrative leaves
  $0.39, so both windows fit.
- **Dashboard spend does not count against the daily cap.** With on-demand windows, four clicks
  at $0.134 each ($0.54) would use up the $0.50 daily cap and block tonight's narrative. Record
  it with `purpose='dashboard'`, count it toward the monthly cap, and leave the daily cap to
  the unattended job.
- **The agent session cap does not apply.** The $0.75 agent session cap (platform-enforced: the
  runner agent's own tokens and sandbox runtime) does not cover the Python process's API calls.

### 7.7 Dashboard behavior for unbaselined titles

`/api/analysis` currently generates anything the first time you ask (`app.py:255-286`). For a
title with no baseline, opening the dashboard would start a primary view ($0.14) as a side
effect, and twenty clicks would be a surprise backfill. Return
`{"needs_backfill": true, "estimate_usd": ...}` instead, and show the `backfill` command.
Baselined titles keep the on-demand behavior.

### 7.8 Calibrating the estimate

Run these before trusting the cost table:

1. `python -m daily_job backfill <appid> --dry-run`. It reads the real review counts, window
   sizes and eligible languages, and measures `AVG(length(review_text))` over the top-voted
   150 reviews (`db.py:353-374`) per side, replacing the 135-token assumption with a measured one.
2. Run P0 only (`--scope priority`) on the first real title. Compare the logged `llm_usage`
   rows to Section 7.4: input and output tokens per call, and above all summary output tokens.
   Summary thinking is the largest uncertainty, and it moves the answer by $0.04 per view for
   every 1,000 tokens.
3. Rescale the table. If the P0 summary output averages 5,000 tokens, not the 2,900 assumed,
   multiply the typical column by about 1.6.

---

## 8. Detection (deterministic SQL)

All thresholds are env-tunable. All use `timestamp_created` windows. Each signal stores n,
the windows and its metric values as evidence. Titles with no baseline (Section 7) are skipped.

| Detector | Compares | Notable when | Phase |
|---|---|---|---|
| `day_over_day` | Today's snapshot vs yesterday's: new reviews, 7-day % positive | 7-day % moves at least 3 points with n_7d at least 100 | 1 |
| `post_release_shift` | % positive since the latest release vs the 14 days before it | n_post at least 50, change at least 5 points, two-proportion z at least 2 | 1 |
| `review_bomb` | Daily volume and negative share vs the trailing 28-day median | volume at least max(3x median, 30) and negative share at least baseline + 20 points, plus 2 markers: median playtime under 120 min, non-purchase share over 2x baseline, one language over 60% of the spike, median text under 80 characters | 1 (author markers in 2) |
| `language_divergence` | Per language: 14-day gap vs the blend, minus that language's 90-day gap | change at least `LANG_MIN_DIVERGENCE` (8) points with n at least 50 | 1 |
| `emerging_theme` | Share of new negative reviews tagged with a theme, 7 days vs prior 28 | share up at least 10 points with at least 15 mentions | 2 |
| `data_health` | Collection results, build-id source and status, feed freshness, titles with no baseline | any title or language failed; a title is skipped for no baseline; zero new reviews in 72 h where the average is at least 5 per day | 1 |

Fingerprint `detector|appid|scope|language|week` dedupes repeats. A signal re-notifies only
when its severity rises. Below the sample-size floors (the reasoning at `analyze.py:51-55`),
a signal appears in the report but never drives the headline.

---

## 9. Report contents

### 9.1 Sections (full form)

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
   source and status, last successful collect and run duration. Titles skipped for no
   baseline, with the backfill command. "Backfill paused" when the daily cap blocked lazy
   windows. Today's LLM spend vs the cap.

Numbers in every section come from SQL. The model writes only the headline, the
per-title "what changed / why it matters / watch" text and the watch list.

### 9.2 Formats

- One Jinja2 template each for HTML and plain text, sent as a `multipart/alternative`
  email by Resend. Jinja2 already ships with Flask.
- HTML is inline-styled for email clients: a single 600 px column, a table per section,
  no images required, no remote fonts. A chart, if added later, is a hosted image URL or a CID
  inline image. Many clients block remote images, so key numbers stay in the text and tables.
- Model text is inserted escaped. The model never writes HTML.
- Links point to Steam store and news URLs only. A `127.0.0.1` dashboard link works for you
  and not for the other recipients, so add a dashboard link only when `REPORT_PAGE_BASE_URL`
  is set.
- Subject: `[Steam daily] Oct 23: <headline, max 70 chars>`.
- Recipient: `REPORT_TO` from the vault (Section 3). Nothing in the repo names an address.

### 9.3 Example (plain-text part, mock data)

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

### 9.4 "Nothing notable today" (short form, no Opus call)

```
Subject: [Steam daily] Oct 24: nothing notable

Nothing crossed a threshold today.
  Hollow Harbor     +121 reviews   7d positive 76.2% (+1.2)
  Pinecrest Rally    +41 reviews   7d positive 91.0% (-0.3)
Data feeds: all ok (build ids via PICS websocket). Run 5 min 40 s. LLM spend $0.00.
```

### 9.5 Failure form

What failed, which step, the `drun_` or session ID, and whether yesterday's data is still the
latest. It goes to `ALERT_TO` (you), never to the list. The team should not get a failure
notice for a report they never saw.

---

## 10. Report delivery

Facts checked 2026-10-22 (vendor docs where noted, otherwise secondary sources). Re-check
prices before signup.

### 10.1 Decision

Keep email. Phase 1 sends one message to the owner's address (`REPORT_TO`), using Resend's
test sender. When recipients are added, send one message to a Google Group address instead, on
a verified domain (Section 10.7). Membership then lives outside the code. Add an optional Slack
post when the whole team already lives in one Slack workspace.

Two findings drove this:

1. **Webhook URLs cannot go in the vault.** The vault replaces secrets only in HTTP headers and
   bodies. It never replaces a secret in the URL path (the `claude-api` reference names Slack
   incoming-webhook URLs as the example). Slack and Discord webhook URLs carry the secret in
   the path. Teams Workflows URLs carry a signature in the URL (not verified). All three are out
   as written. Slack works with a bot token in the `Authorization` header and
   `chat.postMessage`. Discord works the same way with a bot token (`Authorization: Bot <token>`,
   `POST /channels/{id}/messages`), which means creating a bot and inviting it. Teams has no
   equally clean path.
2. **Resend needs a verified domain for anyone but you.** Resend's docs say you must verify a
   domain to send. Its built-in test sender only delivers to the address on your
   Resend account and returns 403 for anyone else. A domain costs roughly $10-15 per year and
   needs DNS records (SPF, DKIM). That is the main setup cost of the email route.

### 10.2 Comparison

| Option | Works with vault | Setup for a small non-developer team | Cost at 1 message/day, 2-20 people | Rich content | Archive and search | People added or removed without code | Failure visibility |
|---|---|---|---|---|---|---|---|
| **Resend to one list address** | Yes. Key in header, `REPORT_TO` in body. | Verify a domain (DNS), create the list. One-time, about an hour. | Free tier: 100 emails/day, 3,000/month. Each To/CC/BCC recipient counts as one email, so a list address counts as one. | Full HTML tables. Chart as inline image or hosted link. | Each person's mailbox. List archive if the Group keeps one. | Yes, in the Group UI. | API returns an error and an email id. Healthchecks catches silent loss. |
| Resend direct to N addresses | Yes, but N addresses sit in one secret. | Same. | Free tier covers 20 recipients (20/day of 100). | Same. | Same. | No. Edit the secret each time. | Same. |
| Postmark | Yes. | Domain or sender signature. | Free plan reported at 100 emails per month, too small for a daily list. Paid from about $15/month. | Same. | Message log in dashboard. | Same as list approach. | Strong delivery logs. |
| SendGrid | Yes. | Sender verification. | Reported: no permanent free plan, 60-day trial, then paid from about $19.95/month. | Same. | Activity feed. | Same. | Good. |
| Amazon SES | Weak. Header auth needs SigV4 signing, which is awkward for a vault. | Domain plus leaving the sandbox. Highest effort. | About $0.10 per 1,000 emails. | Same. | Needs extra setup. | Same. | Needs CloudWatch. |
| Brevo | Yes. API key in header. | Sender verification, domain recommended. | Reported free tier: 300 emails/day. | Same. | Dashboard logs. | Contact lists work, but the daily send needs the list address anyway. | Good. |
| Mailgun | Yes. | Domain. | Reported free tier: 100/day, 1-day log retention (sources conflict). | Same. | Short log retention on free. | Same. | Short logs hurt debugging. |
| Gmail SMTP with app password | No. SMTP is not HTTP, so the vault cannot fill the password. Port 587 may also be blocked under `limited` networking. | App password needs 2-Step Verification. No domain needed. | Free. Personal Gmail: about 500 recipients/day, 100 per message over SMTP (secondary sources). | Full HTML. | Sent folder of that Gmail. | With a list address, yes. | Weak. Over-limit errors can block sending up to 24h. |
| Google Group as the recipient | Not a sender. Pair with any option above. | Create the Group, allow outside posting. A personal Google account can create one. A Workspace admin may need to enable outside posting. | Free. | Whatever the email carries. | Group archive. | Yes, best of all options. | Depends on the sender. Spam filtering can drop mail to a Group, so test it. |
| **Slack bot token and `chat.postMessage`** | Yes. Token in `Authorization` header. Needs a Slack app and a bot invited to the channel. | Create the app, add scopes, invite the bot. About 30 minutes. | Free. | Block Kit sections and fields. Keep text blocks near 4,000 characters (Slack truncates past 40,000). No real table: use code blocks or fields. Charts need a hosted image URL. | Free workspaces see only the last 90 days. Older items are hidden, and items over a year old are deleted. | Yes. Join or leave the channel. | 1 message/second/channel limit is no concern. API error is visible. A missing post is visible to everyone. |
| Slack incoming webhook | No. Secret is in the URL path. | Easy. | Free. | Same blocks. | Same 90-day limit. | Yes. | Weak: failure only shows as an HTTP status. |
| Discord webhook | No for the same reason. Bot token variant works. | Easy. | Free. | 2,000 characters of text. Up to 10 embeds, 6,000 characters in total. No tables. | Full history kept. | Yes. | 5 requests per 2 seconds. Rejects oversize payloads. |
| Teams Workflows webhook | No (signature in URL, not verified). | Needs the Workflows app allowed by the Teams admin. | Free with a Microsoft account that has Teams. | Adaptive Card. About 28 KB per post. Oversize posts fail. | Teams history. | Yes. | Weak. |
| Hosted report page plus a short notice | The upload to storage works with a header key. The notice still needs one of the options above. | Storage bucket and a public or signed URL. GitHub Pages is free for public repos only. A private site needs Enterprise Cloud. | A free storage tier covers it. | Best: charts, filters, full tables. | Best: one page per day, permanent, searchable. | Access control is the problem (10.3). | You see a missing file. |

### 10.3 Hosted page: access trade-off

A report page gives you charts and a permanent history that email and chat cannot match. It
cannot be private for free. A public URL with a long random token in the path is "unlisted",
not secret. Anyone with the link can read it, and links get forwarded. The Steam review data
is public, so the exposure is low. The narrative may mention internal priorities. If that
matters, keep the full report in the email body and skip the hosted page. The `reports` table
already stores the rendered HTML, so a later page costs little extra.

### 10.4 Recommendation

Use **Resend**. Phase 1 sends one email to the owner's address (`REPORT_TO`) with Resend's test
sender. When recipients are added, the email goes to a Google Group address on a verified domain.
Reasons:

- It fits the vault with no change to the design. `REPORT_TO` is a body secret.
- People join or leave in the Group page. No redeploy.
- It supports tables and a chart image with no length trouble.
- The free tier is enough: one send per day, one recipient from Resend's point of view.
- It reaches recipients outside your company, which a Slack channel cannot do.

What to choose by audience:

| Recipients | Choice |
|---|---|
| Everyone is in one Slack workspace | Post to a Slack channel with a bot token (`chat.postMessage`). Skip the domain and the Group. Accept the 90-day history limit on the free plan. Add email to your own address for a permanent copy. |
| Everyone is in one Discord server | Same idea with a Discord bot token. History is kept. No tables, so use embeds and a hosted chart image. |
| Mixed, with outside people | Email to the Group. Add Slack only for people who want a chat ping. |
| No domain and no wish to buy one | Use Slack, or a Brevo single-sender setup with weaker deliverability. Do not use Gmail SMTP from the sandbox. |

One message to the list is simpler than one per person and counts once toward the quota.
Per-person sends allow per-person unsubscribe, which a Group handles itself (members leave in
the Group). Sending to a Group also avoids exposing addresses, which a plain To line would.

### 10.5 Configuration (no addresses in code)

| Name | Where | Purpose |
|---|---|---|
| `DELIVERY_CHANNELS` | Plain config in `config.json` or env | `email`, `slack`, or `email,slack` |
| `RESEND_API_KEY` | Vault, host `api.resend.com`, header | Email send |
| `REPORT_TO` | Vault, host `api.resend.com`, body | The owner's address now; the Google Group address later |
| `REPORT_FROM` | Plain config | Resend's test sender until a domain is verified; then a sender on that domain |
| `ALERT_TO` | Vault, host `api.resend.com`, body | Your own address, never the list, for failure alerts |
| `SLACK_BOT_TOKEN` | Vault, host `slack.com`, header | Only if Slack is on. Add `slack.com` to `allowed_hosts`. |
| `SLACK_CHANNEL_ID` | Plain config | Channel identifier. Not a secret. |
| `REPORT_PAGE_BASE_URL` | Plain config, optional | Link target if a hosted page or dashboard link is added later |

If both channels are on, the job treats the report as sent when at least one succeeds and
records each result in `job_runs` (`email_id`, `slack_ts`). Call Resend and Slack with plain
`requests`, so no client library checks the placeholder's format.

### 10.6 What to verify before building

- That your Group accepts mail from your sending domain (outside-sender setting, admin policy),
  and that mail to it does not land in spam for an outside member.
- Current Resend free-tier numbers on resend.com/pricing (100/day, 3,000/month as of 2026-10-22).
- Whether `slack.com` substitution works for the `Authorization` header on a test post, if you
  pick Slack.
- The Teams and Discord vault behavior, if you pick either. Neither was tested.

Failure alerts and the watchdog are in Section 11.2.

### 10.7 When you add recipients

Phase 1 needs none of this. Do these steps before the first recipient other than you gets
the report:

1. Buy or reuse a domain you control (about $10-$15 per year). Add it in Resend, add the SPF
   and DKIM DNS records, and wait for "Verified".
2. Set `REPORT_FROM` to a sender on that domain.
3. Create the Google Group, set "Who can post" so the sending domain can post, and invite the
   recipients. Write down who owns the Group and who owns the domain renewal.
4. Change `REPORT_TO` in the vault to the Group address. No code changes.
5. Send a test and check the spam folder for an outside member.

Sources (checked 2026-10-22): Resend sending limits (resend.com/docs/knowledge-base/resend-sending-limits),
domain requirement (resend.com/docs/dashboard/domains/introduction), test domain 403
(resend.com/docs/knowledge-base/403-error-resend-dev-domain); Slack free plan limits
(slack.com/help/articles/27204752526611), incoming webhooks and `chat.postMessage`
(docs.slack.dev); Teams connector message size (learn.microsoft.com/en-us/connectors/teams/);
GitHub Pages limits (docs.github.com); vault behavior from the `claude-api` skill
(`shared/managed-agents-tools.md`, Vaults). Free-tier figures for Postmark, SendGrid, SES,
Brevo, Mailgun and Gmail limits come from secondary comparison blogs. Confirm on each
vendor's pricing page.

---

## 11. Guardrails

### 11.1 Spend caps

- **Agent session cap ($0.75, platform-enforced: the runner agent's own tokens and sandbox
  runtime):** the deployment `budget` of $0.75 is copied onto each session at list price. At the cap the
  session pauses (`budget_reached`). It does not cover API calls that Python makes with the
  vaulted key, because those are separate Messages API requests.
- **Job API cap ($0.50/day, code-enforced: the Python job's own Claude calls):** `STEAM_DAILY_LLM_CAP_USD` (default $0.50) and
  `STEAM_MONTHLY_LLM_CAP_USD` (default $15), checked against `llm_usage` before every call.
  Over the cap, the job sends the templated report and says so in feed health. The daily cap
  counts the narrative and lazy backfill windows. It excludes the `backfill` command and
  dashboard on-demand analyses. The monthly cap excludes only the `backfill` command (table in
  Section 7.6).
- **Backfill cap:** `STEAM_BACKFILL_MAX_USD` ($3.00) and `STEAM_BACKFILL_MAX_CALLS` (80) per
  title, enforced by the `Budget` guard (Section 7.2).
- **Price table:** keyed by exact model ID. `models.resolve` (`models.py:145-162`) can pick
  a newer model at runtime. If the resolved ID has no price entry, fail closed (no LLM
  calls) rather than spend at an unknown rate.
- **Account backstop:** use a dedicated Console workspace and API key for the job, with a
  monthly spend limit. Set it to cover $15 plus the backfills you plan.

The two meters are separate, not nested. A normal day's worst case is $1.25 ($0.75 agent
session cap plus $0.50 job API cap), and $2.00 if the agent retries in a second session.
Expected spend stays about $0.10-$0.30 per day.

### 11.2 When a run fails

The watchdog is a **Healthchecks.io** check (Free: 20 checks). It is a dead-man's switch
outside both Anthropic and the data store, so it also catches a full store outage.

- Create one check with the same cron expression and time zone as the deployment (`31 8 * * *`, `America/Los_Angeles`), and a
  3-hour grace period. Add an email integration (confirm it is on the free plan at signup).
- `daily_job` pings `<ping-url>/start` at step 0, `<ping-url>` only after the report is marked
  `emailed` (step 10), and `<ping-url>/fail` on exit 1.
- The ping URL carries its secret in the path, so the vault cannot substitute it. It is the
  one value handed to the job through the deployment's `initial_events`. It is low-value
  configuration. The worst case if it leaks: someone sends fake pings and hides one missed
  day. If you want no secret at all in the session history, use a Cloudflare Worker cron
  instead, which pulls from the store with keys held as Worker secrets.

| Failure | Who notices | What you get |
|---|---|---|
| A step fails inside the sandbox | `daily_job` (exit 1), then the agent | Failure email to `ALERT_TO` via `notify-failure`, plus the `/fail` ping |
| The session never starts (`session_rate_limited`, `vault_not_found`, archived environment) | Healthchecks | "Missed ping" email from Healthchecks |
| The session hits its budget or hangs | Healthchecks | Same |
| Every build-id source fails for a title | `daily_job` (`build_status = unavailable`) | The report still goes out. Feed health lists the title, and the report does not claim "no update". |
| Turso is down | `daily_job` at preflight (exit 1) | `notify-failure` needs only Resend, so the failure email still goes out, and Healthchecks alerts too. |
| Resend is down | Healthchecks (no success ping) | "Missed ping" email. Add a Slack post to a private owner channel as a second route if you want a faster signal. |
| Healthchecks is down | Nobody, from inside | Accept this for a personal tool. A missing report is the signal. |

Rules for alerts:

- **The alert goes to `ALERT_TO` (you), not the list.** If you want the team told, add a
  one-line "report delayed" message to the list on day 2.
- **Check the deployment record.** `client.beta.deployment_runs.list(deployment_id=...,
  has_error=True)` finds firings that never started (rate limit, pause). Run it from your side
  or in a small weekly check.
- **The alert text** says: date, last status in `job_runs`, last error, session id, and the
  command to re-run (`client.beta.deployments.run(id)`).
- **Delivery receipts:** record the Resend email id. Resend's dashboard shows delivered,
  bounced and complained, which you read when a person says they got nothing.
- **Quota check:** the job logs a warning when a send returns a 429 from Resend. That means the
  daily or monthly quota is hit.
- Optional (Phase 2): a small webhook handler (for example a Cloudflare Worker) subscribed to
  Managed Agents webhooks (`deployment_run.failed`, `session.status_terminated`) for faster
  alerts.

### 11.3 Secrets

| Secret | Lives in | Seen by sandbox? |
|---|---|---|
| Job `ANTHROPIC_API_KEY`, `TURSO_AUTH_TOKEN`, `RESEND_API_KEY`, `REPORT_TO`, `ALERT_TO`, optional `SLACK_BOT_TOKEN` | Managed Agents vault, host-limited | Placeholder only |
| Healthchecks ping URL | Deployment `initial_events` (the one exception; secret in the URL path) | Yes. Low value. |
| GitHub PAT (Contents: Read) | Deployment `github_repository` resource | No (git proxy) |
| Your admin Anthropic key, Turso token for your PC | Your PC (`.env`, `ant auth login`) | n/a. The `backfill` command and the dashboard use these. |
| Plain config: `REPORT_FROM`, `DELIVERY_CHANNELS`, `SLACK_CHANNEL_ID` | `config.json` | Yes. Not secrets. |

Nothing else secret goes in the repo, the agent prompt or `initial_events`. `.env` stays
gitignored (`.gitignore`). Each vaulted value is set in one place.

### 11.4 Review text is untrusted input

- Review text reaches exactly one place: the narrative call. That call has no tools and
  uses structured output. Excerpts are wrapped in delimited data blocks, and the system
  prompt says they are data, not instructions.
- The agent with `bash` never sees review text. The status JSON carries counts only.
- Model output is validated: every number must appear in the facts bundle, every quoted
  review ID must exist in the window, and text length is capped. Output is escaped into
  the template. On a failed check, the job drops the narrative and keeps the numbers.
- The recipient is not a model output. It is substituted at egress from the vault.

---

## 12. Data migration (one time, from your PC)

1. Disable the Windows task so the file stops changing:
   `schtasks /Change /TN "SteamReviewCollector" /DISABLE`.
2. Copy `reviews.db`. On the copy, run `PRAGMA wal_checkpoint(TRUNCATE);`, confirm that
   `PRAGMA journal_mode;` returns `wal` (Turso requires WAL, and `db.connect()` already sets
   it), and run `PRAGMA integrity_check;`. Check the file size (Section 4.3). Turso Free holds
   5 GB.
3. Create the database from the file: `turso db create steam --from-file reviews.db` (the file
   limit is 2 GB). The Turso CLI runs on Windows through WSL. If you do not want WSL, use a
   `migrate_to_turso.py` script that copies each table in batches of 500 through `db_turso.py`,
   skipping existing `recommendationid`s so it can resume. That script also tests the adapter.
4. Apply the new tables (Section 4.6) by opening one `db.connect()` against Turso. The schema
   runs idempotently on connect.
5. Verify per `(appid, language)` on both sides: row count, `SUM(voted_up)`,
   `MAX(timestamp_created)`. Also compare row counts for `products`, `releases`, `runs` and
   `analyses`. Any difference fails the migration.
6. Set `backfill_state.status='baselined'` for every appid that has a saved summary (Section
   7.2, item 5), and note the titles left unbaselined.
7. Create the two tokens (Section 4.4). Point the local dashboard at Turso (`DATABASE_URL`,
   `TURSO_AUTH_TOKEN` in `.env`) and check a few views against the local copy.
8. Run the Phase 0 spike and the `[TEST]` week against a copy:
   `turso db create steam-test --from-db steam`. Delete it afterwards.
9. After the first good cloud run, delete the Windows task
   (`schtasks /Delete /TN "SteamReviewCollector" /F`), so two collectors do not write. Keep
   `reviews.db` as a cold backup.

For the Supabase runner-up (Section 4.9), the migration is the same shape, run through Postgres
functions against the `steam` schema.

---

## 13. Cost estimate

Prices from the `claude-api` reference: Claude Opus 5.5 $4 input, $20 output, $0.20 cache read
per million tokens; Claude Haiku 5.5 $0.10 / $0.50 (prompts up to 100K); Managed Agents
session runtime $0.08 per hour. Token counts below are estimates. Check them against
`llm_usage` and the session's `usage.list_cost` after the first week.

### Steady state (daily run)

| Item | Per day | Per month (30 days) |
|---|---|---|
| Session runtime, about 10 min | $0.014 | $0.40 |
| Agent loop, Opus 5.5 effort low, 3-6 turns (about 30K input mostly cached, 3K output with thinking) | $0.08-$0.15 | $2.40-$4.50 |
| Narrative, Opus 5.5 effort medium (about 12K in, 3K out), only on days with signals | $0.00-$0.11 | $1.00-$3.30 |
| Theme tagging, Haiku 5.5 (Phase 2, about 500 new reviews per day) | $0.01 | $0.30 |
| **Anthropic total** | **about $0.10-$0.30** | **about $4-$9** |
| Turso | Free plan $0 (decided: 1-day restore plus weekly dumps; Developer $4.99 is the paid option) | $0 |
| Resend | Free tier $0 (verify current limits) | $0 |
| Sending domain | | $0 now (Resend test sender); about $1 ($10-$15 per year) once recipients are added |
| Healthchecks.io | Free tier $0 | $0 |
| GitHub | $0 | $0 |
| **Total** | | **about $5-$10** |

If Slack replaces email, drop the domain line. The Supabase runner-up (Section 4.9) would cost
$0 on Free or $25 on Pro, in place of the Turso line.

### One-time backfill (per new title, typical, sync)

| Title | Default run (P0-P2) | Full backfill | High case (default / full) |
|---|---|---|---|
| Small (2k reviews) | $0.53, 18 calls | $0.53 | $1.31 / $1.31 |
| Medium (20k) | $0.94, 34 calls | $1.74, 58 calls | $2.31 / $4.27 |
| Large (200k) | $1.63, 66 calls | $6.85, 222 calls | $3.99 / $16.74 |

Backfill and dashboard on-demand analyses are excluded from the daily cap (Section 7.6).
Lazy older windows in the daily run add up to $0.27 per day for about 13 days on a large
title (up to about $3.50 in total). They are part of that title's full-backfill figure. The
Batch API would halve these numbers (Phase 2).

### Options that change the number

- Agent on Haiku 5.5: saves about $2-$4 per month.
- Pre-warming the dashboard's primary views (`PREWARM_PRIMARY_VIEWS=1`) adds the Opus
  summary cost (`analyze.py:346-353`) each time a view crosses `REFRESH_MIN_NEW`
  (`analyze.py:44`). Roughly $0.20-$0.50 per busy title per refresh. Off by default.

Anthropic API usage is billed to your Console account. It does not come out of a claude.ai
subscription.

---

## 14. Findings from the old plan that still apply

- **Edited reviews are never re-fetched.** `iter_reviews` walks `filter=recent` newest-first
  and stops at the last stored `timestamp_created` (`steam_api.py:270-319`, stop at
  `310-314`). A player who flips a review after a patch is missed, and that is exactly the
  post-patch signal. Phase 2 adds a periodic `filter=updated` sweep (confirm the parameter).
- **Failed collections leave no `runs` row.** On `SteamError`, `collect_product` commits,
  logs and `continue`s past `db.log_run` (`collect.py:190-193`). Add `status` and `error`
  columns and write the row on both paths. Feed health and backfill resumption depend on it.
- **`LANG_MIN_VOLUME` drift.** `app.py:40` defaults to 1000, `README.md:194` says 1000, and
  `.env.example:43` documents 500. Pick one value when the helpers move to `scopes.py`, so
  the detector and the dashboard agree.
- **Five copies of the DB path lookup:** `collect.py:41`, `app.py:44`, `clear_cache.py:30`,
  `export.py:25`, `setup.py:30`. Replace them with one `config.py` that also reads
  `DATABASE_URL` and `TURSO_AUTH_TOKEN`.
- **`author.num_reviews` and `num_games_owned` are dropped** in `upsert_review`
  (`db.py:149-183`). They are useful review-bomb markers. Add the columns in Phase 2.
- **News posts act as releases today.** `refresh_releases` stores every `patchnotes` post as a
  release, and `record_public_build` skips a build that falls within 36 hours of one
  (`collect.py:62-76`, `108-116`, `123-162`). Both rules go away once build ids are the only
  release signal (Phase 0, item 7). Existing release rows that came from news posts stay as
  history by default.
- Dropped from the old plan as local-only: Task Scheduler cadence, lock files, the
  sleeping-PC heartbeat, Windows toasts, the hourly patch watch, and the dashboard outbox.

---

## 15. Phased rollout

Effort assumes one developer who knows this codebase. These are estimates.

### Phase 0: spike and refactors (about 2-3 days)

1. **Spike first (about 1 day, a decision gate).** Create the environment, vault, agent and a
   paused deployment. Fire a manual run with a throwaway script that reports:
   - the sandbox Python version, and whether `uv python install 3.12` works;
   - whether `pip install steam gevent-eventemitter "protobuf<3.21"` works;
   - whether the CM WebSocket upgrade passes under `limited` with `*.steamserver.net` allowed,
     and whether `docs/spikes/ws_pics.py` logs in anonymously and reads appid 730's public
     `buildid` (compare with `api.steamcmd.net/v1/info/730`). Only if that fails, repeat under
     `unrestricted` as a test, and try raw TCP to a CM server there. If the WebSocket path still
     fails in the sandbox, `api.steamcmd.net` becomes the primary build-id source, and the GitHub
     Actions PICS job is not built as a fallback;
   - whether `api.steamcmd.net/v1/info/730` is reachable under `limited`;
   - whether vault placeholders get substituted on HTTPS calls to `*.turso.io` (in
     `Authorization`), `api.resend.com` and `api.anthropic.com` from Python `requests` and the
     `anthropic` SDK;
   - how long a 500-statement Turso pipeline request takes, and how values come back typed in
     the JSON (integers arrive as strings);
   - whether a Resend send from the test sender reaches the owner's address, and whether
     `hc-ping.com` is reachable under `limited`;
   - whether `fallbacks="default"` combines with structured output.

   Write the results into this doc.
2. `config.py`: one DB path and env loader, with the `DATABASE_URL` switch.
3. Move `_resolve_window`, `_scope_label` and `_language_eligibility` plus `LANG_MIN_*` out
   of `app.py:40-41`, `54-174` into `scopes.py` with no Flask import. `app.py` imports them.
   Fix the default drift.
4. Replace `analyze._basic_stats` with a `review_stats` aggregate (Section 4.5).
5. `db_turso.py` (about 150 lines) and the `db.connect()` dispatch. Add the new tables
   (Section 4.6) to `db.SCHEMA`.
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
8. `models.py`: add the `narrative` role. Usage logging with a `purpose` at every call site.
   Move `analyze.py:114` and `158` from `output_format` to `output_config.format`.
9. `tests/` with a synthetic SQLite fixture and `pytest`. Run the same function tests against
   the Turso adapter on a throwaway Turso database.

Exit criteria: local SQLite use behaves exactly as before; tests pass on SQLite and on Turso.

### Phase 1: MVP daily email, plus backfill (about 6-9 days)

- `migrate_to_turso.py` (or the CLI import), run once, with verification.
- `daily_job.py` (steps in 6.1), `ops/daily.sh`, `requirements-cloud.txt`.
- Detectors: `day_over_day`, `post_release_shift`, `review_bomb` (existing columns),
  `language_divergence`, `data_health`.
- Narrative call, validation, Jinja templates (full, quiet and failure forms), Resend send to
  REPORT_TO, the owner's address for now (and the Slack post if chosen).
- `job_runs` idempotency, spend caps with `purpose` accounting, Healthchecks pings.
- Backfill (about 2-3 days): `backfill_state`, the `backfill` command with `--dry-run`, the
  `Budget` guard and `BudgetExceeded`, the resumable-collection fix, the window review floor,
  lazy daily windows, and the dashboard `needs_backfill` response.
- `agents/steam-daily/` files for `ant apply`: environment, agent, deployment.
- Dashboard on `DATABASE_URL`, plus `sync_down.py`.
- One week of runs with the subject prefixed `[TEST]`. Then tune thresholds and drop the prefix.

### Phase 2: anomaly depth and themes (about 4-6 days)

- Haiku 5.5 theme tagging of new reviews into `theme_catalog` and `theme_mentions`,
  seeded from the cached `themes_bad` entries. Adds `emerging_theme`.
- `filter=updated` sweep for edited reviews.
- Author markers in `review_bomb`.
- Optional: GitHub Actions PICS job as a third build-id path. It is not part of the chain, so
  build it only if a third path is wanted.
- Batch API path for backfill P2 and P3 (Section 7.5).
- Webhook-based failure alerts (optional).
- Detector backtest over history ("as of day D") to tune false alarms.

The total is about the same as the earlier Postgres plan. Turso saves about 3 days of backend
work, and backfill and delivery setup add about the same.

### What you need to set up yourself

- [ ] Anthropic Console: a workspace for this job with a monthly spend limit, and an API
      key in it. A separate key for the dashboard is optional.
- [ ] The `ant` CLI, then `ant auth login` on your PC (used to create the agent and deployment).
- [ ] Turso: an account on the Free plan. Install the CLI (WSL on Windows) and run
      `turso auth login`. Pick a US location unless you know where the sandbox runs.
- [ ] Turso: create `steam` from `reviews.db` and pass the verification in Section 12, step 5.
- [ ] Turso: one database token for the job and one for your PC. No organization tokens.
- [ ] Backups (decided: Turso Free): a weekly dump from your PC (`turso db shell steam .dump > steam-YYYYMMDD.sql`;
      confirm `.dump` in the CLI version you install). The Developer plan is the paid option if that changes.
- [ ] Resend: an account and an API key. Phase 1 sends with Resend's test sender, which
      delivers only to the owner's address on the account.
- [ ] Optional Slack: create a Slack app with the `chat:write` scope, install it, invite the bot
      to the channel, and store the token in the vault.
- [ ] Healthchecks.io: one check (cron `31 8 * * *`, time zone `America/Los_Angeles`, 3-hour grace) with email alerts. Put the ping
      URL in the deployment's `initial_events`.
- [ ] GitHub: a fine-grained PAT with `Contents: Read` on this repo only, and a branch
      `agent-stable` for the deployment.
- [ ] Managed Agents vault credentials, each limited to its host: `ANTHROPIC_API_KEY` (header),
      `TURSO_AUTH_TOKEN` (header), `RESEND_API_KEY` (header), `REPORT_TO` (body), `ALERT_TO`
      (body), and `SLACK_BOT_TOKEN` (header) if Slack is on.
- [ ] Managed Agents environment `allowed_hosts`: `store.steampowered.com`,
      `api.steampowered.com`, `*.steamserver.net`, `api.steamcmd.net`, `steam-<org>.turso.io`,
      `api.anthropic.com`, `api.resend.com`, `hc-ping.com`, and `slack.com` if Slack is on.
- [ ] Optional, later: a GitHub Actions workflow and a repo secret for the Turso token, only if
      a third build-id path is wanted (no other sign-up is needed for build ids).
- [ ] Run the one-time migration from your PC. Delete the Windows scheduled task after
      the first good cloud run.
- [ ] For each new title: run `python -m daily_job backfill <appid> --dry-run`, then the real run.

#### Later, when adding recipients

- [ ] Resend: buy or reuse a domain you control, add it in Resend, add the SPF and DKIM DNS
      records, and wait for "Verified". Set `REPORT_FROM` to a sender on that domain.
- [ ] Google Group: create it, set "Who can post" so the sending domain can post, and invite the
      recipients. Send a test and check the spam folder for an outside member.
- [ ] Write down who owns the Group and who owns the domain renewal.
- [ ] Change `REPORT_TO` in the vault to the Group address.

---

## 16. Resolved decisions

- **Send time:** 09:00 America/Los_Angeles. The job starts at 08:31, and the IANA zone follows
  daylight saving time (Section 3).
- **Recipient:** the owner only for now. Phase 1 sends with Resend's test sender to the owner's
  address on the Resend account, so no domain and no Google Group are needed. `REPORT_TO` holds
  the owner's address. The Google Group plus verified domain are a step for when recipients are
  added (Section 10.7).
- **Build ids:** if the Phase 0 WebSocket test fails, `api.steamcmd.net` (community mirror) is the
  primary source. The GitHub Actions PICS job is an optional later option.
- **Backups:** Turso Free (1-day restore plus weekly dumps).
- **Caps:** agent session cap $0.75 (platform-enforced); job API cap $0.50/day and $15/month
  (code-enforced); backfill $3.00 and 80 calls per title (Section 11.1).

Open:

- Confirm the cap amounts after the first week of real usage logs (`llm_usage`, and the
  session's usage).
