"""Dashboard server noise control — dropped clients must not flood the console.

The bot runs on a machine with a public IP, so port scanners, closed tabs
and half-open sockets hit the dashboard constantly. Every drop made
socketserver print a full traceback («Exception occurred during processing
of request from …» / «ConnectionResetError … forcibly closed by the remote
host») on the very console the quiet mode exists to keep clean.

Two halves are pinned here:
* connection-level errors are swallowed while a real bug inside a request
  handler still prints its traceback;
* an idle/abandoned connection is closed after the socket timeout instead
  of keeping a worker thread alive as long as the socket stays open.

Run:  python -m unittest tests.test_web_noise -v
"""
from __future__ import annotations

import http.client
import io
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import Config  # noqa: E402
from nobitex_bot.web import BotHTTPServer, Handler  # noqa: E402
from nobitex_bot.webauth import DashboardAuth  # noqa: E402


class StubEngine:
    """The noise tests never route into the engine."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.events = None

    def status(self) -> dict:
        return {"running": False}


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_noise_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cfg = Config()
        cfg.engine.data_dir = str(self.tmp / "data")
        self.cfg = cfg
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps({"web": {"password": ""}}),
                                 encoding="utf-8")
        self.server = BotHTTPServer(("127.0.0.1", 0), StubEngine(cfg),
                                    DashboardAuth(cfg,
                                                  config_path=self.cfg_path))
        self.addCleanup(self.server.server_close)

    def _serve(self):
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.server.shutdown)
        return t

    def _capture_stderr(self, fn) -> str:
        buf = io.StringIO()
        old = sys.stderr
        sys.stderr = buf
        try:
            fn()
        finally:
            sys.stderr = old
        return buf.getvalue()

    def _handle_error_with(self, exc: BaseException) -> str:
        def run():
            try:
                raise exc
            except type(exc):
                self.server.handle_error(None, ("2.145.116.215", 33138))
        return self._capture_stderr(run)


class TestDroppedConnectionsStayQuiet(TempCase):
    def test_connection_reset_is_silent(self):
        # the exact error from the user's console
        out = self._handle_error_with(ConnectionResetError(
            10054, "An existing connection was forcibly closed by the "
                   "remote host"))
        self.assertEqual(out, "")

    def test_all_client_hangups_are_silent(self):
        for exc in (ConnectionAbortedError(10053, "software caused abort"),
                    BrokenPipeError(32, "broken pipe"),
                    TimeoutError("timed out")):
            self.assertEqual(self._handle_error_with(exc), "",
                             "noise from %r" % (exc,))

    def test_real_handler_bug_still_prints(self):
        out = self._handle_error_with(ValueError("boom"))
        self.assertIn("Exception occurred during processing", out)
        self.assertIn("ValueError", out)

    def test_idle_connection_is_closed_by_the_timeout(self):
        # the production value is 30s; shortened so the test stays quick
        Handler.timeout = 0.4
        self.addCleanup(setattr, Handler, "timeout", 30)
        self._serve()
        host, port = self.server.server_address
        s = socket.create_connection((host, port), timeout=5)
        self.addCleanup(s.close)
        s.settimeout(5)
        started = time.time()
        data = s.recv(1)          # the server closes the silent socket
        self.assertEqual(data, b"")
        self.assertLess(time.time() - started, 3.0)

    def test_normal_request_still_served(self):
        self._serve()
        conn = http.client.HTTPConnection(*self.server.server_address,
                                          timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/auth/status")
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(resp.status, 200)
        self.assertIn("authenticated", body)


if __name__ == "__main__":
    unittest.main()
