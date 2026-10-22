# Phase 0 spike (docs/AGENT_PLAN.md, Section 15, item 1). Throwaway. Not production code.
#
# Runs inside a Managed Agents sandbox and prints one JSON report. Every check is
# independent: it has its own timeout, records pass / fail / skip plus detail, and a
# failure never stops the rest. A check whose secret or setting is missing is skipped.
# Secret values are never printed: the report scrubber replaces any value of the
# secret environment variables, and every email-shaped string, before output.
#
# Usage (inside the sandbox, normally started by the spike agent):
#   python3 sandbox_check.py --turso-host <db-host>.turso.io --mode limited
#   python3 sandbox_check.py --mode unrestricted --turso-host <host>   # the re-test
#
# Environment variables (the vault puts opaque placeholders here; the real values are
# substituted into outbound HTTPS requests at egress):
#   ANTHROPIC_API_KEY   header   api.anthropic.com
#   TURSO_AUTH_TOKEN    header   <db>.turso.io
#   RESEND_API_KEY      header   api.resend.com
#   REPORT_TO           body     api.resend.com
#
# The script is standard library only until it has installed `requests` and `anthropic`
# into a virtualenv; it then re-runs itself with that interpreter and continues.
# Works offline for the checks that need no network: with nothing configured it still
# reports the environment checks and skips the vault checks.

import argparse
import base64
import datetime
import json
import os
import platform
import re
import shutil
import signal
import socket
import ssl
import statistics
import subprocess
import sys
import time
from urllib.parse import urlparse

SPIKE_VERSION = 1
WORK = os.environ.get("SPIKE_WORK_DIR", "/tmp/spike")
PARTIAL = os.path.join(WORK, "partial.json")
SECRET_NAMES = ["ANTHROPIC_API_KEY", "TURSO_AUTH_TOKEN", "RESEND_API_KEY", "REPORT_TO"]
APPID = 730
STEAM_PKGS = ["steam", "gevent-eventemitter", "protobuf<3.21"]
ANTHROPIC_VERSIONS = ["2026-10-21", "2023-06-01"]
FALLBACK_BETA = "server-side-fallback-2026-07-01"
CM_TCP_HOST = ("cmp1-lax1.steamserver.net", 27017)
# Resend's public test sender, built from parts so no address literal sits in the file.
RESEND_FROM = "onboarding" + "@" + "resend.dev"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

START = time.time()
CHECKS = []
CTX = {}


# ----------------------------------------------------------------------------- helpers

def log(msg):
    sys.stderr.write("[sandbox_check +%5.1fs] %s\n" % (time.time() - START, msg))
    sys.stderr.flush()


def scrub(value):
    """Remove secret values and email addresses from any string inside value."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        for name in SECRET_NAMES:
            secret = os.environ.get(name, "")
            if len(secret) >= 6:
                value = value.replace(secret, "<%s>" % name)
        return EMAIL_RE.sub("<email>", value)
    return value


def tail(text, n=700):
    text = (text or "").strip()
    return text[-n:]


def run(cmd, timeout, env=None, cwd=None):
    """Run a command. Returns (returncode, stdout, stderr). rc is None on timeout."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        return None, out, err
    except FileNotFoundError as e:
        return 127, "", str(e)


class CheckTimeout(Exception):
    pass


def _on_alarm(signum, frame):
    raise CheckTimeout()


def save_partial():
    try:
        os.makedirs(WORK, exist_ok=True)
        CTX["t0"] = START
        with open(PARTIAL, "w") as f:
            json.dump({"checks": CHECKS, "ctx": CTX}, f)
    except Exception:
        pass


def check(cid, timeout, fn, skip_reason=None, why=""):
    """Run one check. fn returns (status, detail, data). Never raises."""
    t0 = time.time()
    if skip_reason:
        status, detail, data = "skip", skip_reason, {}
    else:
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(int(timeout) + 15)
        try:
            status, detail, data = fn()
        except CheckTimeout:
            status, detail, data = "fail", "timed out (hard limit %ss)" % (int(timeout) + 15), {}
        except subprocess.TimeoutExpired:
            status, detail, data = "fail", "a command timed out", {}
        except Exception as e:  # one failure must not stop the rest
            status, detail, data = "fail", "%s: %s" % (type(e).__name__, tail(str(e), 400)), {}
        finally:
            signal.alarm(0)
    res = scrub({
        "id": cid,
        "status": status,
        "detail": detail,
        "data": data,
        "seconds": round(time.time() - t0, 2),
        "tests": why,
    })
    CHECKS.append(res)
    log("%-28s %-5s %s" % (cid, status, str(res["detail"])[:110]))
    save_partial()
    return res


def can_import(*mods):
    for m in mods:
        try:
            __import__(m)
        except Exception:
            return False
    return True


def proxy_info():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return None
    p = urlparse(proxy)
    return {"host": p.hostname, "port": p.port, "has_userinfo": bool(p.username)}


