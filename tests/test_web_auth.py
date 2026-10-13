"""Dashboard password gate tests — lockout, sessions, config.json recovery.

Run:  python -m unittest tests.test_web_auth -v
Covers: password verify (plaintext + PBKDF2), lockout after N wrong tries,
correct password rejected while locked, unlock by editing config.json,
session issue/expiry/revoke, the HTTP gate (401 before login, 302 at
/login when signed in, CSRF origin refusal) and the non-loopback guard
that refuses to expose an unprotected dashboard.
"""
from __future__ import annotations

import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nobitex_bot.config import Config
from nobitex_bot.webauth import (
    COOKIE_NAME, DashboardAuth, hash_password, is_loopback_host,
    verify_password, write_password,
)


def make_cfg(tmp: Path, password: str = "", **web) -> Config:
    cfg = Config()
    cfg.engine.data_dir = str(tmp / "data")
    cfg.web.password = password
    for k, v in web.items():
        setattr(cfg.web, k, v)
    return cfg


class StubEngine:
    """Minimal stand-in for TradingEngine: the gate never touches it."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.events = None

    def status(self) -> dict:
        return {"running": False, "mode": self.cfg.exchange.mode}


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nobot_webauth_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg_path = self.tmp / "config.json"
        self.cfg_path.write_text(json.dumps({"web": {"password": ""}}),
                                 encoding="utf-8")

    def auth(self, password: str = "s3cret!", **web) -> DashboardAuth:
        cfg = make_cfg(self.tmp, password, **web)
        self.write_config(password, **web)
        return DashboardAuth(cfg, config_path=self.cfg_path)

    def write_config(self, password: str, **web) -> None:
        web_doc = {"password": password}
        web_doc.update(web)
        self.cfg_path.write_text(json.dumps({"web": web_doc}), encoding="utf-8")


class TestPasswordVerify(TempCase):
    def test_plaintext_matches(self):
        self.assertTrue(verify_password("hunter2", "hunter2"))
        self.assertFalse(verify_password("hunter2", "hunter3"))
        self.assertFalse(verify_password("", "hunter2"))
        self.assertFalse(verify_password("hunter2", ""))

    def test_pbkdf2_hash_roundtrip(self):
        stored = hash_password("پسورد من", rounds=1000)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(verify_password(stored, "پسورد من"))
        self.assertFalse(verify_password(stored, "پسورد منی"))

    def test_malformed_hash_never_matches(self):
        self.assertFalse(verify_password("pbkdf2_sha256$broken", "x"))
        self.assertFalse(verify_password("pbkdf2_sha256$abc$zz$zz", "x"))

    def test_loopback_detection(self):
        for host in ("127.0.0.1", "::1", "localhost", "", "127.5.5.5"):
            self.assertTrue(is_loopback_host(host), host)
        for host in ("0.0.0.0", "::", "192.168.1.10", "example.com"):
            self.assertFalse(is_loopback_host(host), host)


class TestWritePassword(TempCase):
    """`python -m nobitex_bot.webauth --set` — the CLI/Terminal path."""

    def test_set_writes_hash_and_keeps_every_other_section(self):
        self.cfg_path.write_text(json.dumps({
            "engine": {"server_port": 8787, "max_markets": 300},
            "exchange": {"mode": "nobitex", "nobitex_username": "me@x"},
            "risk": {"max_open_positions": 8},
        }), encoding="utf-8")
        hashed = write_password(self.cfg_path, "my-secret-pw", rounds=1000)
        doc = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertTrue(doc["web"]["password"].startswith("pbkdf2_sha256$"))
        self.assertEqual(doc["web"]["password"], hashed)
        self.assertNotIn("my-secret-pw", json.dumps(doc))
        self.assertEqual(doc["engine"]["max_markets"], 300)
        self.assertEqual(doc["exchange"]["nobitex_username"], "me@x")
        self.assertEqual(doc["risk"]["max_open_positions"], 8)
        # the new password is what the live gate accepts
        auth = DashboardAuth(make_cfg(self.tmp, ""),
                             config_path=self.cfg_path)
        self.assertTrue(auth.enabled())
        self.assertTrue(auth.attempt("my-secret-pw").ok)
        self.assertFalse(auth.attempt("my-secret-p").ok)

    def test_set_overwrites_a_previous_password_and_unlocks(self):
        self.cfg_path.write_text(json.dumps({
            "web": {"password": "old-pass", "max_attempts": 3}}),
            encoding="utf-8")
        auth = DashboardAuth(make_cfg(self.tmp, "old-pass"),
                             config_path=self.cfg_path)
        for _ in range(3):
            auth.attempt("nope")
        self.assertTrue(auth.locked())
        write_password(self.cfg_path, "another-pass", rounds=1000)
        self.assertFalse(auth.locked())      # a changed value clears the lock
        self.assertTrue(auth.attempt("another-pass").ok)


class TestLockout(TempCase):
    def test_lock_after_three_wrong_attempts(self):
        a = self.auth("right-pass")
        self.assertTrue(a.enabled())
        for i in (1, 2):
            res = a.attempt("wrong")
            self.assertEqual(res.status, "BAD_PASSWORD")
            self.assertEqual(res.attempts_left, 3 - i)
            self.assertFalse(res.locked)
        third = a.attempt("wrong")
        self.assertEqual(third.status, "LOCKED")
        self.assertTrue(third.locked)
        self.assertEqual(third.attempts_left, 0)
        # even the CORRECT password is refused while locked
        self.assertTrue(a.locked())
        blocked = a.attempt("right-pass")
        self.assertTrue(blocked.locked)
        self.assertFalse(blocked.ok)
        # the public lock text must not hand out the recovery path
        self.assertNotIn("config.json", blocked.message)
        self.assertNotIn("web.password", blocked.message)
        self.assertIn("قفل", blocked.message)

    def test_lockout_persists_across_restart(self):
        a = self.auth("right-pass")
        for _ in range(3):
            a.attempt("wrong")
        fresh = DashboardAuth(make_cfg(self.tmp, "right-pass"),
                              config_path=self.cfg_path)
        self.assertTrue(fresh.locked())
        self.assertFalse(fresh.attempt("right-pass").ok)

    def test_new_password_in_config_unlocks(self):
        a = self.auth("old-pass")
        for _ in range(3):
            a.attempt("wrong")
        self.assertTrue(a.locked())
        # the documented recovery: edit config.json (no restart)
        self.write_config("brand-new")
        self.assertFalse(a.locked())
        self.assertEqual(a.attempt("old-pass").status, "BAD_PASSWORD")
        res = a.attempt("brand-new")
        self.assertTrue(res.ok)
        self.assertEqual(res.status, "OK")

    def test_auto_unlock_seconds(self):
        a = self.auth("pw", lockout_seconds=1)
        for _ in range(3):
            a.attempt("wrong")
        self.assertTrue(a.locked())
        self.assertGreater(a.status()["lock_seconds_left"], 0)
        import time as _t
        _t.sleep(1.2)
        self.assertFalse(a.locked())

    def test_no_password_configured(self):
        a = self.auth("")
        self.assertFalse(a.enabled())
        res = a.attempt("anything")
        self.assertEqual(res.status, "NO_PASSWORD")
        self.assertFalse(res.ok)

    def test_attempt_log_never_stores_the_password(self):
        a = self.auth("right-pass")
        a.attempt("wrong-secret")
        raw = (self.tmp / "data" / "web_auth.json").read_text(encoding="utf-8")
        self.assertNotIn("wrong-secret", raw)
        self.assertNotIn("right-pass", raw)


class TestSessions(TempCase):
    def test_session_lifecycle(self):
        a = self.auth("pw")
        tok = a.create_session()
        self.assertTrue(a.session_valid(tok))
        self.assertFalse(a.session_valid("garbage"))
        self.assertFalse(a.session_valid(""))
        a.revoke(tok)
        self.assertFalse(a.session_valid(tok))

    def test_password_change_kills_sessions(self):
        a = self.auth("pw")
        tok = a.create_session()
        self.assertTrue(a.session_valid(tok))
        self.write_config("other-pw")
        self.assertFalse(a.session_valid(tok))

    def test_expiry(self):
        a = self.auth("pw", session_hours=0.01)   # 36 seconds
        tok = a.create_session()
        self.assertTrue(a.session_valid(tok))
        a._sessions[tok]["expires_at"] = 0
        self.assertFalse(a.session_valid(tok))

    def test_token_is_not_derived_from_password(self):
        a = self.auth("pw")
        tok = a.create_session()
        self.assertNotIn("pw", tok)
        self.assertGreaterEqual(len(tok), 32)


class TestHttpGate(TempCase):
    """End-to-end through the real HTTP handler."""

    def setUp(self):
        super().setUp()
        self.cfg = make_cfg(self.tmp, "http-pass")
        self.write_config("http-pass")
        self.server, self.port, self.auth_obj = self.start_server(self.cfg)

    def start_server(self, cfg: Config, auth: DashboardAuth | None = None):
        """Bind a real server on a free loopback port and stop it cleanly
        (shutdown -> join -> close, so teardown cannot race the accept loop)."""
        from nobitex_bot.web import BotHTTPServer
        auth = auth or DashboardAuth(cfg, config_path=self.cfg_path)
        server = BotHTTPServer(("127.0.0.1", 0), StubEngine(cfg), auth)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.addCleanup(stop)
        return server, server.server_address[1], auth

    def req(self, method: str, path: str, body: dict | None = None,
            cookie: str = "", origin: str = ""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(payload))
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        data = res.read().decode("utf-8", "replace")
        out = (res.status, dict(res.getheaders()), data)
        conn.close()
        return out

    def login(self, password: str):
        status, headers, body = self.req("POST", "/api/auth/login",
                                         {"password": password})
        return status, headers, json.loads(body)

    # ---------------------------------------------------------- unauthenticated
    def test_api_is_401_before_login(self):
        status, _h, body = self.req("GET", "/api/status")
        self.assertEqual(status, 401)
        self.assertTrue(json.loads(body)["auth_required"])

    def test_root_serves_login_page_not_the_app(self):
        status, headers, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("ورود به داشبورد", body)
        self.assertNotIn("holdingsBox", body)

    def test_index_and_unknown_static_are_gated(self):
        status, _h, body = self.req("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertIn("ورود به داشبورد", body)
        status, _h, _b = self.req("GET", "/api/config")
        self.assertEqual(status, 401)

    def test_status_endpoint_is_public_and_password_free(self):
        status, _h, body = self.req("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertTrue(doc["password_set"])
        self.assertFalse(doc["authenticated"])
        self.assertEqual(doc["max_attempts"], 3)
        self.assertNotIn("http-pass", body)
        self.assertNotIn("config.json", body)
        self.assertNotIn("web.password", body)

    def test_login_page_leaks_no_internal_paths(self):
        _s, _h, body = self.req("GET", "/login")
        self.assertNotIn("config.json", body)
        self.assertNotIn("web.password", body)
        self.assertNotIn("webauth", body)

    def test_post_control_is_401_before_login(self):
        status, _h, _b = self.req("POST", "/api/control/cancel_bot_orders", {})
        self.assertEqual(status, 401)

    # --------------------------------------------------------------- lockout
    def test_three_wrong_passwords_lock_over_http(self):
        self.assertEqual(self.login("nope")[0], 401)
        self.assertEqual(self.login("nope")[0], 401)
        status, _h, doc = self.login("nope")
        self.assertEqual(status, 423)
        self.assertTrue(doc["locked"])
        self.assertEqual(self.login("http-pass")[0], 423)   # correct, still locked
        self.assertEqual(self.req("GET", "/api/status")[0], 401)

    def test_config_edit_unlocks_over_http(self):
        for _ in range(3):
            self.login("nope")
        self.write_config("fresh-pass")
        status, headers, doc = self.login("fresh-pass")
        self.assertEqual(status, 200)
        self.assertTrue(doc["ok"])
        self.assertIn("redirect", doc)

    # --------------------------------------------------------------- session
    def test_login_sets_hardened_cookie_and_grants_access(self):
        status, headers, doc = self.login("http-pass")
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn(COOKIE_NAME + "=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        token = cookie.split(COOKIE_NAME + "=", 1)[1].split(";", 1)[0]
        status, headers, body = self.req("GET", "/api/status",
                                        cookie="%s=%s" % (COOKIE_NAME, token))
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["running"])
        # already signed in -> /login bounces to the dashboard
        status, headers, _b = self.req("GET", "/login",
                                       cookie="%s=%s" % (COOKIE_NAME, token))
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/")

    def test_logout_revokes_the_session(self):
        _s, headers, _d = self.login("http-pass")
        token = headers["Set-Cookie"].split(COOKIE_NAME + "=", 1)[1] \
            .split(";", 1)[0]
        status, headers, _b = self.req("POST", "/api/auth/logout", {},
                                       cookie="%s=%s" % (COOKIE_NAME, token))
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))
        status, _h, _b = self.req("GET", "/api/status",
                                  cookie="%s=%s" % (COOKIE_NAME, token))
        self.assertEqual(status, 401)

    def test_successful_login_resets_failed_counter(self):
        self.login("nope")
        self.login("http-pass")
        self.assertEqual(self.auth_obj.status()["fail_count"], 0)

    # ------------------------------------------------------------ csrf/hardening
    def test_cross_origin_mutation_is_refused(self):
        status, _h, _b = self.req("POST", "/api/auth/login",
                                  {"password": "http-pass"},
                                  origin="http://evil.example")
        self.assertEqual(status, 403)
        # the refused call must not count as a failed attempt either
        self.assertEqual(self.auth_obj.status()["fail_count"], 0)

    def test_security_headers_present(self):
        _s, headers, _b = self.req("GET", "/")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_gate_off_keeps_local_dashboard_open(self):
        cfg = make_cfg(self.tmp, "")
        cfg.engine.data_dir = str(self.tmp / "open_data")
        self.write_config("")
        _srv, port, _auth = self.start_server(cfg)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        res = conn.getresponse()
        body = res.read().decode("utf-8", "replace")
        conn.close()
        self.assertEqual(res.status, 200)
        self.assertIn("holdingsBox", body)


class TestConfigSaveKeepsSecret(TempCase):
    """The settings saves must never lose the stored dashboard password."""

    def setUp(self):
        super().setUp()
        import nobitex_bot.config as cfg_mod
        self.cfg_mod = cfg_mod
        self.real_path = cfg_mod.DEFAULT_CONFIG_PATH
        self.cfg_mod.DEFAULT_CONFIG_PATH = self.cfg_path
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        self.cfg_mod.DEFAULT_CONFIG_PATH = self.real_path

    def test_masked_password_is_kept_and_other_sections_survive(self):
        from nobitex_bot.web import save_config
        self.cfg_path.write_text(json.dumps({
            "engine": {"server_port": 8787},
            "exchange": {"mode": "nobitex", "nobitex_username": "me@x"},
            "web": {"password": "stored-secret", "max_attempts": 3},
        }), encoding="utf-8")
        engine = StubEngine(make_cfg(self.tmp, "stored-secret"))
        # what the dashboard posts: the full copy with the mask in place
        res = save_config(engine, {"web": {"password": "***",
                                           "max_attempts": 5}})
        self.assertTrue(res["ok"])
        doc = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["web"]["password"], "stored-secret")
        self.assertEqual(doc["web"]["max_attempts"], 5)
        # untouched sections are still there
        self.assertEqual(doc["exchange"]["mode"], "nobitex")
        self.assertEqual(doc["exchange"]["nobitex_username"], "me@x")
        self.assertEqual(doc["engine"]["server_port"], 8787)

    def test_new_password_replaces_the_stored_one(self):
        from nobitex_bot.web import save_config
        self.cfg_path.write_text(json.dumps({"web": {"password": "old"}}),
                                 encoding="utf-8")
        engine = StubEngine(make_cfg(self.tmp, "old"))
        save_config(engine, {"web": {"password": "brand-new"}})
        doc = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["web"]["password"], "brand-new")
        # and the live auth module picks the new value up WITHOUT a restart
        auth = DashboardAuth(make_cfg(self.tmp, "old"),
                             config_path=self.cfg_path)
        self.assertTrue(auth.attempt("brand-new").ok)


class TestRemoteGuard(TempCase):
    def test_remote_bind_without_password_is_refused(self):
        from nobitex_bot.web import serve
        cfg = make_cfg(self.tmp, "")
        cfg.web.allow_remote_without_password = False
        auth = DashboardAuth(cfg, config_path=self.cfg_path)
        with self.assertRaises(SystemExit) as ctx:
            serve(StubEngine(cfg), "0.0.0.0", 0, auth=auth)
        self.assertIn("web.password", str(ctx.exception))

    def test_remote_bind_allowed_when_explicitly_accepted(self):
        # guard must be the ONLY thing stopping it: with the flag set, serve()
        # proceeds to bind. We run it in a thread and shut it down at once.
        from nobitex_bot.web import BotHTTPServer
        cfg = make_cfg(self.tmp, "")
        cfg.web.allow_remote_without_password = True
        auth = DashboardAuth(cfg, config_path=self.cfg_path)
        self.assertFalse(auth.enabled())
        server = BotHTTPServer(("127.0.0.1", 0), StubEngine(cfg), auth)
        self.assertEqual(server.auth, auth)
        server.server_close()


if __name__ == "__main__":
    unittest.main()
