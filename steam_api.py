"""Thin wrappers around Steam's public store + review endpoints, plus PICS.

No API key is required for any of these. The HTTP calls (store search, app
details, reviews, news) go through a single session with a browser-like
User-Agent, a timeout, and bounded retry/backoff so a transient hiccup doesn't
kill an unattended run.

`get_public_build_id` is different: it talks to Steam's PICS system over the CM
(Connection Manager) protocol via the `steam` package, to read the live public
branch's build id. This is the authoritative "an update shipped" signal — it
catches silent depot pushes and needs no cooperation from the developer, and it is
the ONLY release signal this project uses (the news feed carries posts that are not
patches, so it is context at most). The CM connection is a WebSocket on port 443
(see the transport shim below), because raw TCP to CM servers is blocked in
sandboxes. `get_public_build_id_mirror` reads the same PICS data from the
api.steamcmd.net community mirror over plain HTTPS, and `build_source` tries the
two in that order.

WHY PYTHON 3.12 (not newer): the `steam` package and its transitive deps pin us
to 3.12. `gevent`/`protobuf` only ship working wheels for 3.12 at the versions
`steam` requires (`protobuf<3.21`; >=3.21 breaks steam's generated `_pb2`
modules), and Python 3.13+ removed stdlib modules (`cgi`, `imp`) that `steam`
still imports. On 3.14 the import fails outright. The project therefore runs
from a dedicated 3.12 `.venv` (see SETUP.md). The PICS path degrades gracefully:
if `steam` can't be imported (e.g. someone runs collect.py outside the venv),
the WebSocket source returns nothing and `build_source` falls through to the
HTTPS mirror, which needs no extra packages.
"""

from __future__ import annotations

import base64
import html
import logging
import os
import re
import struct
import time
import types
from typing import Iterator, NamedTuple
from urllib.parse import urlparse

import requests

STORE_SEARCH = "https://store.steampowered.com/api/storesearch/"
APP_DETAILS = "https://store.steampowered.com/api/appdetails"
APP_REVIEWS = "https://store.steampowered.com/appreviews/{appid}"
APP_NEWS = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
CM_DIRECTORY = "https://api.steampowered.com/ISteamDirectory/GetCMListForConnect/v1/"
STEAMCMD_INFO = "https://api.steamcmd.net/v1/info/{appid}"

# Where a build id came from. These strings are what job_runs.build_source stores.
SOURCE_PICS_WS = "pics_ws"
SOURCE_MIRROR = "steamcmd_net"

_LOG = logging.getLogger("steam_api")

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


class BuildInfo(NamedTuple):
    """The public-branch build for one app, and which source answered."""

    build_id: str
    timeupdated: int  # unix ts of the push; 0 when the source did not say
    source: str       # SOURCE_PICS_WS | SOURCE_MIRROR


# --- PICS over WebSocket (port 443) -------------------------------------------------
#
# The stock `steam` client speaks only raw TCP to CM servers, which sandboxes block.
# Steam also serves the CM protocol over WebSocket on port 443. This shim swaps the
# transport and leaves the rest of `steam` (anonymous login, get_product_info) as is.
# On the WebSocket transport each binary frame is exactly one Steam message, with NO
# 8-byte VT01 length header and NO ChannelEncrypt handshake (TLS secures the channel).
#
# It must use gevent's own `ssl` and `socket`: the stdlib `ssl` blocks gevent's hub
# and login hangs until the CM times out. It is built lazily so that importing this
# module never needs `steam`/`gevent` (the 3.12 venv); see _ws_stack().

_WS_STACK: types.SimpleNamespace | None = None


