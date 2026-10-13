"""VPS pre-flight tests — shared TLS trust store + `run.py --doctor`.

Run:  python -m unittest tests.test_doctor -v

Covers the two failures that only appear after copying the bot to a server:
a host without a CA bundle (every HTTPS call fails with
CERTIFICATE_VERIFY_FAILED) and a dashboard bound to 127.0.0.1, which is
unreachable from the user's PC at http://<server-ip>:8787. The doctor must
name the cause and the exact repair, and its FAIL verdict must gate a start
script (exit code 2).
"""
from __future__ import annotations

import io
import json
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot import tls
from nobitex_bot.config import Config
from nobitex_bot.doctor import (
    FAIL, OK, WARN, exit_code, render, run_doctor,
)
from nobitex_bot.nobitex_api import NobitexAPI, NobitexError, NobitexSession


def cert_exc() -> ssl.SSLCertVerificationError:
    return ssl.SSLCertVerificationError(
        1, "certificate verify failed: unable to get local issuer certificate")


def pick(checks: list, name: str):
    for c in checks:
        if c.name == name:
            return c
    raise AssertionError("no check named %r in %r"
                         % (name, [c.name for c in checks]))


class _StatusHandler(BaseHTTPRequestHandler):
    """Stands in for the running dashboard on /api/auth/status."""

    def do_GET(self):  # noqa: N802 (http.server naming)
        body = json.dumps({"password_set": True, "authenticated": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestTrustStore(unittest.TestCase):
    """tls.py — the verifier must stay STRICT and explain itself."""

    def test_context_verifies_and_is_reused(self):
        ctx = tls.context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertIs(tls.context(), ctx)

    def test_cert_error_detected_through_urlerror_reason(self):
        self.assertTrue(tls.is_cert_error(cert_exc()))
        self.assertTrue(tls.is_cert_error(urllib.error.URLError(cert_exc())))
        self.assertFalse(tls.is_cert_error(OSError("timed out")))
        self.assertFalse(tls.is_cert_error(TimeoutError()))

    def test_hint_names_the_repair_command(self):
        hint = tls.error_hint(urllib.error.URLError(cert_exc()))
        self.assertIn("ca-certificates", hint)
        self.assertIn("update-ca-certificates", hint)
        self.assertIn("certifi", hint)
        self.assertIn("TLS certificate verification failed", hint)

    def test_hint_is_empty_for_unrelated_failures(self):
        self.assertEqual(tls.error_hint(OSError("connection reset")), "")
        self.assertEqual(tls.error_hint(urllib.error.URLError("timed out")), "")

    def test_ca_report_shape(self):
        rep = tls.ca_report()
        self.assertIn("certs", rep)
        self.assertIn("source", rep)
        self.assertIn("certifi", rep)
        self.assertIn("ok", rep)
        self.assertIsInstance(rep["certs"], int)
        # a host either has a usable store or the report must say so
        self.assertEqual(rep["ok"], rep["certs"] > 0)

    def _isolate_context_cache(self):
        """Drop the module-level cache for one test, restore it after."""
        saved = (tls._CTX, tls._SOURCE)
        tls._CTX, tls._SOURCE = None, ""
        self.addCleanup(setattr, tls, "_CTX", saved[0])
        self.addCleanup(setattr, tls, "_SOURCE", saved[1])

    def test_certifi_is_merged_even_when_the_os_store_is_not_empty(self):
        """A non-empty OS store is not a working store.

        Measured 2026-10-11: this host's Windows ROOT subset exposed to
        Python held 23 roots (no TrustAsia, no Certum) after the exchange
        rotated the chain it serves, while certifi verified the same
        handshake. Certifi must therefore be MERGED whenever present — not
        skipped just because *some* certificates loaded.
        """
        tmp = Path(tempfile.mkdtemp(prefix="nobot_ca_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bundle = str(tmp / "cacert.pem")
        Path(bundle).write_text("# stand-in for a certifi bundle\n",
                                encoding="utf-8")
        fake = mock.MagicMock()
        fake.get_ca_certs.return_value = [
            {"subject": ((('commonName', "Example Root"),),)}]
        self._isolate_context_cache()
        with mock.patch.object(tls.ssl, "create_default_context",
                               return_value=fake), \
                mock.patch.object(tls, "certifi_bundle", return_value=bundle):
            ctx = tls.context()
        self.assertIs(ctx, fake)
        fake.load_verify_locations.assert_called_once_with(cafile=bundle)
        self.assertIn("certifi", tls._SOURCE)

    def test_empty_store_without_certifi_stays_a_loud_failure(self):
        """The fresh-VPS case: no OS bundle and no certifi must keep the
        loud WARNING + repair command — never a silent unverified context."""
        fake = mock.MagicMock()
        fake.get_ca_certs.return_value = []
        self._isolate_context_cache()
        out = io.StringIO()
        with mock.patch.object(tls.ssl, "create_default_context",
                               return_value=fake), \
                mock.patch.object(tls, "certifi_bundle", return_value=""), \
                redirect_stdout(out):
            tls.context()
        self.assertEqual(tls._SOURCE, "EMPTY (no CA certificates found)")
        self.assertIn("WARNING", out.getvalue())
        self.assertIn("ca-certificates", out.getvalue())
        fake.load_verify_locations.assert_not_called()


class TestNetworkErrorMapping(unittest.TestCase):
    """A broken trust store is a HOST problem, not a network hiccup.

    The raw call lives on NobitexSession (token + retries) and the public
    market-data call on NobitexAPI; both must use the shared verifier.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_tls_") )
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def session(self) -> NobitexSession:
        return NobitexSession(self.tmp / "data", timeout=5.0,
                              user_agent="test/1")

    def api(self) -> NobitexAPI:
        obj = NobitexAPI.__new__(NobitexAPI)
        obj.base = "https://apiv2.nobitex.ir"
        obj.session = SimpleNamespace(user_agent="test/1", timeout=5.0)
        return obj

    def test_raw_request_maps_cert_failure_to_actionable_error(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError(cert_exc())):
            with self.assertRaises(NobitexError) as caught:
                self.session()._raw_request("GET", "/market/stats", None)
        self.assertEqual(caught.exception.code, "TLS_CA_MISSING")
        self.assertIn("ca-certificates", str(caught.exception))

    def test_raw_request_keeps_plain_network_code(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("timed out")):
            with self.assertRaises(NobitexError) as caught:
                self.session()._raw_request("GET", "/market/stats", None)
        self.assertEqual(caught.exception.code, "NETWORK")

    def test_public_get_reraises_non_tls_and_maps_tls(self):
        api = self.api()
        with mock.patch("urllib.request.urlopen",
                        side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                api._public_get("/market/stats")
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError(cert_exc())):
            with self.assertRaises(NobitexError) as caught:
                api._public_get("/market/stats")
        self.assertEqual(caught.exception.code, "TLS_CA_MISSING")

    def test_requests_use_the_shared_context(self):
        """Every outgoing HTTPS call must pass the shared verifier."""
        api = self.api()
        seen = []

        def fake_urlopen(req, **kwargs):
            seen.append(kwargs.get("context"))
            raise urllib.error.URLError("stop here")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(NobitexError):   # session wraps it
                self.session()._raw_request("GET", "/market/stats", None)
            with self.assertRaises(urllib.error.URLError):  # api re-raises
                api._public_get("/market/stats")
        self.assertEqual(len(seen), 2)
        for ctx in seen:
            self.assertIs(ctx, tls.context())


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_doctor_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def cfg(self, host: str = "127.0.0.1", port: int = 8787,
            password: str = "", allow: bool = False,
            mode: str = "nobitex") -> Config:
        cfg = Config()
        cfg.engine.server_host = host
        cfg.engine.server_port = port
        cfg.engine.data_dir = str(self.tmp / "data")
        cfg.web.password = password
        cfg.web.allow_remote_without_password = allow
        cfg.exchange.mode = mode
        return cfg


class TestDashboardBindCheck(DoctorCase):
    """Why http://<server-ip>:8787 shows nothing from the user's PC."""

    def test_remote_bind_without_password_refuses_to_start(self):
        checks = run_doctor(self.cfg(host="0.0.0.0"), probe_net=False)
        bind = pick(checks, "dashboard bind")
        self.assertEqual(bind.status, FAIL)
        self.assertIn("REFUSES to start", bind.detail)
        self.assertIn("webauth --set", bind.fix)
        self.assertEqual(exit_code(checks), 2)

    def test_remote_bind_with_password_passes(self):
        checks = run_doctor(self.cfg(host="0.0.0.0", password="s3cret!"),
                            probe_net=False)
        bind = pick(checks, "dashboard bind")
        self.assertEqual(bind.status, OK)

    def test_open_remote_bind_is_warned(self):
        checks = run_doctor(
            self.cfg(host="0.0.0.0", allow=True), probe_net=False)
        bind = pick(checks, "dashboard bind")
        self.assertEqual(bind.status, WARN)
        self.assertIn("NO password", bind.detail)

    def test_loopback_bind_explains_both_fixes(self):
        checks = run_doctor(self.cfg(host="127.0.0.1", password="s3cret!"),
                            probe_net=False)
        bind = pick(checks, "dashboard bind")
        self.assertEqual(bind.status, WARN)
        self.assertIn("answers nothing", bind.detail)
        self.assertIn("0.0.0.0", bind.fix)
        self.assertIn("ssh -L 8787:127.0.0.1:8787", bind.fix)

    def test_reachability_advice_follows_the_bind_host(self):
        """Direct 0.0.0.0 and loopback behind a proxy fail differently, so the
        advice must name the right gate (and never suggest "just use 0.0.0.0"
        to a proxy host - that steals the port Caddy needs)."""
        direct = pick(run_doctor(self.cfg(host="0.0.0.0", password="s3cret!"),
                                 probe_net=False), "remote reachability")
        self.assertIn("direct exposure is ON", direct.detail)
        self.assertIn("netsh advfirewall", direct.fix)
        proxied = pick(run_doctor(self.cfg(host="127.0.0.1", password="s3cret!"),
                                  probe_net=False), "remote reachability")
        self.assertIn("direct exposure is OFF", proxied.detail)
        self.assertIn("do not \"fix\"", proxied.fix)
        for check in (direct, proxied):
            self.assertTrue(check.detail.isascii())
            self.assertTrue(check.fix.isascii())

    def test_port_already_in_use_is_reported_not_fatal(self):
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        self.addCleanup(held.close)
        port = held.getsockname()[1]
        checks = run_doctor(self.cfg(port=port), probe_net=False)
        check = pick(checks, "dashboard port")
        self.assertEqual(check.status, WARN)
        self.assertIn("already in use", check.detail)

    def test_wildcard_listener_is_not_reported_as_free(self):
        """The bug behind "I cannot open IP:8787 on the VPS".

        The old probe set SO_REUSEADDR and bound the wildcard; on Windows a
        second wildcard bind may succeed even while another process owns the
        port, so the report said "8787 is free" with IIS http.sys holding it.
        A wildcard holder must be flagged, not blessed.
        """
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        held.bind(("0.0.0.0", 0))
        held.listen(1)
        self.addCleanup(held.close)
        port = held.getsockname()[1]
        check = pick(run_doctor(self.cfg(host="0.0.0.0", password="s3cret!",
                                        port=port), probe_net=False),
                     "dashboard port")
        self.assertEqual(check.status, WARN)
        self.assertIn("already in use", check.detail)

    def test_our_own_running_dashboard_is_ok_not_a_conflict(self):
        """A second start must not be told its own healthy dashboard is a
        squatter: /api/auth/status identifies us."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StatusHandler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        check = pick(run_doctor(self.cfg(port=port), probe_net=False),
                     "dashboard port")
        self.assertEqual(check.status, OK)
        self.assertIn("served by this bot's dashboard", check.detail)

    def test_account_check_finds_the_real_token_file(self):
        cfg = self.cfg()
        real = Path(cfg.engine.data_dir) / "real"
        real.mkdir(parents=True)
        (real / "credentials.json").write_text(
            json.dumps({"token": "x" * 8}), encoding="utf-8")
        checks = run_doctor(cfg, probe_net=False)
        self.assertEqual(pick(checks, "exchange account").status, OK)

    def test_missing_credentials_are_reported(self):
        checks = run_doctor(self.cfg(), probe_net=False)
        check = pick(checks, "exchange account")
        self.assertEqual(check.status, WARN)
        self.assertIn("no credentials found", check.detail)

    def test_demo_mode_is_explained(self):
        checks = run_doctor(self.cfg(mode="demo"), probe_net=False)
        self.assertIn("simulator", pick(checks, "exchange account").detail)


class TestReport(DoctorCase):
    def test_report_is_ascii_only_for_windows_consoles(self):
        # the VPS is UTF-8 but the operator's Windows console is cp1252:
        # a stray em dash there prints as a replacement character
        out = render(run_doctor(self.cfg(), probe_net=False))
        self.assertTrue(out.isascii(), "non-ASCII leaked into the report")
        self.assertIn("verdict:", out)
        self.assertIn("dashboard bind", out)

    def test_certificate_failure_report_is_ascii_and_actionable(self):
        """The exact lines a VPS user sees must survive a cp1252 console
        AND name the repair command."""
        broken = {"host": "apiv2.nobitex.ir", "ok": False, "cert_error": True,
                  "error": tls.describe_error(urllib.error.URLError(cert_exc()))}
        with mock.patch("nobitex_bot.doctor.tls_probe", return_value=broken), \
                mock.patch("nobitex_bot.doctor.public_ip", return_value={}):
            checks = run_doctor(self.cfg(password="s3cret!"), probe_net=True)
        out = render(checks)
        self.assertTrue(out.isascii(), "non-ASCII leaked into the report")
        self.assertEqual(pick(checks, "exchange https").status, FAIL)
        self.assertIn("ca-certificates", out)
        self.assertEqual(exit_code(checks), 2)
        self.assertTrue(tls.FIX_HINT.isascii())
        self.assertTrue(tls.error_hint(urllib.error.URLError(cert_exc())).isascii())

    def test_skipped_network_probe_fails_loudly(self):
        checks = run_doctor(self.cfg(), probe_net=False)
        self.assertEqual(pick(checks, "exchange https").status, FAIL)
        self.assertEqual(pick(checks, "egress ip").status, WARN)

    def test_json_line_is_machine_readable(self):
        checks = run_doctor(self.cfg(), probe_net=False)
        doc = json.loads(pick(checks, "report_json").detail)
        names = [c["name"] for c in doc["checks"]]
        self.assertIn("trust store", names)
        self.assertIn("dashboard bind", names)
        self.assertNotIn("report_json", render(checks))


if __name__ == "__main__":
    unittest.main()
