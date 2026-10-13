"""Reverse-proxy contract tests — what deploy/Caddyfile and the nginx block
must get right in front of the bot.

Run:  python -m unittest tests.test_proxy_contract -v

The dashboard is served behind TLS + a proxy password while the bot itself
stays on 127.0.0.1:8787 (deploy/). A minimal proxy implementation stands in
for Caddy/nginx here and drives the REAL BotHTTPServer, so the requirements
in deploy/README.md are executable, not folklore:

* the proxy password must be demanded before anything else (401),
* the ORIGINAL Host must reach the bot, or its CSRF guard rejects every POST
  (403) — the single most common misconfiguration,
* X-Forwarded-Proto: https is what marks the session cookie Secure,
* and the bot's own password gate stays on top of the proxy.
"""
from __future__ import annotations

import base64
import hmac
import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import Config
from nobitex_bot.web import BotHTTPServer
from nobitex_bot.webauth import COOKIE_NAME, DashboardAuth

PROXY_USER, PROXY_PW = "ops", "proxy-pw"
BOT_PW = "bot-pw"


class StubEngine:
    """Just enough engine for the routes under test."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.events = None
        self.auto_trading = False
        self.safe_mode = False
        self._running = True
        self.stopped = 0

    def status(self) -> dict:
        return {"running": self._running, "mode": self.cfg.exchange.mode}

    def running(self) -> bool:
        return self._running

    def stop(self) -> None:
        self.stopped += 1
        self._running = False


class ProxyHandler(BaseHTTPRequestHandler):
    """A stand-in for Caddy/nginx: basic auth + header handling + relay."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the test output clean
        pass

    def _auth_ok(self) -> bool:
        if not self.server.proxy_cfg["basic_auth"]:
            return True
        header = self.headers.get("Authorization") or ""
        if not header.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
        except Exception:
            return False
        user, _, pw = raw.partition(":")
        return (hmac.compare_digest(user, PROXY_USER) and
                hmac.compare_digest(pw, PROXY_PW))

    def _handle(self, method: str) -> None:
        cfg = self.server.proxy_cfg
        if not self._auth_ok():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        drop = ("host", "connection", "content-length", "authorization",
                "proxy-connection")
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in drop}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        if cfg["host"]:
            # nginx: proxy_set_header Host $http_host;  Caddy does this by default
            headers["Host"] = cfg["host"]
        if cfg["proto"]:
            headers["X-Forwarded-Proto"] = cfg["proto"]
        conn = http.client.HTTPConnection(*self.server.upstream, timeout=15)
        conn.request(method, self.path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in ("transfer-encoding", "connection",
                               "content-length", "keep-alive"):
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)
        conn.close()

    def do_GET(self):        # noqa: N802
        self._handle("GET")

    def do_POST(self):       # noqa: N802
        self._handle("POST")


