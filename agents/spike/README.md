# Phase 0 spike: test the cloud sandbox from your PC

This tests the one thing the plan cannot answer on paper: what the Managed Agents sandbox
lets the daily job do. It creates a throwaway agent, runs one script inside the sandbox,
and gives you a JSON report. Plan reference: `docs/AGENT_PLAN.md`, Section 15, Phase 0, item 1.

- **Time:** about 30 minutes the first time (most of it creating accounts), then 5 to 12 minutes per run.
- **Cost:** under $0.50. The agent session is capped at $1.00 by the platform. The API calls
  inside the script cost a few cents.
- **Secrets:** you type them at silent prompts. They go straight to the Anthropic vault. They
  are never written to a file in this repository.
- **Your email address:** you type it yourself at one prompt. It is never in a file.

You run everything inside **Ubuntu on WSL** (Windows Subsystem for Linux). The plan already
assumes WSL for the Turso command-line tool, and the `ant` installer documents Linux and WSL.

## 1. Open Ubuntu on WSL

If you do not have it, open PowerShell as administrator, run this, restart, and create a
Linux user name and password when Ubuntu starts:

```powershell
wsl --install
```

Then, in the Ubuntu window:

```bash
sudo apt update && sudo apt install -y python3 curl
```

## 2. Install the `ant` command

In the Ubuntu window:

```bash
VERSION=2.1.0
curl -fsSL "https://github.com/anthropics/anthropic-cli/releases/download/v${VERSION}/ant_${VERSION}_linux_amd64.tar.gz" | sudo tar -xz -C /usr/local/bin ant
ant --version
```

You should see a version number. A newer version is fine; the releases page is
`https://github.com/anthropics/anthropic-cli/releases`. On an ARM Windows PC, replace `amd64` with `arm64`.

## 3. Sign in

```bash
ant auth login --no-browser
```

Open the link it prints in your Windows browser. Choose your organization and the workspace
you plan to use for this job. Then check:

```bash
ant auth status
```

If you have set `ANTHROPIC_API_KEY` in this Ubuntu window, it overrides the login. Run
`unset ANTHROPIC_API_KEY` first.

## 4. Create the accounts and keys

Collect these three things. Keep them in a password manager until step 6. You paste them
into prompts. Do not put them in a file or in a chat.

**a. Anthropic API key.** In the Anthropic Console, open your job workspace, then API keys,
then create a key named `steam-spike`. It starts with `sk-ant-`.

**b. Turso database host and token.** In the Ubuntu window:

```bash
curl -sSfL https://get.tur.so/install.sh | bash
exec bash -l
turso auth login --headless
turso db create steam
turso db show steam --url
turso db tokens create steam
```

- `turso db show steam --url` prints something like `libsql://steam-yourname.aws-us-east-1.turso.io`.
  Keep that line. It is the **host**, and it is not a secret.
- `turso db tokens create steam` prints the **token**. It is a secret.
- If `turso db create` asks for a location, choose one in the US.
- If you have no Turso account yet, `turso auth login --headless` walks you through creating one (Free plan).

**c. Resend API key.** Sign up at `https://resend.com` **using the email address that should receive
the reports**. Open API Keys, create a key with "Sending access", and copy it. The Resend test
sender delivers only to the address on your Resend account. That address is what you type for
`REPORT_TO` in step 6.

You can skip any of these. The checks that need a skipped item report `skip`, and you can
run again later.

## 5. Go to the repository folder

Your Windows drives appear under `/mnt/c`, `/mnt/d` and so on. For example, if the repository
is `C:\Users\YourName\Projects\Steam_Aggregator`:

```bash
cd /mnt/c/Users/YourName/Projects/Steam_Aggregator
sed -i 's/\r$//' agents/spike/run_spike.sh
```

The `sed` line removes Windows line endings from the script, which would otherwise break it.

## 6. Run the spike (the main command)

```bash
bash agents/spike/run_spike.sh
```

What you will see, in order:

