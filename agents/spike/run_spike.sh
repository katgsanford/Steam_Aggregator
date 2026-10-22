#!/usr/bin/env bash
# Phase 0 spike runner (docs/AGENT_PLAN.md, Section 15, item 1). Run it in WSL, macOS or Linux.
#
#   bash agents/spike/run_spike.sh                  # limited networking (the plan's setup)
#   bash agents/spike/run_spike.sh --unrestricted   # the re-test, only if limited failed
#   bash agents/spike/run_spike.sh --fetch sesn_... # just collect the result of an earlier run
#
# What it does: applies the environment, agent and vault, asks you for the secrets at a
# silent prompt and stores them in the vault, uploads the two scripts, creates a PAUSED
# deployment, fires one manual run, waits for it, and saves the JSON report under
# agents/spike/results/. Secrets never go into a file, a command line or this terminal's
# history. Full steps and what each outcome means: agents/spike/README.md.

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
LOCK="$HERE/claude-lock.json"
MODE=limited
TURSO_HOST=""
FETCH_SID=""
MAX_WAIT=1500   # seconds to wait for the run

say()  { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die()  { printf '\nSTOP: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --unrestricted) MODE=unrestricted ;;
    --turso-host)   TURSO_HOST="${2:-}"; shift ;;
    --fetch)        FETCH_SID="${2:-}"; shift ;;
    -h|--help)      sed -n '2,13p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

if [ "$MODE" = unrestricted ]; then ENV_FILE=environment-unrestricted.yaml; else ENV_FILE=environment.yaml; fi

lock_id() {  # lock_id <file-name-suffix>  ->  the ID ant apply recorded for that file
  python3 - "$LOCK" "$1" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for k, v in d.get("resources", {}).items():
    if k.endswith(sys.argv[2]):
        print(v["id"])
        break
PY
}

finish() {  # finish <session-id>: wait, download the report, print a digest
  local sid="$1" waited=0 status="" fid="" out="" ts
  say "Waiting for session $sid (up to $((MAX_WAIT / 60)) minutes)"
  note "Watch it live in a second terminal with:  ant beta:sessions connect $sid"
  while [ "$waited" -lt "$MAX_WAIT" ]; do
    status="$(ant beta:sessions retrieve --session-id "$sid" --transform status -r 2>/dev/null || true)"
    printf '   status: %-12s (%ss)\n' "${status:-unknown}" "$waited"
    case "$status" in
      terminated) break ;;
      idle) [ "$waited" -ge 40 ] && break ;;   # ignore an idle seen in the first seconds, before the sandbox starts
    esac
    sleep 20; waited=$((waited + 20))
  done
  case "$status" in
    idle|terminated) ;;
    *) note "The run is still going. Collect it later with:  bash agents/spike/run_spike.sh --fetch $sid"; return 1 ;;
  esac
  mkdir -p "$HERE/results"
  ts="$(date +%Y%m%d-%H%M%S)"
  out="$HERE/results/sandbox_report_${MODE}_${ts}.json"
  for _ in 1 2 3 4 5 6; do   # output files appear 1-3 seconds after the session goes idle
    fid="$(ant beta:files list --scope-id "$sid" --beta managed-agents-2026-04-01 \
           --transform '{id,filename}' --format jsonl 2>/dev/null \
           | python3 -c 'import sys,json
for l in sys.stdin:
    try:
        d=json.loads(l)
    except Exception:
        continue
    if d.get("filename")=="sandbox_report.json":
        print(d["id"]); break')"
    [ -n "$fid" ] && break
    sleep 4
  done
  if [ -n "$fid" ] && ant beta:files download --file-id "$fid" --output "$out" >/dev/null 2>&1; then
    say "Report saved: $out"
    python3 - "$out" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("   mode: %s, %s seconds" % (d.get("mode"), d.get("total_seconds")))
for c in d["checks"]:
    print("   %-5s %-26s %s" % (c["status"].upper(), c["id"], c["detail"][:150]))