def venv_python(venv):
    return os.path.join(venv, "bin", "python")


def make_venv(py, venv):
    """Create a virtualenv with interpreter py. Returns (ok, note)."""
    shutil.rmtree(venv, ignore_errors=True)
    rc, out, err = run([py, "-m", "venv", venv], 120)
    if rc == 0 and os.path.exists(venv_python(venv)):
        return True, "venv"
    uv = shutil.which("uv")
    if uv:
        rc2, out2, err2 = run([uv, "venv", "--python", py, venv], 120)
        if rc2 == 0:
            return True, "uv venv"
        return False, "venv failed (%s); uv venv failed (%s)" % (tail(err, 200), tail(err2, 200))
    return False, "venv failed: %s" % tail(err, 300)


def pip_install(venv, pkgs, timeout):
    """Install into venv with pip (uv pip when the venv has no pip). Returns (ok, output)."""
    py = venv_python(venv)
    rc, out, err = run([py, "-m", "pip", "--version"], 30)
    if rc == 0:
        cmd = [py, "-m", "pip", "install", "--disable-pip-version-check", "-q"] + pkgs
    elif shutil.which("uv"):
        cmd = [shutil.which("uv"), "pip", "install", "--python", py] + pkgs
    else:
        return False, "no pip in venv and no uv"
    rc, out, err = run(cmd, timeout)
    if rc is None:
        return False, "pip timed out after %ss: %s" % (timeout, tail(err, 300))
    return rc == 0, tail(err or out, 600)


# ----------------------------------------------------------------------------- stage 1: stdlib only