1. It shows who you are signed in as. Type `y`.
2. `ant` prints a plan to create an environment, an agent and a vault, and asks to apply. Type `y`.
3. It asks for each secret. Keys are not shown as you type. Press Enter alone to skip one.
   - Anthropic API key (step 4a)
   - Turso token (step 4b)
   - Resend API key (step 4c)
   - Your own email address (the Resend account address; this one is shown as you type)
4. It asks for the Turso host (the part of the URL after `libsql://`).
5. It uploads the two scripts, creates a **paused** deployment, and fires one manual run.
6. It waits and prints a status line every 20 seconds. Then it saves the report and prints a summary.

The report is saved in `agents/spike/results/sandbox_report_limited_<date>.json`. That folder is
git-ignored. The report contains no secrets and no email addresses.

To watch the agent work, open a second Ubuntu window and run the `ant beta:sessions connect ...`
line the script prints.

If the script stops waiting before the run ends, collect the result later with:

```bash
bash agents/spike/run_spike.sh --fetch sesn_PASTE_THE_SESSION_ID_HERE
```

## 7. Only if the WebSocket check failed: the unrestricted re-test

The plan says to repeat the test with unrestricted networking only when the WebSocket path
fails under `limited` (see the table in step 8). Then run:

```bash
bash agents/spike/run_spike.sh --unrestricted
```

It reuses the vault secrets already stored. It asks only for the Turso host again. It saves a second
report named `sandbox_report_unrestricted_<date>.json`. It also tries raw TCP to a Steam server
(the `raw_tcp_cm` check) and expects `example.com` to be reachable.

## 8. What each result means for the plan

Open the saved JSON, or read the table the script printed. Each check has `status`
(`pass`, `fail` or `skip`) and a `detail` line. Start with `summary` at the bottom.

| Check | If it fails or looks like this | What it means |
|---|---|---|
| `ws_upgrade`, `steam_ws_pics` | Both pass, and the build id matches `steamcmd_reachable`. | Plan stands. PICS over WebSocket is source 1, the mirror is source 2 (Section 5.2). |
| `ws_upgrade` or `steam_ws_pics` | Fail under `limited`, pass under `--unrestricted`. | The sandbox allowlist blocks the WebSocket upgrade. Section 3 says no `unrestricted` networking, so `api.steamcmd.net` becomes the primary build-id source (Decisions, Section 5.2, Section 16). Do not build the GitHub Actions PICS job. |
| `ws_upgrade` or `steam_ws_pics` | Fail under both modes. | Same result: `api.steamcmd.net` is the primary source. |
| `steam_ws_pics` | Passes but the build id differs from the mirror. | Run again first, because a build may have landed between the two reads. If it still differs, the mirror lags. Keep it as a fallback only, with the stale-answer rule in Section 5.4. |
| `steamcmd_reachable` | Fails, and the WebSocket path also fails. | The sandbox has no build-id source. Then the optional GitHub Actions PICS job becomes necessary. Stop and send the JSON to Claude. |
| `uv_python312`, `pip_steam` | The image already has Python 3.12 (`sandbox_info`) and `pip_steam` passes. | Nothing to add. |
| `uv_python312` | Fails and the image has no Python 3.12. | Add the three GitHub hosts (commented in `environment.yaml`) to `allowed_hosts`, run again, and add them to Section 3 of the plan. |
| `pip_steam` | Fails. | The `steam` stack cannot be installed in the sandbox. Read `data.output_tail`. Use the mirror as primary, or switch to Node `steam-user` (Section 5.2). |
| `vault_turso` | `pass`. Note `data.value_typing`. | The token is substituted in the header. Integers arrive as `{"type": "integer", "value": "<text>"}`, so `db_turso.py` must convert them. |
| `vault_turso` | Fails with 401 or 403. | The vault did not substitute the token for `*.turso.io`, or the token is for another database. Re-check the token and the host. |
| `vault_turso` | Fails with 404 "Host not found". | The Turso host you typed is wrong. |
| `turso_latency_500` | Pass, with `pipeline_runs[].seconds`. | This is the real cost of a 500-statement batch. If it is more than a few seconds, use smaller batches in `db_turso.py`. |
| `vault_resend` | `pass`. | Both the header key and the `REPORT_TO` body value were substituted. Check your inbox for `[TEST] sandbox check`. If it is not there after 5 minutes, look in spam and in the Resend dashboard under Emails. |
| `vault_resend` | Fails with 401. | The `RESEND_API_KEY` header was not substituted, or the key is wrong. |
| `vault_resend` | Fails with 422. | The `REPORT_TO` body value was not substituted. Body substitution is not working. Send the JSON to Claude; the recipient settings in Section 3 and Section 10.5 need a rethink. |
| `vault_resend` | Fails with 403 and "own email". | The auth works, but `REPORT_TO` is not the Resend account address. Run the script again and retype it. |
| `vault_anthropic_requests`, `vault_anthropic_sdk` | Pass. | The job can call Claude from the sandbox with a vaulted key. |
| `vault_anthropic_sdk` | Fails while `vault_anthropic_requests` passes. | The SDK rejects the placeholder. The job's `models.py` must call the API with `requests` for now. |
| `vault_*` | All fail with 401. | The vault is not substituting at all. The plan's design depends on it. Stop and send the JSON to Claude. |
| `fallbacks_structured` | `pass`. | `fallbacks="default"` and structured output work together. Keep both in the narrative call (Section 11.4). |
| `fallbacks_structured` | Fails with 400. | The API rejects the combination. Send the narrative call without `fallbacks` and handle `stop_reason: "refusal"` in code (the exact error is in `data.error`). |
| `hc_ping` | `pass`. | `hc-ping.com` is reachable under `limited`. If it fails, check the host name when you sign up for Healthchecks.io. |
| `control_blocked_host` | Fails under `limited` (example.com reachable). | The allowlist is not enforced the way the plan assumes. Treat the other "reachable" results with care and send the JSON to Claude. |
| `raw_tcp_cm` | Information only. | Raw TCP to a Steam server. The plan does not rely on it. |

