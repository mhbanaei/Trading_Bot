"""Dashboard password gate — access control for a VPS-exposed dashboard.

Why it exists
-------------
The dashboard can start/stop the engine and place REAL orders, so the moment
it listens on a public address it needs a lock. This module implements:

* One password read LIVE from ``config.json`` (``web.password``). The value may
  be plaintext or a PBKDF2-SHA256 hash (``pbkdf2_sha256$<rounds>$<salt>$<hash>``
  — generate one with ``python -m nobitex_bot.webauth --hash``). Editing
  config.json is the documented recovery path: a NEW password value clears the
  lock immediately.
* Lockout after ``web.max_attempts`` wrong passwords (3 by default). The lock
  is persisted in ``data/web_auth.json`` so restarting the bot cannot bypass
  it, and it clears only when the configured password changes (or after
  ``web.lockout_seconds`` when that is > 0).
* Constant-time comparisons (``hmac.compare_digest``), a per-failure delay and
  no distinction between "wrong password" and "unknown" responses that could
  leak hints.
* Server-side sessions: opaque 256-bit tokens with an absolute TTL, revocable,
  never derived from the password — a leaked cookie cannot reveal it and
  changing the password kills every session at once.

Nothing here logs, stores or returns the password itself; only a one-way
fingerprint of the CONFIG VALUE (not the secret) is persisted, so we can tell
"the user changed the password in config.json" from "the same password".
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, ROOT, Config, load_config

COOKIE_NAME = "nobitex_bot_session"
PBKDF2_ROUNDS = 200_000
HASH_PREFIX = "pbkdf2_sha256$"
MAX_SESSIONS = 12              # per dashboard, oldest evicted
ATTEMPT_LOG_KEEP = 12          # recent attempts kept in the lock file


def hash_password(plain: str, rounds: int = PBKDF2_ROUNDS) -> str:
    """Return a ready-to-paste ``pbkdf2_sha256$...`` value for config.json."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", (plain or "").encode("utf-8"),
                                 salt, int(rounds))
    return "%s%d$%s$%s" % (HASH_PREFIX, int(rounds), salt.hex(), digest.hex())