s = d.get("summary", {})
print("\n   counts: %s" % s.get("counts"))
print("   build id source if decided now: %s" % s.get("build_id_source_if_decided_now"))
print("   fallbacks=default with structured output: %s" % s.get("fallbacks_default_with_structured_output"))
for n in s.get("notes", []):
    print("   note: " + n)
PY
    note "Read agents/spike/README.md, step 8, for what each result means."
    return 0
  fi
  say "No report file found. Showing what the agent printed instead"
  ant beta:sessions:events list --session-id "$sid" --type agent.message --transform 'content.0.text' -r 2>/dev/null | tail -n 200
  note "If that is empty too, open the session in the Console or run: ant beta:sessions connect $sid"
  return 1
}

# ---- preflight --------------------------------------------------------------------------
command -v ant >/dev/null     || die "the 'ant' command is not installed. See README step 1."
command -v python3 >/dev/null || die "python3 is not installed. In WSL Ubuntu run: sudo apt install -y python3"

if ! ant models list --max-items 1 >/dev/null 2>&1; then
  die "ant is not signed in. Run:  ant auth login --no-browser   then run this script again."
fi

if [ -n "$FETCH_SID" ]; then finish "$FETCH_SID"; exit $?; fi

for f in "$REPO/docs/spikes/sandbox_check.py" "$REPO/docs/spikes/ws_pics.py"; do
  [ -f "$f" ] || die "missing $f. Run this from a full copy of the repository."
done

say "You are signed in as:"
ant auth status 2>&1 | sed 's/^/   /'
printf '\nThis creates resources in the organization and workspace shown above. Continue? [y/N] '
read -r ans
case "$ans" in y|Y|yes|YES) ;; *) die "cancelled." ;; esac

# ---- 1. environment, agent, vault ---------------------------------------------------------
say "1/6  Create the environment ($MODE), the agent and the vault"
note "ant shows a plan and asks y/n. Answer y."
( cd "$HERE" && ant apply --lock-file "$LOCK" "$ENV_FILE" agent.md vault.yaml ) || die "ant apply failed."
VID="$(lock_id /vault.yaml)"
[ -n "$VID" ] || die "could not find the vault ID in $LOCK."

# ---- 2. vault credentials -----------------------------------------------------------------
say "2/6  Store the secrets in the vault"
note "Type each value and press Enter. Nothing is shown on screen for keys. Press Enter alone to skip one;"
note "the checks that need it will then report 'skip'. A value already in the vault is kept."
EXISTING="$(ant beta:vaults:credentials list --vault-id "$VID" --transform auth.secret_name -r 2>/dev/null || true)"

add_cred() {  # add_cred NAME HOSTS_JSON INJECTION_JSON PROMPT SILENT(1/0)
  local name="$1" hosts="$2" inj="$3" label="$4" silent="$5" val=""
  if printf '%s\n' "$EXISTING" | grep -qx "$name"; then
    note "$name is already in the vault. Keeping it."
    return 0
  fi
  printf '\n%s (Enter to skip): ' "$label"
  if [ "$silent" = 1 ]; then read -rs val; echo; else read -r val; fi
  if [ -z "$val" ]; then note "Skipped $name."; return 0; fi
  # Build the JSON body first and hand it to ant as a here-string. `ant` looks at stdin the moment
  # it starts, so a slow producer in a pipe makes it fail with 'Required flag "auth" not set'.
  # The secret lives only in shell variables here, never in a file or on a command line.
  local body
  body="$(CMA_VALUE="$val" python3 - "$name" "$hosts" "$inj" <<'PY'
import json, os, sys
name, hosts, inj = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
print(json.dumps({
    "display_name": name,
    "auth": {"type": "environment_variable", "secret_name": name,
             "secret_value": os.environ["CMA_VALUE"],
             "networking": {"type": "limited", "allowed_hosts": hosts},
             "injection_location": inj},
}))
PY
)"
  if ant beta:vaults:credentials create --vault-id "$VID" --transform id -r >/dev/null <<<"$body"; then
    note "Stored $name."
  else
    note "WARNING: could not store $name. Read the message above."
  fi
  body=""
  val=""
}