def _build_ws_stack() -> types.SimpleNamespace:
    import gevent
    from gevent import socket, ssl
    from steam.client import SteamClient  # type: ignore[import-not-found]
    from steam.core.connection import Connection  # type: ignore[import-not-found]
    from steam.enums import EResult  # type: ignore[import-not-found]

    class WSConnection(Connection):
        """Drop-in for steam.core.connection.TCPConnection that speaks WebSocket over TLS."""

        def _new_socket(self):
            self.socket = None

        def _open(self, host, port):
            # Honor HTTPS_PROXY with an HTTP CONNECT (sandboxes reach the internet only
            # through one). Credentials in the proxy URL are sent as Basic auth.
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if proxy:
                p = urlparse(proxy)
                sock = socket.create_connection((p.hostname, p.port or (443 if p.scheme == "https" else 80)),
                                                timeout=15)
                auth = ""
                if p.username:
                    token = base64.b64encode(f"{p.username}:{p.password or ''}".encode()).decode()
                    auth = f"Proxy-Authorization: Basic {token}\r\n"
                sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode())
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise socket.error("proxy closed")
                    buf += chunk
                if b" 200 " not in buf.split(b"\r\n", 1)[0]:
                    raise socket.error("proxy CONNECT failed: %r" % buf[:80])
            else:
                sock = socket.create_connection((host, port), timeout=15)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # gevent.ssl, so reads yield to the hub
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
            if cafile:
                ctx.load_verify_locations(cafile)
            else:
                ctx.load_default_certs()
            return ctx.wrap_socket(sock, server_hostname=host)

        def _connect(self, server_addr):
            host, port = server_addr
            self.socket = self._open(host, port)
            key = base64.b64encode(os.urandom(16)).decode()
            self.socket.sendall((
                "GET /cmsocket/ HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (host, key)).encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self.socket.recv(4096)
                if not chunk:
                    raise socket.error("closed during handshake")
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                raise socket.error("websocket upgrade refused: %r" % head[:80])
            self.socket.settimeout(None)
            self._readbuf = rest
            self._frag = b""

        def _writer_loop(self):
            while True:
                msg = self.send_queue.get()
                mask = os.urandom(4)  # client frames must be masked
                n = len(msg)
                hdr = b"\x82" + (bytes([0x80 | n]) if n < 126 else b"\xfe" + struct.pack(">H", n) if n < 65536
                                 else b"\xff" + struct.pack(">Q", n))
                masked = bytes(b ^ mask[i % 4] for i, b in enumerate(msg))
                try:
                    self.socket.sendall(hdr + mask + masked)
                except Exception:
                    self.disconnect()
                    return

        def _need(self, n):
            while len(self._readbuf) < n:
                chunk = self.socket.recv(65536)
                if not chunk:
                    raise EOFError
                self._readbuf += chunk
            out, self._readbuf = self._readbuf[:n], self._readbuf[n:]
            return out

        def _reader_loop(self):
            try:
                while True:
                    b0, b1 = self._need(2)
                    op, fin, ln = b0 & 0x0F, b0 & 0x80, b1 & 0x7F
                    if ln == 126:
                        ln, = struct.unpack(">H", self._need(2))
                    elif ln == 127:
                        ln, = struct.unpack(">Q", self._need(8))
                    data = self._need(ln)  # server frames are unmasked
                    if op == 0x8:  # close
                        break
                    if op == 0x9:  # ping -> pong (masked)
                        mask = os.urandom(4)
                        self.socket.sendall(bytes([0x8A, 0x80 | len(data)]) + mask +
                                            bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
                        continue
                    if op in (0x1, 0x2, 0x0):  # text / binary / continuation
                        self._frag += data
                        if fin:
                            self.recv_queue.put(self._frag)
                            self._frag = b""
            except Exception:
                pass
            self.disconnect()

    class WSSteamClient(SteamClient):
        def __init__(self, hosts):
            super().__init__()
            self.connection = WSConnection()
            self.cm_servers.clear()
            self.cm_servers.merge_list(hosts)

        def _handle_cm_list(self, msg):  # the server pushes TCP ip:port lists; keep our 443 list
            pass

        def connect(self, *a, **kw):
            ok = super().connect(*a, **kw)
            if ok:
                self.channel_secured = True  # TLS replaces the Steam channel-encrypt handshake
                self.emit(self.EVENT_CHANNEL_SECURED)
            return ok

    return types.SimpleNamespace(gevent=gevent, client_cls=WSSteamClient, EResult=EResult)


def _ws_stack() -> types.SimpleNamespace:
    """The WebSocket client classes, built on first use. Raises ImportError without `steam`."""
    global _WS_STACK
    if _WS_STACK is None:
        _WS_STACK = _build_ws_stack()
    return _WS_STACK


def cm_ws_hosts(limit: int = 8) -> list[tuple[str, int]]:
    """WebSocket CM endpoints (port 443) from Steam's public directory, over plain HTTPS."""
    resp = _get(CM_DIRECTORY, {"cellid": 0, "cmtype": "websockets", "format": "json"}, retries=2)
    out = []
    for s in resp.json()["response"]["serverlist"]:
        host, port = s["endpoint"].rsplit(":", 1)
        if port == "443" and s["type"] == "websockets":
            out.append((host, 443))
    return out[:limit]


def get_public_build_ids(appids: list[int], *, timeout: float = 60.0) -> dict[int, tuple[str, int]]:
    """Return {appid: (build_id, timeupdated)} for each app's PUBLIC branch, via PICS.

    Connects anonymously to a Steam CM server over WebSocket (port 443) and queries
    PICS — the same product-info system SteamDB reads — once for all apps. We
    deliberately look only at `depots.branches.public`, the default branch every player
    receives, so pushes to beta/experimental/unstable branches are ignored. A change in
    the returned build id means a real update shipped to the public, regardless of
    whether the developer posted patch notes.

    The whole exchange is a login (~0.8 s) plus one product-info request (~0.5 s). An
    app with no public build id is simply absent from the result.

    Returns {} on ANY failure so the caller never breaks: the `steam` package not being
    importable (running outside the 3.12 venv — see this module's docstring), a failed
    anonymous login, an unreachable directory, a hang (bounded by `timeout`), or a
    transient CM error.
    """
    appids = [int(a) for a in appids]
    if not appids:
        return {}
    try:
        stack = _ws_stack()
    except ImportError as exc:
        # `steam` (and its gevent/protobuf stack) isn't installed — almost always
        # because collect.py is being run outside the 3.12 venv.
        _LOG.warning("build ids via PICS WebSocket unavailable: %s", exc)
        return {}

    client = None
    try:
        with stack.gevent.Timeout(timeout):  # gevent can hang; never let it stall a run
            hosts = cm_ws_hosts()
            if not hosts:
                _LOG.warning("PICS WebSocket: Steam returned no port-443 CM servers")
                return {}
            client = stack.client_cls(hosts)
            result = client.anonymous_login()
            if result != stack.EResult.OK:
                _LOG.warning("PICS WebSocket: anonymous login failed (%r)", result)
                return {}
            info = client.get_product_info(apps=appids, timeout=30)
    except (Exception, stack.gevent.Timeout) as exc:
        # CM connection/timeout/protocol hiccup — best-effort, never fatal.
        _LOG.warning("PICS WebSocket failed: %s: %s", exc.__class__.__name__, exc)
        return {}
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

    out: dict[int, tuple[str, int]] = {}
    for appid in appids:
        public = (
            (info.get("apps") or {}).get(appid, {})
            .get("depots", {})
            .get("branches", {})
            .get("public", {})
        )
        build_id = public.get("buildid")
        if build_id:
            out[appid] = (str(build_id), int(public.get("timeupdated", 0) or 0))
    return out


def get_public_build_id(appid: int) -> tuple[str, int] | None:
    """Return (build_id, timeupdated) for the app's PUBLIC branch via PICS, or None.

    Single-app form of get_public_build_ids. Returns None on any failure; see that
    function for how it works and why.
    """
    return get_public_build_ids([appid]).get(int(appid))


def get_public_build_id_mirror(appid: int) -> tuple[str, int] | None:
    """Return (build_id, timeupdated) for the app's PUBLIC branch from api.steamcmd.net.

    The same PICS data read through a free community mirror over plain HTTPS: one app
    per request, no key, no extra packages. It is third-party with no SLA, so it ranks
    below the direct WebSocket read in build_source. Returns None on any failure or
    when the app has no public build id.
    """
    try:
        resp = _get(STEAMCMD_INFO.format(appid=int(appid)), {}, retries=2)
        payload = resp.json()
        if payload.get("status", "success") != "success":
            return None
        public = payload["data"][str(appid)]["depots"]["branches"]["public"]
        build_id = public.get("buildid")
        if not build_id:
            return None
        return str(build_id), int(public.get("timeupdated", 0) or 0)
    except (SteamError, ValueError, KeyError, TypeError, AttributeError) as exc:
        _LOG.warning("steamcmd mirror failed for appid %s: %s: %s", appid, exc.__class__.__name__, exc)
        return None


def build_sources(appids: list[int], *, min_timeupdated: dict[int, int] | None = None) -> dict[int, BuildInfo | None]:
    """Resolve the public build for each app: PICS over WebSocket, then the mirror.

    One PICS login covers every app; the mirror is asked only for apps PICS did not
    answer. A mirror answer older than `min_timeupdated[appid]` (the newest build we
    already hold) is treated as stale and dropped, so a lagging mirror can never
    present an old build as current. A mirror answer with no timestamp can't be judged
    and is accepted. An app both sources fail on maps to None: the caller reports
    "build id unavailable" and makes no claim about updates.
    """
    appids = [int(a) for a in appids]
    min_timeupdated = min_timeupdated or {}
    out: dict[int, BuildInfo | None] = {}
    pics = get_public_build_ids(appids)
    for appid in appids:
        if appid in pics:
            out[appid] = BuildInfo(pics[appid][0], pics[appid][1], SOURCE_PICS_WS)
            continue
        hit = get_public_build_id_mirror(appid)
        if hit and hit[1] and hit[1] < min_timeupdated.get(appid, 0):
            _LOG.warning("steamcmd mirror is stale for appid %s (build %s pushed %s, newest held %s)",
                         appid, hit[0], hit[1], min_timeupdated[appid])
            hit = None
        out[appid] = BuildInfo(hit[0], hit[1], SOURCE_MIRROR) if hit else None
    return out


def build_source(appid: int, *, min_timeupdated: int = 0) -> BuildInfo | None:
    """The build_sources chain for one app: PICS over WebSocket, then the mirror."""
    return build_sources([appid], min_timeupdated={int(appid): min_timeupdated})[int(appid)]


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