def verify_password(stored: str, given: str) -> bool:
    """Constant-time check of ``given`` against the config value ``stored``.

    Supports both a plaintext config value and a ``pbkdf2_sha256$`` hash.
    Never raises: any malformed stored value means "does not match".
    """
    stored = (stored or "").strip()
    if not stored:
        return False
    given = given or ""
    if stored.startswith(HASH_PREFIX):
        try:
            _p, rounds, salt_hex, hash_hex = stored.split("$", 3)
            digest = hashlib.pbkdf2_hmac("sha256", given.encode("utf-8"),
                                         bytes.fromhex(salt_hex), int(rounds))
        except Exception:
            return False
        return hmac.compare_digest(digest.hex(), hash_hex)
    return hmac.compare_digest(stored.encode("utf-8"), given.encode("utf-8"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_password(config_path: Path, plain: str,
                   rounds: int = PBKDF2_ROUNDS) -> str:
    """Store ``plain`` as a PBKDF2 hash in config.json -> web.password.

    MERGES with the existing file (every other section/knob survives) and
    never writes the plaintext password. Returns the hash that was stored.
    """
    config_path = Path(config_path)
    doc: dict = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                doc = loaded
        except Exception:
            doc = {}
    web = doc.get("web")
    web = dict(web) if isinstance(web, dict) else {}
    hashed = hash_password(plain, rounds)
    web["password"] = hashed
    web.setdefault("max_attempts", 3)
    web.setdefault("lockout_seconds", 0)
    web.setdefault("session_hours", 12.0)
    web.setdefault("failure_delay_s", 0.8)
    web.setdefault("allow_remote_without_password", False)
    doc["web"] = web
    config_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return hashed


def is_loopback_host(host: str) -> bool:
    """True for 127.0.0.0/8, ::1, localhost and an empty bind host.

    Used to decide whether the dashboard is exposed beyond this machine:
    a public listener without a password is refused (it can place real
    orders), while a local one keeps working exactly as before.
    """
    h = (host or "").strip().strip("[]").lower()
    if h in ("", "localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def host_header_allowed(bound_host: str, header: str | None,
                        gate_on: bool) -> bool:
    """Reject a Host header that cannot belong to this dashboard.

    With the password gate OFF on a loopback bind, any web page the user
    visits can point a hostname at 127.0.0.1 (DNS rebinding) and drive the
    API from their browser. Requiring the Host to actually be loopback
    closes that hole. When a password is configured — or the dashboard is
    bound to a public interface, where a reverse proxy legitimately sends
    its own domain in Host — the syntax is still checked but the name is
    not restricted, so documented Caddy/nginx setups keep working.
    """
    raw = (header or "").strip()
    if not raw:
        return True                      # HTTP/1.0 clients, health probes
    name = raw
    if name.startswith("["):             # [::1]:8787
        end = name.find("]")
        name = name[1:end] if end != -1 else name
    elif name.count(":") == 1:            # host:port
        name = name.rsplit(":", 1)[0]
    name = name.strip().strip(".").lower()
    if not name:
        return False
    if is_loopback_host(bound_host) and not gate_on:
        return is_loopback_host(name)
    return bool(re.fullmatch(r"[a-z0-9._-]+", name))


@dataclass
class Attempt:
    """Result of one login try (never carries the password)."""

    ok: bool = False
    status: str = "BAD_PASSWORD"   # OK|BAD_PASSWORD|LOCKED|NO_PASSWORD
    attempts_left: int = 0
    locked: bool = False
    lock_seconds_left: int = 0
    message: str = ""
    first_failure: bool = False


class DashboardAuth:
    """Password gate + session store for the bot's web dashboard."""

    def __init__(self, cfg: Config, events=None, config_path: Path | None = None):
        self.cfg = cfg
        self.events = events
        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        data_dir = Path(getattr(cfg.engine, "data_dir", "data") or "data")
        self.data_dir = data_dir if data_dir.is_absolute() else ROOT / data_dir
        self.state_path = self.data_dir / "web_auth.json"
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}
        self._state = self._load_state()
        # first call syncs: a password edited while the bot was DOWN must
        # unlock immediately on the next attempt
        with self._lock:
            self._sync_locked()

    # ------------------------------------------------------------ config
    def _cfg_password(self) -> str:
        """Password value from config.json, re-read on every attempt.

        Re-reading means the user can rotate the password (or unlock a locked
        dashboard) by editing config.json on the VPS — no restart. Falls back
        to the in-memory config when the file is momentarily unreadable.
        """
        try:
            if self.config_path.exists():
                doc = json.loads(self.config_path.read_text(encoding="utf-8"))
                web = doc.get("web") or {}
                if isinstance(web, dict) and "password" in web:
                    return str(web.get("password") or "")
        except Exception:
            pass
        return str(getattr(self.cfg.web, "password", "") or "")

    def _fingerprint(self, raw: str) -> str:
        """One-way fingerprint of the config VALUE (not the secret)."""
        return hashlib.sha256(("v1|" + (raw or "")).encode("utf-8")).hexdigest()[:32]

    def enabled(self) -> bool:
        """True when a password is configured (gate active)."""
        return bool(self._cfg_password().strip())

    # ------------------------------------------------------- state file
    def _load_state(self) -> dict:
        try:
            if self.state_path.exists():
                doc = json.loads(self.state_path.read_text(encoding="utf-8"))
                return {
                    "fail_count": int(doc.get("fail_count") or 0),
                    "locked_at": doc.get("locked_at"),
                    "fingerprint": str(doc.get("fingerprint") or ""),
                    "attempts": list(doc.get("attempts") or []),
                }
        except Exception:
            pass
        return {"fail_count": 0, "locked_at": None, "fingerprint": "",
                "attempts": []}

    def _save_state(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    def _note(self, ok: bool, ip: str = "") -> None:
        self._state.setdefault("attempts", []).append(
            {"at": now_iso(), "ip": ip or "", "ok": bool(ok)})
        self._state["attempts"] = self._state["attempts"][-ATTEMPT_LOG_KEEP:]

    # ------------------------------------------------------------ lock
    def _lockout_seconds(self) -> int:
        try:
            return max(0, int(getattr(self.cfg.web, "lockout_seconds", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _failure_delay(self) -> float:
        """Delay applied to every wrong password (anti brute-force)."""
        try:
            return max(0.0, float(getattr(self.cfg.web, "failure_delay_s", 0.8)
                                   or 0))
        except (TypeError, ValueError):
            return 0.8

    def _sync_locked(self) -> None:
        """Reconcile the persisted lock with the CURRENT config password.

        A different password value = the documented unlock: counters, lock and
        every existing dashboard session are dropped.
        """
        fp = self._fingerprint(self._cfg_password())
        if fp != self._state.get("fingerprint"):
            self._state["fingerprint"] = fp
            self._state["fail_count"] = 0
            self._state["locked_at"] = None
            self._sessions.clear()
            self._save_state()

    def locked(self) -> bool:
        with self._lock:
            self._sync_locked()
            return self._locked_locked()

    def _locked_locked(self) -> bool:
        if not self._state.get("locked_at"):
            return False
        secs = self._lockout_seconds()
        if secs <= 0:
            # stay locked until the password in config.json changes
            return True
        return self._lock_seconds_left_locked() > 0

    def _lock_seconds_left_locked(self) -> int:
        try:
            at = datetime.fromisoformat(str(self._state.get("locked_at")))
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
        except Exception:
            return 0
        left = (at + timedelta(seconds=self._lockout_seconds())
                - datetime.now(timezone.utc)).total_seconds()
        # ceil, not trunc: a fraction of a remaining second must still count
        # as "locked" — otherwise a short lockout_seconds never registers
        return max(0, int(math.ceil(left)))

    # ------------------------------------------------------- attempts
    def attempt(self, password: str, ip: str = "") -> Attempt:
        """Check one login try against the config password + lock state."""
        max_attempts = max(1, int(getattr(self.cfg.web, "max_attempts", 3) or 3))
        with self._lock:
            self._sync_locked()
            if not self.enabled():
                # NOTE: never name the config file or the field here — this
                # text is returned to whoever can reach the port.
                return Attempt(status="NO_PASSWORD", locked=False,
                               message="ورود در حال حاضر امکان‌پذیر نیست")
            if self._locked_locked():
                left = self._lock_seconds_left_locked()
                return Attempt(status="LOCKED", locked=True,
                               lock_seconds_left=left, attempts_left=0,
                               message=self._lock_message(left))
            ok = verify_password(self._cfg_password(), password)
            self._note(ok, ip)
            if ok:
                self._state["fail_count"] = 0
                self._state["locked_at"] = None
                self._save_state()
                self._publish("OK", ip, 0)
                return Attempt(ok=True, status="OK",
                               attempts_left=max_attempts,
                               message="ورود موفق")
            self._state["fail_count"] = int(self._state.get("fail_count") or 0) + 1
            fails = self._state["fail_count"]
            left = max(0, max_attempts - fails)
            first = fails == 1
            if left <= 0:
                self._state["locked_at"] = now_iso()
            self._save_state()
            locked = self._locked_locked()
            self._publish("LOCKED" if locked else "BAD_PASSWORD", ip, left)
        # slowdown OUTSIDE the lock: brute force must not be fast, and a
        # failed login must not block the engine's threads meanwhile
        if not locked:
            delay = self._failure_delay()
            if delay > 0:
                time.sleep(min(delay, 3.0))
        msg = (self._lock_message(self._lock_seconds_left_locked()) if locked
               else "پسورد اشتباه است — %d تلاش باقی مانده" % left)
        return Attempt(status="LOCKED" if locked else "BAD_PASSWORD",
                       locked=locked, lock_seconds_left=(
                           self._lock_seconds_left_locked() if locked else 0),
                       attempts_left=left, message=msg, first_failure=first)

    def _lock_message(self, seconds_left: int = 0) -> str:
        """Public lock text — deliberately free of ANY recovery hint.

        It is shown to whoever can reach the port, so it must not name the
        config file, the field, or how the lock is cleared. The operator gets
        the real recovery path from the server console instead.
        """
        if seconds_left > 0:
            return ("دسترسی موقتاً قفل شد (پسورد %d بار اشتباه). %d ثانیه "
                    "دیگر دوباره امتحان کنید."
                    % (int(getattr(self.cfg.web, "max_attempts", 3) or 3),
                       seconds_left))
        return ("دسترسی قفل شده است. تا زمانی که مدیر دسترسی را بازنشانی "
                "نکند، ورود ممکن نیست.")

    def _publish(self, kind: str, ip: str, attempts_left: int) -> None:
        if self.events is None:
            return
        try:
            self.events.publish("SYSTEM_ERROR", "web_auth", "", {
                "where": "dashboard_login",
                "code": "AUTH_" + kind,
                "message": {"OK": "ورود موفق به داشبورد",
                            "BAD_PASSWORD": "پسورد داشبورد اشتباه بود",
                            "LOCKED": "داشبورد قفل شد (۳ تلاش اشتباه)"}
                           .get(kind, kind),
                "ip": ip or "-",
                "attempts_left": attempts_left,
            })
        except Exception:
            pass

    # ------------------------------------------------------- sessions
    def session_hours(self) -> float:
        try:
            return max(0.01, float(getattr(self.cfg.web, "session_hours", 12.0)
                                   or 12.0))
        except (TypeError, ValueError):
            return 12.0

    def create_session(self, ip: str = "", user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._drop_expired_locked()
            self._sessions[token] = {
                "expires_at": time.time() + self.session_hours() * 3600.0,
                "ip": ip or "",
                "ua": (user_agent or "")[:160],
                "created_at": now_iso(),
            }
            while len(self._sessions) > MAX_SESSIONS:
                oldest = min(self._sessions,
                             key=lambda t: self._sessions[t]["expires_at"])
                self._sessions.pop(oldest, None)
        return token

    def session_valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            self._sync_locked()
            self._drop_expired_locked()
            meta = self._sessions.get(token)
            return bool(meta and meta["expires_at"] > time.time())

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def session_expires_in(self, token: str) -> int:
        with self._lock:
            meta = self._sessions.get(token) or {}
            return max(0, int(meta.get("expires_at", 0) - time.time()))

    def _drop_expired_locked(self) -> None:
        now = time.time()
        for t in [t for t, m in self._sessions.items()
                  if m.get("expires_at", 0) <= now]:
            self._sessions.pop(t, None)

    # --------------------------------------------------------- status
    def status(self) -> dict:
        """Safe, password-free snapshot for the login page."""
        with self._lock:
            self._sync_locked()
            max_attempts = max(1, int(getattr(self.cfg.web, "max_attempts", 3)
                                      or 3))
            locked = self._locked_locked()
            return {
                "password_set": self.enabled(),
                "locked": locked,
                "lock_seconds_left": (self._lock_seconds_left_locked()
                                      if locked else 0),
                "fail_count": int(self._state.get("fail_count") or 0),
                "max_attempts": max_attempts,
                "attempts_left": max(0, max_attempts -
                                     int(self._state.get("fail_count") or 0)),
                "lockout_seconds": self._lockout_seconds(),
                "session_hours": self.session_hours(),
                "failure_delay_s": self._failure_delay(),
            }


def _main() -> int:
    """``python -m nobitex_bot.webauth --hash`` — make a config.json value."""
    import argparse
    ap = argparse.ArgumentParser(
        description="Dashboard password helper (config.json web.password)")
    ap.add_argument("--hash", metavar="PASSWORD",
                    help="print a pbkdf2_sha256 value to paste into config.json")
    ap.add_argument("--check", metavar="PASSWORD",
                    help="verify PASSWORD against the configured value")
    ap.add_argument("--set", metavar="PASSWORD",
                    help=("write PASSWORD (hashed) into config.json -> "
                          "web.password; every other setting is kept"))
    ap.add_argument("--rounds", type=int, default=PBKDF2_ROUNDS)
    ap.add_argument("--config", metavar="PATH", default=None,
                    help="config.json to use (default: the project one)")
    ap.add_argument("--show-status", action="store_true",
                    help="show whether a dashboard password is configured")
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    if args.hash:
        print(hash_password(args.hash, args.rounds))
        return 0
    if args.set:
        if len(args.set) < 6:
            print("[x] password too short — use at least 6 characters")
            return 2
        write_password(cfg_path, args.set, args.rounds)
        print("[ok] dashboard password set (stored as a PBKDF2 hash) in %s"
              % cfg_path)
        print("     the password itself is NOT stored or printed anywhere — "
              "the dashboard login page now asks for it")
        return 0
    if args.check:
        # strict=False: the password helper must keep working even when the
        # rest of config.json is invalid — that is how the file gets fixed
        cfg = load_config(cfg_path, strict=False)
        ok = verify_password(str(getattr(cfg.web, "password", "") or ""),
                             args.check)
        print("OK" if ok else "MISMATCH")
        return 0 if ok else 1
    if args.show_status:
        cfg = load_config(cfg_path, strict=False)
        auth = DashboardAuth(cfg, config_path=cfg_path)
        st = auth.status()
        print("password set: %s | locked: %s | fail_count: %s/%s"
              % (st["password_set"], st["locked"], st["fail_count"],
                 st["max_attempts"]))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