def chk_sandbox_info():
    secrets = {}
    for n in SECRET_NAMES:
        v = os.environ.get(n)
        secrets[n] = {"set": bool(v), "length": len(v) if v else 0}
    os_name = ""
    try:
        for line in open("/etc/os-release"):
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    data = {
        "python": sys.version.split()[0],
        "python_is_3_12": sys.version_info[:2] == (3, 12),
        "executable": sys.executable,
        "os": os_name or platform.platform(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count(),
        "uv_on_path": shutil.which("uv"),
        "python3_12_on_path": shutil.which("python3.12"),
        "git_on_path": bool(shutil.which("git")),
        "https_proxy": proxy_info(),
        "ca_env": {k: bool(os.environ.get(k)) for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")},
        "anthropic_base_url_env": bool(os.environ.get("ANTHROPIC_BASE_URL")),
        "secret_env": secrets,
        "env_var_names": sorted(k for k in os.environ.keys()),
    }
    rc, out, err = run([sys.executable, "-m", "pip", "--version"], 20)
    data["pip"] = tail(out, 120) if rc == 0 else "not available"
    ok = [n for n, s in secrets.items() if s["set"]]
    return "pass", "python %s; secrets present: %s" % (data["python"], ", ".join(ok) or "none"), data


def chk_pip_requests_anthropic():
    if can_import("requests", "anthropic"):
        import anthropic
        import requests
        CTX["http_python"] = sys.executable
        return "pass", "already installed (requests %s, anthropic %s)" % (requests.__version__, anthropic.__version__), {}
    venv = os.path.join(WORK, "venv_http")
    ok, note = make_venv(sys.executable, venv)
    if not ok:
        return "fail", "could not create a virtualenv: " + note, {}
    ok, out = pip_install(venv, ["requests", "anthropic"], 240)
    if not ok:
        return "fail", "pip install requests anthropic failed: " + out, {}
    rc, vout, verr = run([venv_python(venv), "-c",
                          "import requests, anthropic; print(requests.__version__, anthropic.__version__)"], 60)
    if rc != 0:
        return "fail", "installed but import failed: " + tail(verr, 300), {}
    CTX["http_python"] = venv_python(venv)
    return "pass", "installed into %s (requests, anthropic = %s)" % (venv, vout.strip()), {"venv": venv}


def chk_uv_python312():
    uv = shutil.which("uv")
    data = {"uv_was_preinstalled": bool(uv)}
    if not uv:
        venv = os.path.join(WORK, "venv_uv")
        ok, note = make_venv(sys.executable, venv)
        if ok:
            ok, out = pip_install(venv, ["uv"], 180)
        if not ok:
            return "fail", "uv is not installed and pip install uv failed", data
        uv = os.path.join(venv, "bin", "uv")
    CTX["uv"] = uv
    env = dict(os.environ, UV_HTTP_TIMEOUT="20", UV_PYTHON_DOWNLOADS="automatic")
    already = sys.version_info[:2] == (3, 12) or bool(shutil.which("python3.12"))
    data["python_3_12_already_present"] = already
    rc, out, err = run([uv, "python", "install", "3.12"], 150, env=env)
    data["uv_install_returncode"] = rc
    if rc == 0:
        rc2, out2, err2 = run([uv, "python", "find", "3.12"], 30, env=env)
        if rc2 == 0 and out2.strip():
            CTX["python312"] = out2.strip().splitlines()[-1]
            data["python312_path"] = CTX["python312"]
        return "pass", "`uv python install 3.12` works", data
    data["output_tail"] = tail(err or out, 600)
    if already:
        return "fail", ("`uv python install 3.12` failed, but python 3.12 is already in the image, so it is not needed. "
                        "(GitHub release hosts are not in allowed_hosts.)"), data
    return "fail", ("`uv python install 3.12` failed and the image has no python 3.12. Add the GitHub release hosts "
                    "(github.com, release-assets.githubusercontent.com, objects.githubusercontent.com) to allowed_hosts."), data


def pick_python312():
    if sys.version_info[:2] == (3, 12):
        return sys.executable
    return CTX.get("python312") or shutil.which("python3.12")


def chk_pip_steam():
    py = pick_python312()
    data = {"used_python312": bool(py)}
    if not py:
        py = sys.executable
        data["warning"] = "no python 3.12 available; trying the sandbox python %s" % sys.version.split()[0]
    venv = os.path.join(WORK, "venv_steam")
    ok, note = make_venv(py, venv)
    if not ok:
        return "fail", "could not create a virtualenv: " + note, data
    ok, out = pip_install(venv, STEAM_PKGS, 240)
    if not ok:
        data["output_tail"] = out
        return "fail", "pip install %s failed" % " ".join(STEAM_PKGS), data
    rc, vout, verr = run([venv_python(venv), "-c",
                          "import steam, gevent, google.protobuf as p; "
                          "print(steam.__version__, gevent.__version__, p.__version__)"], 60)
    if rc != 0:
        data["output_tail"] = tail(verr, 500)
        return "fail", "installed but import failed", data
    CTX["steam_python"] = venv_python(venv)
    data["versions_steam_gevent_protobuf"] = vout.strip()
    return "pass", "installed (steam, gevent, protobuf = %s)%s" % (
        vout.strip(), "" if data["used_python312"] else " on a non-3.12 python"), data


# ----------------------------------------------------------------------------- stage 2: needs requests

def chk_steamcmd(args):
    import requests
    t0 = time.time()
    r = requests.get("https://api.steamcmd.net/v1/info/%d" % APPID, timeout=(10, 30))
    el = round(time.time() - t0, 2)
    if r.status_code != 200:
        return "fail", "HTTP %s from api.steamcmd.net" % r.status_code, {"http_status": r.status_code}
    j = r.json()
    app = (j.get("data") or {}).get(str(APPID)) or {}
    pub = ((app.get("depots") or {}).get("branches") or {}).get("public") or {}
    bid, tu = pub.get("buildid"), pub.get("timeupdated")
    if not bid:
        return "fail", "reachable, but no depots.branches.public.buildid in the answer", {"keys": list(j.keys())}
    CTX["mirror_buildid"] = str(bid)
    CTX["mirror_timeupdated"] = str(tu)
    return "pass", "reachable; appid %d public buildid %s" % (APPID, bid), {"buildid": str(bid), "timeupdated": str(tu), "seconds": el}


def cm_ws_hosts(limit=4):
    import requests
    r = requests.get("https://api.steampowered.com/ISteamDirectory/GetCMListForConnect/v1/",
                     params={"cellid": 0, "cmtype": "websockets", "format": "json"}, timeout=(10, 20))
    r.raise_for_status()
    out = []
    for s in r.json()["response"]["serverlist"]:
        host, port = s["endpoint"].rsplit(":", 1)
        if port == "443" and s["type"] == "websockets":
            out.append(host)
    return out[:limit]


def tls_connect(host, port=443, timeout=15):
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        p = urlparse(proxy)
        sock = socket.create_connection((p.hostname, p.port), timeout=timeout)
        hdr = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (host, port, host, port)
        if p.username:
            tok = base64.b64encode(("%s:%s" % (p.username, p.password or "")).encode()).decode()
            hdr += "Proxy-Authorization: Basic %s\r\n" % tok
        sock.sendall((hdr + "\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("proxy closed the connection")
            buf += chunk
        first = buf.split(b"\r\n", 1)[0].decode("latin1")
        if " 200" not in first:
            raise OSError("proxy CONNECT refused: %s" % first[:80])
    else:
        sock = socket.create_connection((host, port), timeout=timeout)
    ctx = ssl.create_default_context()
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if cafile:
        ctx.load_verify_locations(cafile)
    return ctx.wrap_socket(sock, server_hostname=host)


def chk_ws_upgrade(args):
    try:
        hosts = cm_ws_hosts()
    except Exception as e:
        return "fail", "could not read the CM list from api.steampowered.com (%s: %s)" % (type(e).__name__, tail(str(e), 200)), {}
    if not hosts:
        return "fail", "the CM list had no websocket hosts on port 443", {}
    tried = []
    for h in hosts:
        t0 = time.time()
        try:
            s = tls_connect(h)
            key = base64.b64encode(os.urandom(16)).decode()
            s.sendall(("GET /cmsocket/ HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (h, key)).encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            s.close()
            line = buf.split(b"\r\n", 1)[0].decode("latin1")
            tried.append({"host": h, "result": line[:80], "seconds": round(time.time() - t0, 2)})
            if " 101 " in line:
                return "pass", "WebSocket upgrade accepted by %s (101 Switching Protocols)" % h, {"attempts": tried}
        except Exception as e:
            tried.append({"host": h, "error": "%s: %s" % (type(e).__name__, tail(str(e), 160))})
    return "fail", "no CM host accepted the WebSocket upgrade on port 443", {"attempts": tried}


def chk_steam_ws_pics(args):
    py = CTX.get("steam_python")
    if not py:
        return "skip", "the steam packages are not installed (see pip_steam)", {}
    script = args.ws_pics
    if not os.path.exists(script):
        return "fail", "ws_pics.py not found at %s" % script, {}
    t0 = time.time()
    rc, out, err = run([py, script, str(APPID)], 120, cwd=WORK)
    el = round(time.time() - t0, 2)
    data = {"seconds": el, "returncode": rc}
    m = re.search(r"^%d buildid (\d+) timeupdated (\d+)" % APPID, out or "", re.M)
    sline = re.search(r"^server (.*)$", out or "", re.M)
    if sline:
        data["server_line"] = sline.group(1)[:120]
    if rc is None:
        data["output_tail"] = tail((out or "") + (err or ""), 600)
        return "fail", "ws_pics.py hung for 120s (login or PICS never completed)", data
    if rc != 0 or not m:
        data["output_tail"] = tail((err or "") + (out or ""), 800)
        return "fail", "ws_pics.py did not return a build id (exit %s)" % rc, data
    bid, tu = m.group(1), m.group(2)
    data.update({"buildid": bid, "timeupdated": tu})
    mirror = CTX.get("mirror_buildid")
    if not mirror:
        data["compared_with_mirror"] = False
        return "pass", "anonymous login and PICS worked; appid %d public buildid %s (mirror not available to compare)" % (APPID, bid), data
    data["mirror_buildid"] = mirror
    data["matches_mirror"] = (mirror == bid)
    if mirror == bid:
        return "pass", "appid %d public buildid %s matches api.steamcmd.net" % (APPID, bid), data
    return "fail", ("buildid differs: websocket %s, mirror %s. Re-run to rule out a build pushed between the two reads."
                    % (bid, mirror)), data


def chk_raw_tcp(args):
    host, port = CM_TCP_HOST
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=8)
        s.close()
        return "pass", "raw TCP to %s:%d connected (informational: the plan does not rely on it)" % (host, port), {"seconds": round(time.time() - t0, 2)}
    except Exception as e:
        return "fail", "raw TCP to %s:%d did not connect (%s). Expected under limited networking." % (host, port, type(e).__name__), {}


def chk_control(args):
    import requests
    try:
        r = requests.get("https://example.com/", timeout=(8, 10))
        reached, note = True, "HTTP %s" % r.status_code
    except Exception as e:
        reached, note = False, type(e).__name__
    expected_blocked = (args.mode == "limited")
    ok = (not reached) if expected_blocked else reached
    word = "reachable" if reached else "blocked"
    exp = "blocked" if expected_blocked else "reachable"
    return ("pass" if ok else "fail"), "example.com is %s (%s); expected %s in %s mode" % (word, note, exp, args.mode), {"reached": reached}


# ---- Turso

def norm_host(h):
    h = (h or "").strip()
    for pre in ("libsql://", "https://", "http://"):
        if h.startswith(pre):
            h = h[len(pre):]
    return h.strip("/")


def targ(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def turso_pipeline(host, stmts, timeout=90):
    import requests
    body = {"requests": [{"type": "execute", "stmt": s} for s in stmts] + [{"type": "close"}]}
    raw = json.dumps(body)
    t0 = time.time()
    r = requests.post("https://%s/v2/pipeline" % host,
                      headers={"Authorization": "Bearer " + os.environ["TURSO_AUTH_TOKEN"],
                               "Content-Type": "application/json"},
                      data=raw, timeout=(10, timeout))
    return r, round(time.time() - t0, 3), len(raw)


def turso_http_problem(r):
    if r.status_code in (401, 403):
        return ("Turso answered %s. The vault placeholder was probably not replaced in the Authorization header, "
                "or the token does not belong to this database. Body: %s" % (r.status_code, tail(r.text, 200)))
    return "HTTP %s from Turso: %s" % (r.status_code, tail(r.text, 250))


def column_types(res):
    cols = [c.get("name") for c in res.get("cols", [])]
    row = (res.get("rows") or [[]])[0]
    out = {}
    for name, cell in zip(cols, row):
        v = cell.get("value") if isinstance(cell, dict) else cell
        out[name] = {"type": cell.get("type") if isinstance(cell, dict) else None,
                     "json_value_type": type(v).__name__, "value": v if v is None or len(str(v)) < 40 else "..."}
    return out


def chk_turso_vault(args):
    host = norm_host(args.turso_host)
    sql = ("SELECT 1 AS int_col, 1.5 AS real_col, 'x' AS text_col, NULL AS null_col, x'0102' AS blob_col, "
           "9007199254740993 AS big_int_col, (1 = 1) AS bool_expr_col, ? AS int_arg, ? AS float_arg, ? AS text_arg")
    stmt = {"sql": sql, "args": [targ(42), targ(2.5), targ("hello")]}
    r, el, nbytes = turso_pipeline(host, [stmt], timeout=30)
    if r.status_code != 200:
        return "fail", turso_http_problem(r), {"http_status": r.status_code}
    res = r.json()["results"][0]
    if res.get("type") != "ok":
        return "fail", "Turso reached and authenticated, but the statement failed: %s" % tail(json.dumps(res), 300), {}
    types = column_types(res["response"]["result"])
    return "pass", "token substituted; the pipeline answered in %.2fs" % el, {
        "host_pattern_note": "reached with the credential allowed on *.turso.io",
        "value_typing": types,
        "note": "integers come back as {type: integer, value: '<string>'}",
    }


def chk_turso_latency(args):
    host = norm_host(args.turso_host)
    singles = []
    for _ in range(3):
        r, el, _n = turso_pipeline(host, [{"sql": "SELECT 1"}], timeout=30)
        if r.status_code != 200:
            return "fail", turso_http_problem(r), {}
        singles.append(el)
    insert_stmts = [{"sql": "CREATE TEMP TABLE spike_t (id INTEGER PRIMARY KEY, label TEXT, score REAL, note TEXT)"}]
    for i in range(500):
        insert_stmts.append({"sql": "INSERT INTO spike_t (id, label, score, note) VALUES (?, ?, ?, ?)",
                             "args": [targ(i), targ("label-%d" % i), targ(i * 0.5), targ(None if i % 7 == 0 else "n")]})
    insert_stmts.append({"sql": "SELECT count(*) AS n, sum(id) AS id_sum, avg(score) AS score_avg, max(label) AS label_max FROM spike_t"})
    select_stmts = [{"sql": "SELECT ? AS a, ? AS b, ? AS c, ? AS d",
                     "args": [targ(i), targ("label-%d" % i), targ(i * 0.5), targ(None)]} for i in range(500)]
    variant, stmts = "500 INSERTs into a TEMP table (plus 1 CREATE and 1 SELECT)", insert_stmts
    runs, final = [], None
    for attempt in range(3):  # attempt 0 may fall back to SELECTs; then two timed runs (cold, warm)
        r, el, nbytes = turso_pipeline(host, stmts, timeout=120)
        if r.status_code != 200:
            return "fail", turso_http_problem(r), {"variant": variant}
        results = r.json()["results"]
        errs = [x for x in results if x.get("type") == "error"]
        if errs and stmts is insert_stmts and not runs:
            variant = "500 parameterised SELECTs (TEMP table not allowed: %s)" % tail(json.dumps(errs[0]), 160)
            stmts = select_stmts
            continue
        if errs:
            return "fail", "a statement failed: %s" % tail(json.dumps(errs[0]), 300), {"variant": variant}
        runs.append({"seconds": el, "request_bytes": nbytes, "response_bytes": len(r.content)})
        final = results[-2]["response"]["result"]  # the SELECT (results[-1] is the close)
        if len(runs) == 2:
            break
    best = min(x["seconds"] for x in runs)
    data = {
        "variant": variant,
        "statements": 500,
        "single_statement_round_trips_s": singles,
        "single_statement_median_s": statistics.median(singles),
        "pipeline_runs": runs,
        "per_statement_ms_best": round(best / 500 * 1000, 2),
        "aggregate_row_typing": column_types(final) if final else {},
    }
    return "pass", "500-statement pipeline: %s s (best of %d); single statement median %.2f s" % (
        best, len(runs), statistics.median(singles)), data


# ---- Resend

def chk_resend(args):
    import requests
    body = {"from": "Steam spike <%s>" % RESEND_FROM, "to": [os.environ["REPORT_TO"]],
            "subject": "[TEST] sandbox check",
            "text": "Sent from the Managed Agents sandbox by sandbox_check.py. "
                    "If you can read this, the RESEND_API_KEY header and the REPORT_TO body were both substituted."}
    r = requests.post("https://api.resend.com/emails",
                      headers={"Authorization": "Bearer " + os.environ["RESEND_API_KEY"]},
                      json=body, timeout=(10, 30))
    try:
        j = r.json()
    except Exception:
        j = {}
    if r.status_code == 200 and j.get("id"):
        return "pass", "Resend accepted the message (id %s). Check the inbox of the REPORT_TO address to confirm delivery." % j["id"], {"http_status": 200, "resend_id": j["id"]}
    name = j.get("name") or ""
    msg = j.get("message") or tail(r.text, 200)
    hint = ""
    if r.status_code in (401, 403) and "own email" not in msg.lower():
        hint = " Likely the RESEND_API_KEY header was not substituted (or the key is invalid)."
    elif r.status_code == 403 and "own email" in msg.lower():
        hint = " The auth header worked. The test sender only delivers to the Resend account's own address, so REPORT_TO must equal it."
    elif r.status_code == 422:
        hint = " Likely the REPORT_TO body placeholder was not substituted (the address looks invalid to Resend)."
    return "fail", "HTTP %s %s: %s.%s" % (r.status_code, name, msg, hint), {"http_status": r.status_code, "resend_error_name": name}


# ---- Anthropic

def anthropic_post(body, extra_headers=None):
    import requests
    last = None
    for ver in ANTHROPIC_VERSIONS:
        headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": ver,
                   "content-type": "application/json"}
        headers.update(extra_headers or {})
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, data=json.dumps(body), timeout=(10, 90))
        last = (r, ver)
        if not (r.status_code == 400 and "version" in r.text.lower()):
            break
    return last


def chk_anthropic_requests(args):
    r, ver = anthropic_post({"model": "claude-haiku-5-5", "max_tokens": 16,
                             "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
    if r.status_code == 200:
        return "pass", "x-api-key substituted; model answered (anthropic-version %s)" % ver, {"http_status": 200, "api_version_used": ver, "model": r.json().get("model")}
    hint = " Likely the ANTHROPIC_API_KEY header was not substituted (or the key is invalid)." if r.status_code in (401, 403) else ""
    return "fail", "HTTP %s: %s.%s" % (r.status_code, tail(r.text, 250), hint), {"http_status": r.status_code}


def chk_anthropic_sdk(args):
    import anthropic
    client = anthropic.Anthropic(max_retries=0, timeout=60)
    try:
        m = client.messages.create(model="claude-haiku-5-5", max_tokens=16,
                                   messages=[{"role": "user", "content": "Reply with the single word OK."}])
    except anthropic.AuthenticationError as e:
        return "fail", "401 from the API through the SDK: the placeholder was not replaced, or the key is invalid. %s" % tail(str(e), 200), {"sdk_version": anthropic.__version__}
    except anthropic.APIConnectionError as e:
        return "fail", "SDK could not connect to api.anthropic.com: %s" % tail(str(e), 200), {"sdk_version": anthropic.__version__}
    except anthropic.APIStatusError as e:
        return "fail", "HTTP %s through the SDK: %s" % (e.status_code, tail(str(e), 250)), {"sdk_version": anthropic.__version__}
    return "pass", "the SDK accepted the placeholder key and the call succeeded", {
        "sdk_version": anthropic.__version__, "model": m.model, "request_id": getattr(m, "_request_id", None)}


SCHEMA = {"type": "object",
          "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
          "required": ["ok", "word"], "additionalProperties": False}


def structured_body(with_fallbacks):
    body = {"model": "claude-opus-5-5", "max_tokens": 400,
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            "messages": [{"role": "user", "content": "Return ok = true and word = \"spike\"."}]}
    if with_fallbacks:
        body["fallbacks"] = "default"
    return body


def evaluate_structured(r):
    j = r.json()
    texts = [b.get("text", "") for b in j.get("content", []) if b.get("type") == "text"]
    parsed = None
    try:
        parsed = json.loads(texts[0]) if texts else None
    except Exception:
        pass
    return j, parsed


def chk_fallbacks(args):
    r, ver = anthropic_post(structured_body(True), {"anthropic-beta": FALLBACK_BETA})
    data = {"http_status": r.status_code, "api_version_used": ver, "beta_header": FALLBACK_BETA}
    if r.status_code == 200:
        j, parsed = evaluate_structured(r)
        data.update({"stop_reason": j.get("stop_reason"), "served_by": j.get("model"), "parsed_ok": parsed is not None,
                     "schema_ok": bool(parsed) and parsed.get("ok") is True and isinstance(parsed.get("word"), str)})
        if j.get("stop_reason") == "refusal":
            return "fail", "request combined without an error, but the model refused (unexpected for this prompt)", data
        if data["schema_ok"]:
            return "pass", "fallbacks=\"default\" together with output_config.format returned schema-valid JSON", data
        return "fail", "accepted (HTTP 200) but the output did not match the schema", data
    data["error"] = tail(r.text, 400)
    if r.status_code in (401, 403):
        return "fail", "HTTP %s: authentication failed, so this check says nothing about the combination (see vault_anthropic_requests)" % r.status_code, data
    # Control: same request without fallbacks, to say which parameter the API objects to.
    c, _v = anthropic_post(structured_body(False))
    data["control_without_fallbacks_http_status"] = c.status_code
    which = "structured output alone also fails" if c.status_code != 200 else "structured output alone works, so the combination is rejected"
    return "fail", "HTTP %s with fallbacks + structured output; %s" % (r.status_code, which), data


def chk_fallbacks_sdk(args):
    import anthropic
    client = anthropic.Anthropic(max_retries=0, timeout=90)
    try:
        m = client.messages.create(model="claude-opus-5-5", max_tokens=400,
                                   output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                                   messages=[{"role": "user", "content": "Return ok = true and word = \"spike\"."}],
                                   extra_headers={"anthropic-beta": FALLBACK_BETA},
                                   extra_body={"fallbacks": "default"})
    except anthropic.APIStatusError as e:
        return "fail", "HTTP %s through the SDK: %s" % (e.status_code, tail(str(e), 300)), {"sdk_version": anthropic.__version__}
    text = next((b.text for b in m.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except Exception:
        return "fail", "SDK call succeeded but the output was not JSON", {"stop_reason": m.stop_reason}
    return ("pass" if parsed.get("ok") is True else "fail"), "SDK call with extra_body fallbacks=\"default\" returned JSON", {
        "stop_reason": m.stop_reason, "served_by": m.model, "sdk_version": anthropic.__version__}


# ---- Healthchecks

def chk_hc_ping(args):
    import requests
    r = requests.get("https://hc-ping.com/", timeout=(10, 15), allow_redirects=False)
    ok = r.status_code < 500
    return ("pass" if ok else "fail"), "hc-ping.com answered HTTP %s (reachable; no check was pinged)" % r.status_code, {"http_status": r.status_code}


# ----------------------------------------------------------------------------- summary

def summarize(args):
    st = {c["id"]: c["status"] for c in CHECKS}
    counts = {k: sum(1 for v in st.values() if v == k) for k in ("pass", "fail", "skip")}
    ws_ok = st.get("steam_ws_pics") == "pass"
    mirror_ok = st.get("steamcmd_reachable") == "pass"
    if ws_ok:
        build_source = "pics_websocket"
    elif mirror_ok:
        build_source = "steamcmd_mirror"
    else:
        build_source = "none_working"
    if st.get("steam_ws_pics") == "skip" and mirror_ok:
        build_source = "steamcmd_mirror (websocket not tested)"
    info = next((c["data"] for c in CHECKS if c["id"] == "sandbox_info"), {})
    notes = []
    if not ws_ok and st.get("ws_upgrade") == "pass":
        notes.append("The WebSocket upgrade passes but ws_pics.py failed. Read steam_ws_pics.data before deciding.")
    if not ws_ok and st.get("ws_upgrade") == "fail":
        notes.append("The WebSocket upgrade is blocked in %s mode." % args.mode)
    if st.get("control_blocked_host") == "fail" and args.mode == "limited":
        notes.append("example.com was reachable under limited networking: the allowlist may not be enforced, so treat other 'reachable' results with care.")
    return {
        "counts": counts,
        "mode": args.mode,
        "build_id_source_if_decided_now": build_source,
        "python_3_12": {"in_image": bool(info.get("python_is_3_12") or info.get("python3_12_on_path")),
                        "uv_install_works": st.get("uv_python312") == "pass"},
        "pip_steam_stack": st.get("pip_steam"),
        "vault_substitution": {"turso_requests": st.get("vault_turso"), "resend_requests": st.get("vault_resend"),
                               "anthropic_requests": st.get("vault_anthropic_requests"), "anthropic_sdk": st.get("vault_anthropic_sdk")},
        "turso_latency_500": st.get("turso_latency_500"),
        "fallbacks_default_with_structured_output": st.get("fallbacks_structured"),
        "healthchecks_reachable": st.get("hc_ping"),
        "notes": notes,
    }


# ----------------------------------------------------------------------------- main

def parse_args():
    here = os.path.dirname(os.path.abspath(__file__))
    default_ws = os.path.join(here, "ws_pics.py")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--turso-host", default=os.environ.get("TURSO_HOST", ""), help="database host, e.g. steam-yourorg.aws-us-east-1.turso.io (not a secret)")
    ap.add_argument("--mode", choices=["limited", "unrestricted"], default="limited", help="which environment this run is in (used for the control check and the report)")
    ap.add_argument("--ws-pics", default=default_ws, help="path to docs/spikes/ws_pics.py")
    ap.add_argument("--out", default="", help="also write the report here (default: /mnt/session/outputs/sandbox_report.json when that folder exists)")
    ap.add_argument("--only", default="", help="comma list of check ids to run (debugging)")
    ap.add_argument("--skip", default="", help="comma list of check ids to skip (debugging)")
    ap.add_argument("--resume", action="store_true", help="internal: continue after the re-exec into the venv python")
    return ap.parse_args()


def main():
    args = parse_args()
    os.makedirs(WORK, exist_ok=True)
    only = set(filter(None, args.only.split(",")))
    skip = set(filter(None, args.skip.split(",")))

    def wanted(cid):
        if only and cid not in only:
            return False
        return cid not in skip

    if args.resume:
        try:
            saved = json.load(open(PARTIAL))
            CHECKS.extend(saved["checks"])
            CTX.update(saved["ctx"])
            global START
            START = CTX.get("t0", START)
        except Exception:
            pass
    else:
        if wanted("sandbox_info"):
            check("sandbox_info", 30, chk_sandbox_info,
                  why="Python version, tools, proxy, which secret variables are present (names and lengths only)")
        if wanted("pip_requests_anthropic"):
            check("pip_requests_anthropic", 240, chk_pip_requests_anthropic,
                  why="pip can install requests and anthropic from PyPI under limited networking")
        if wanted("uv_python312"):
            check("uv_python312", 150, chk_uv_python312, why="whether `uv python install 3.12` works")
        if wanted("pip_steam"):
            check("pip_steam", 240, chk_pip_steam, why="pip install steam gevent-eventemitter \"protobuf<3.21\"")
        http_py = CTX.get("http_python")
        if http_py and not can_import("requests", "anthropic"):
            log("re-running with %s" % http_py)
            save_partial()
            os.execv(http_py, [http_py, os.path.abspath(__file__)] + sys.argv[1:] + ["--resume"])

    have_http = can_import("requests")
    no_http = None if have_http else "requests is not installed (see pip_requests_anthropic)"
    have_anthropic = can_import("anthropic")
    no_sdk = None if have_anthropic else "the anthropic SDK is not installed (see pip_requests_anthropic)"
    key = os.environ.get("ANTHROPIC_API_KEY")
    host = norm_host(args.turso_host)
    tok = os.environ.get("TURSO_AUTH_TOKEN")

    def reason_turso():
        if no_http:
            return no_http
        if not host:
            return "no --turso-host given"
        if not tok:
            return "TURSO_AUTH_TOKEN is not set (vault credential not configured)"
        return None

    def reason_anthropic(sdk=False):
        if sdk and no_sdk:
            return no_sdk
        if no_http:
            return no_http
        return None if key else "ANTHROPIC_API_KEY is not set (vault credential not configured)"

    plan = [
        ("steamcmd_reachable", 60, lambda: chk_steamcmd(args), no_http, "api.steamcmd.net/v1/info/730 reachable; its public buildid"),
        ("ws_upgrade", 90, lambda: chk_ws_upgrade(args), no_http, "WebSocket upgrade to a Steam CM on port 443 passes the egress proxy"),
        ("steam_ws_pics", 150, lambda: chk_steam_ws_pics(args), None, "ws_pics.py: anonymous login and appid 730 public buildid, compared with the mirror"),
        ("raw_tcp_cm", 30, lambda: chk_raw_tcp(args), None, "informational: raw TCP to a CM server"),
        ("control_blocked_host", 40, lambda: chk_control(args), no_http, "control: example.com is blocked under limited networking"),
        ("vault_turso", 60, lambda: chk_turso_vault(args), reason_turso(), "vault header substitution on *.turso.io from requests; value typing"),
        ("turso_latency_500", 300, lambda: chk_turso_latency(args), reason_turso(), "500-statement Turso pipeline latency"),
        ("vault_resend", 60, lambda: chk_resend(args),
         no_http or (None if (os.environ.get("RESEND_API_KEY") and os.environ.get("REPORT_TO")) else "RESEND_API_KEY and/or REPORT_TO not set (vault credentials not configured)"),
         "vault header (key) and body (REPORT_TO) substitution on api.resend.com; test-sender email"),
        ("vault_anthropic_requests", 120, lambda: chk_anthropic_requests(args), reason_anthropic(), "vault header substitution on api.anthropic.com from requests"),
        ("vault_anthropic_sdk", 120, lambda: chk_anthropic_sdk(args), reason_anthropic(sdk=True), "vault header substitution through the anthropic SDK"),
        ("fallbacks_structured", 200, lambda: chk_fallbacks(args), reason_anthropic(), "fallbacks=\"default\" combined with output_config.format (raw HTTP)"),
        ("fallbacks_structured_sdk", 200, lambda: chk_fallbacks_sdk(args), reason_anthropic(sdk=True), "the same combination through the SDK"),
        ("hc_ping", 40, lambda: chk_hc_ping(args), no_http, "hc-ping.com reachable"),
    ]
    done = {c["id"] for c in CHECKS}
    for cid, timeout, fn, skip_reason, why in plan:
        if cid in done or not wanted(cid):
            continue
        check(cid, timeout, fn, skip_reason=skip_reason, why=why)

    report = {
        "spike_version": SPIKE_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_seconds": round(time.time() - START, 1),
        "mode": args.mode,
        "checks": CHECKS,
        "summary": summarize(args),
    }
    text = json.dumps(scrub(report), indent=2, sort_keys=False)
    targets = [os.path.join(WORK, "sandbox_report.json")]
    out = args.out or ("/mnt/session/outputs/sandbox_report.json" if os.path.isdir("/mnt/session/outputs") else "")
    if out:
        targets.append(out)
    for t in targets:
        try:
            with open(t, "w") as f:
                f.write(text + "\n")
        except Exception as e:
            log("could not write %s: %s" % (t, e))
    print(text)


if __name__ == "__main__":
    main()