add_cred ANTHROPIC_API_KEY '["api.anthropic.com"]' '{"header": true}' \
  "Anthropic API key for the spike (sk-ant-...; use a key from a separate workspace)" 1
add_cred TURSO_AUTH_TOKEN '["*.turso.io"]' '{"header": true}' \
  "Turso database token" 1
add_cred RESEND_API_KEY '["api.resend.com"]' '{"header": true}' \
  "Resend API key" 1
add_cred REPORT_TO '["api.resend.com"]' '{"body": true}' \
  "Your own email address, the one on your Resend account (shown as you type)" 0

if [ -z "$TURSO_HOST" ]; then
  printf '\nTurso database host, for example steam-yourname.aws-us-east-1.turso.io (not a secret; Enter to skip): '
  read -r TURSO_HOST
fi
TURSO_HOST="${TURSO_HOST#libsql://}"; TURSO_HOST="${TURSO_HOST#https://}"; TURSO_HOST="${TURSO_HOST%/}"
[ -n "$TURSO_HOST" ] || TURSO_HOST=none

# ---- 3. upload the scripts ------------------------------------------------------------------
say "3/6  Upload the two scripts"
F1="$(ant beta:files upload --file "$REPO/docs/spikes/sandbox_check.py" --expires-in-seconds 604800 --transform id -r)" || die "upload failed."
F2="$(ant beta:files upload --file "$REPO/docs/spikes/ws_pics.py" --expires-in-seconds 604800 --transform id -r)" || die "upload failed."
note "sandbox_check.py -> $F1"
note "ws_pics.py       -> $F2"

# ---- 4. paused deployment ---------------------------------------------------------------------
say "4/6  Create the deployment (paused) for the $MODE environment"
mkdir -p "$HERE/.run"
RUN_FILE=".run/deployment-spike-$MODE.yaml"
python3 - "$HERE/deployment-spike.template.yaml" "$HERE/$RUN_FILE" "$MODE" "$ENV_FILE" "$VID" "$F1" "$F2" "$TURSO_HOST" <<'PY'
import sys
src, dst, mode, env_file, vid, f1, f2, host = sys.argv[1:9]
t = open(src).read()
for k, v in {"REPLACE_MODE": mode, "REPLACE_ENV_FILE": env_file, "REPLACE_VAULT_ID": vid,
             "REPLACE_CHECK_FILE_ID": f1, "REPLACE_WS_FILE_ID": f2, "REPLACE_TURSO_HOST": host}.items():
    t = t.replace(k, v)
open(dst, "w").write(t)
PY
( cd "$HERE" && ant apply --lock-file "$LOCK" "$RUN_FILE" ) || die "ant apply failed for the deployment."
DID="$(lock_id "/deployment-spike-$MODE.yaml")"
[ -n "$DID" ] || die "could not find the deployment ID in $LOCK."
ant beta:deployments pause --deployment-id "$DID" >/dev/null 2>&1 && note "Deployment $DID is paused (it never fires on its own)." \
  || note "Could not pause $DID (it may already be paused). Its schedule is 1 January only, so this is harmless."

# ---- 5. fire one manual run ---------------------------------------------------------------------
say "5/6  Fire one manual run"
SID="$(ant beta:deployments run --deployment-id "$DID" --transform session_id -r 2>/dev/null || true)"
case "$SID" in sesn_*|session_*) ;; *) SID="$(ant beta:deployment-runs list --deployment-id "$DID" --max-items 1 --transform session_id -r 2>/dev/null || true)" ;; esac
case "$SID" in sesn_*|session_*) ;; *) die "the run did not return a session ID. Check: ant beta:deployment-runs list --deployment-id $DID" ;; esac
note "Session: $SID"
note "Console:  https://platform.claude.com/workspaces/default/sessions/$SID   (use your workspace ID instead of 'default' if you made a separate workspace)"

# ---- 6. wait and collect --------------------------------------------------------------------------
say "6/6  Wait for the result"
finish "$SID"
