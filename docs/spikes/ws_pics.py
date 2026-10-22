# Phase 0 spike (docs/AGENT_PLAN.md, Section 5). Not production code.
# Tested 2026-10-22: anonymous login and PICS public build ids over a WebSocket CM on port 443.
# Move the shim into steam_api.py only after the Managed Agents networking test passes.

"""PICS over WebSocket (port 443) using ValvePython steam 1.4.4 + a ~100 line transport shim.

Needs Python 3.12 and: pip install steam gevent-eventemitter "protobuf<3.21"
Usage: python ws_pics.py 730 570 440
Honors HTTPS_PROXY, SSL_CERT_FILE and REQUESTS_CA_BUNDLE when they are set.
"""
import base64
import os
from gevent import ssl
import struct
import sys
import time
from urllib.parse import urlparse

import gevent
import requests
from gevent import socket

from steam.client import SteamClient
from steam.core.connection import Connection
from steam.enums import EResult


def cm_ws_hosts(limit=8):
    """Websocket CM endpoints from the public Steam directory (plain HTTPS)."""
    r = requests.get(
        "https://api.steampowered.com/ISteamDirectory/GetCMListForConnect/v1/",
        params={"cellid": 0, "cmtype": "websockets", "format": "json"}, timeout=15)
    r.raise_for_status()
    out = []
    for s in r.json()["response"]["serverlist"]:
        host, port = s["endpoint"].rsplit(":", 1)
        if port == "443" and s["type"] == "websockets":
            out.append((host, 443))
    return out[:limit]


class WSConnection(Connection):
    """Drop-in for steam.core.connection.TCPConnection that speaks WebSocket over TLS.

    On the websocket CM transport each binary frame is exactly one Steam message,
    with NO 8-byte VT01 length header and NO ChannelEncrypt handshake (TLS does that).
    """

    def _new_socket(self):
        self.socket = None

    def _open(self, host, port):
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            p = urlparse(proxy)
            sock = socket.create_connection((p.hostname, p.port), timeout=15)
            sock.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n" % (host, port, host, port)).encode())
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
            mask = os.urandom(4)
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
                if op == 0x8:
                    break
                if op == 0x9:  # ping -> pong (masked)
                    mask = os.urandom(4)
                    self.socket.sendall(bytes([0x8A, 0x80 | len(data)]) + mask +
                                        bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
                    continue
                if op in (0x1, 0x2, 0x0):
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

    def _handle_cm_list(self, msg):  # server pushes TCP ip:port lists; ignore
        pass

    def connect(self, *a, **kw):
        ok = super().connect(*a, **kw)
        if ok:
            self.channel_secured = True  # TLS replaces the Steam channel-encrypt handshake
            self.emit(self.EVENT_CHANNEL_SECURED)
        return ok


def public_build_ids(appids):
    client = WSSteamClient(cm_ws_hosts())
    try:
        t0 = time.time()
        res = client.anonymous_login()
        if res != EResult.OK:
            raise RuntimeError("login: %r" % res)
        t1 = time.time()
        info = client.get_product_info(apps=list(appids), timeout=30)
        t2 = time.time()
        out = {}
        for a in appids:
            pub = info["apps"].get(a, {}).get("depots", {}).get("branches", {}).get("public", {})
            out[a] = (pub.get("buildid"), pub.get("timeupdated"))
        return out, (t1 - t0, t2 - t1), client.current_server_addr
    finally:
        client.disconnect()


if __name__ == "__main__":
    ids = [int(x) for x in sys.argv[1:]] or [730]
    out, timings, srv = public_build_ids(ids)
    print("server", srv, "login %.2fs pics %.2fs" % timings)
    for a, (b, t) in out.items():
        print(a, "buildid", b, "timeupdated", t, time.strftime("%F %T", time.gmtime(int(t))) if t else "")