## 9. Send the result back

Paste the contents of the saved JSON file(s) back into the Claude Code chat. The script removes
secret values and email addresses before it writes anything. Claude then records the findings in
`docs/AGENT_PLAN.md`.

## 10. Clean up (optional)

The spike resources cost nothing while idle. When you finish Phase 0 you can archive them
(`archive` cannot be undone):

```bash
ant beta:deployments archive --deployment-id DEPLOYMENT_ID
ant beta:environments archive --environment-id ENVIRONMENT_ID
```

The IDs are in `agents/spike/claude-lock.json`. In the Anthropic Console, revoke the `steam-spike`
API key. In Turso, `turso db tokens invalidate steam` revokes its tokens.

## If something goes wrong

| Message | Fix |
|---|---|
| `$'\r': command not found` | Run the `sed -i` line from step 5 again. |
| `ant: command not found` | Redo step 2. |
| `ant is not signed in` | Redo step 3. |
| `ant apply` says a resource was changed outside these files | Do not add `--force`. Send Claude what it printed. |
| The run ends but no report file exists | The script prints the agent's own messages instead. Paste them into the chat. |
| A secret was typed wrong | In the Anthropic Console, archive the credential in the `steam-spike` vault, then run the script again. It asks only for the missing ones. |

## What is in this folder

| File | What it is |
|---|---|
| `run_spike.sh` | The one command you run. |
| `environment.yaml` | `limited` networking with the plan's allowed hosts. |
| `environment-unrestricted.yaml` | The re-test environment (`unrestricted`). |
| `agent.md` | The runner agent: Opus 5.5 at low effort, `bash` and `read` only, web tools off, no approvals. |
| `vault.yaml` | The empty vault. `run_spike.sh` adds the four secrets. |
| `deployment-spike.template.yaml` | The deployment template. The script fills in the IDs and writes `.run/deployment-spike-<mode>.yaml`. It has a yearly schedule and is paused on creation. |
| `../../docs/spikes/sandbox_check.py` | The script that runs inside the sandbox. |
| `../../docs/spikes/ws_pics.py` | The WebSocket PICS test it calls. |

Do not run `ant apply` on the whole `agents/spike` folder or on the template. `run_spike.sh`
names the files it needs.