class ProxyCase(unittest.TestCase):
    """Real bot server + simulated proxy, both on loopback."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_proxy_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = Config()
        self.cfg.engine.data_dir = str(self.tmp / "data")
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(
            json.dumps({"web": {"password": BOT_PW}}), encoding="utf-8")

        self.engine = StubEngine(self.cfg)
        auth = DashboardAuth(self.cfg, config_path=self.cfg_path)
        self.assert_true = auth.enabled()
        self.assertTrue(auth.enabled(), "the bot's own gate must be active")
        self.bot = BotHTTPServer(("127.0.0.1", 0), self.engine, auth)
        threading.Thread(target=self.bot.serve_forever, daemon=True).start()
        self.addCleanup(self.bot.server_close)
        self.addCleanup(self.bot.shutdown)
        self.bot_port = self.bot.server_address[1]

        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        self.proxy.daemon_threads = True
        self.proxy.upstream = ("127.0.0.1", self.bot_port)
        self.proxy.proxy_cfg = {"basic_auth": True, "host": "v42.ir",
                                "proto": "https"}
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)
        self.proxy_port = self.proxy.server_address[1]

    # ------------------------------------------------------------- helpers
    def call(self, method: str, path: str, body: dict | None = None,
             origin: str = "https://v42.ir", host: str = "v42.ir",
             auth: tuple | None = None, cookie: str = ""):
        headers = {"Host": host, "Origin": origin}
        if auth:
            token = base64.b64encode(("%s:%s" % auth).encode()).decode()
            headers["Authorization"] = "Basic " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy_port,
                                          timeout=15)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read().decode("utf-8", "replace")
            return resp.status, dict(resp.getheaders()), payload
        finally:
            conn.close()

    def login(self, **kw):
        return self.call("POST", "/api/auth/login", {"password": BOT_PW},
                         auth=(PROXY_USER, PROXY_PW), **kw)

    # ---------------------------------------------------------------- tests
    def test_proxy_password_is_demanded_first(self):
        status, headers, body = self.call("GET", "/api/auth/status")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))
        self.assertNotIn("password_set", body)

    def test_well_configured_proxy_logs_in_with_secure_cookie(self):
        status, headers, payload = self.login()
        self.assertEqual(status, 200, payload)
        self.assertTrue(json.loads(payload)["ok"])
        cookie = headers.get("Set-Cookie") or ""
        self.assertIn(COOKIE_NAME + "=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        # X-Forwarded-Proto: https is what makes the proxy-authenticated
        # session impossible to steal over plain HTTP
        self.assertIn("Secure", cookie)

    def test_missing_host_rewrite_breaks_every_post(self):
        """The failure mode behind 'GET works, all POSTs 403'."""
        self.proxy.proxy_cfg["host"] = ""          # nginx default behaviour
        status, _, payload = self.login()
        self.assertEqual(status, 403, payload)

    def test_missing_forwarded_proto_drops_the_secure_flag(self):
        self.proxy.proxy_cfg["proto"] = ""
        status, headers, _ = self.login()
        self.assertEqual(status, 200)
        self.assertNotIn("Secure", headers.get("Set-Cookie") or "")

    def test_bot_gate_still_requires_its_own_password(self):
        status, headers, payload = self.login_wrong()
        self.assertEqual(status, 401, payload)
        self.assertFalse(json.loads(payload)["ok"])
        self.assertNotIn(COOKIE_NAME, headers.get("Set-Cookie") or "")

    def login_wrong(self):
        return self.call("POST", "/api/auth/login", {"password": "nope"},
                         auth=(PROXY_USER, PROXY_PW))

    def test_session_then_mutation_passes_csrf_and_reaches_the_engine(self):
        status, headers, _ = self.login()
        self.assertEqual(status, 200)
        cookie = (headers.get("Set-Cookie") or "").split(";", 1)[0]
        status, _, payload = self.call("GET", "/api/status",
                                       auth=(PROXY_USER, PROXY_PW),
                                       cookie=cookie)
        self.assertEqual(status, 200, payload)
        self.assertTrue(json.loads(payload)["running"])

        status, _, payload = self.call("POST", "/api/control/stop", {},
                                       auth=(PROXY_USER, PROXY_PW),
                                       cookie=cookie)
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.engine.stopped, 1)

    def test_cross_site_post_is_still_refused(self):
        """A foreign Origin cannot ride the proxy session (CSRF stays on)."""
        status, headers, _ = self.login()
        cookie = (headers.get("Set-Cookie") or "").split(";", 1)[0]
        status, _, payload = self.call("POST", "/api/control/stop", {},
                                       origin="https://evil.example",
                                       auth=(PROXY_USER, PROXY_PW),
                                       cookie=cookie)
        self.assertEqual(status, 403, payload)
        self.assertEqual(self.engine.stopped, 0)

    def direct(self, path: str, host: str = "127.0.0.1:8787"):
        """Talk to the bot on loopback, skipping the proxy entirely."""
        conn = http.client.HTTPConnection("127.0.0.1", self.bot_port,
                                          timeout=10)
        try:
            conn.request("GET", path, headers={"Host": host})
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8", "replace")
        finally:
            conn.close()

    def test_direct_loopback_access_still_needs_the_bot_password(self):
        """Anyone with a shell on the box can skip the proxy: the second
        layer must still hold, so the dashboard never renders unauthenticated."""
        status, body = self.direct("/api/status")
        self.assertEqual(status, 401, body)
        status, body = self.direct("/")
        self.assertEqual(status, 200)
        self.assertIn("loginForm", body)          # the login page, not the app
        self.assertNotIn('id="kpis"', body)        # no dashboard payload


if __name__ == "__main__":
    unittest.main()
