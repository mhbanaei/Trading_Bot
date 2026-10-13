"""Pre-flight self-check for a fresh machine / VPS — `python run.py --doctor`.

Copying the bot to a server exposes two problems the local Windows machine
never shows:

1. ``engine.server_host`` defaults to ``127.0.0.1``, so the dashboard listens
   on the loopback interface only: ``http://<server-ip>:8787`` from your PC
   answers nothing at all (the OS refuses the port). Changing it to
   ``0.0.0.0`` requires a dashboard password, otherwise the server refuses
   to start on purpose.
2. A minimal Linux image ships without a CA bundle, so every HTTPS call ends
   with ``CERTIFICATE_VERIFY_FAILED — unable to get local issuer certificate``
   and Nobitex login fails. Windows uses its own ROOT store, hence "works
   locally, broken on the VPS".

This module answers "what is wrong with THIS host" without touching the
account: it inspects the trust store, does one real TLS handshake to the
exchange, compares the host clock with the server's ``Date`` header, and
reports the dashboard bind/port/firewall situation with the exact repair.

Exits ``2`` when a check FAILs, so it can gate a start script.
"""
from __future__ import annotations

import http.client
import socket
import ssl
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import tls
from .config import Config
from .nobitex_api import API_BASE
from .webauth import is_loopback_host

OK, WARN, FAIL = "OK", "WARN", "FAIL"
UA = "NobitexBotDoctor/1.0"
WRAP = 96


@dataclass
class Check:
    """One line of the report: verdict + evidence + how to repair it."""

    name: str
    status: str = OK
    detail: str = ""
    fix: str = ""


def _cn(field) -> str:
    for rdn in field or ():
        for key, value in rdn:
            if key in ("commonName", "organizationName"):
                return str(value)
    return ""


def tls_probe(host: str, timeout: float = 10.0) -> dict:
    """One real HTTPS handshake + GET against ``host`` — does it verify?

    Returns evidence either way: TLS version, certificate CN/issuer/expiry,
    HTTP status and the clock skew read from the ``Date`` header.
    """
    out = {"host": host, "ok": False, "cert_error": False, "error": "",
           "tls": "", "subject": "", "issuer": "", "expires_days": None,
           "status": 0, "skew_s": None}
    conn = None
    try:
        conn = http.client.HTTPSConnection(host, 443, timeout=timeout,
                                           context=tls.context())
        conn.request("GET", "/", headers={"User-Agent": UA})
        resp = conn.getresponse()
        out["status"] = resp.status
        try:
            out["tls"] = conn.sock.version() or ""
            cert = conn.sock.getpeercert() or {}
        except Exception:
            cert = {}
        out["subject"] = _cn(cert.get("subject"))
        out["issuer"] = _cn(cert.get("issuer"))
        not_after = cert.get("notAfter")
        if not_after:
            try:
                out["expires_days"] = int(
                    (ssl.cert_time_to_seconds(not_after) - time.time()) // 86400)
            except Exception:
                pass
        date_hdr = resp.getheader("Date") or ""
        if date_hdr:
            try:
                skew = (datetime.now(timezone.utc) -
                        parsedate_to_datetime(date_hdr)).total_seconds()
                out["skew_s"] = int(skew)
            except Exception:
                pass
        try:
            resp.read(1)
        except Exception:
            pass
        out["ok"] = True
    except Exception as exc:                     # noqa: BLE001 — report, don't raise
        out["error"] = tls.describe_error(exc)
        out["cert_error"] = tls.is_cert_error(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def public_ip(timeout: float = 6.0) -> dict:
    """Egress IP + country, over PLAIN HTTP on purpose: it must still work
    when the trust store is the very thing that is broken."""
    out = {"ip": "", "country": "", "code": ""}
    url = "http://ip-api.com/json/?fields=status,query,country,countryCode"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            import json
            doc = json.loads(resp.read().decode("utf-8"))
        out["ip"] = str(doc.get("query") or "")
        out["country"] = str(doc.get("country") or "")
        out["code"] = str(doc.get("countryCode") or "")
    except Exception:
        try:
            req = urllib.request.Request("http://api.ipify.org",
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out["ip"] = resp.read(64).decode("utf-8").strip()
        except Exception:
            pass
    return out


def run_doctor(cfg: Config, probe_net: bool = True) -> list[Check]:
    """Every check, in the order the failures normally appear."""
    import json
    import platform                      # local imports keep --doctor light
    checks: list[Check] = []
    checks.append(Check(
        "runtime", OK,
        "python %s | %s | cwd %s" % (platform.python_version(),
                                     platform.platform(), Path.cwd())))
    checks.append(_trust_check())

    api_host = urlsplit(API_BASE).hostname or "apiv2.nobitex.ir"
    probe = tls_probe(api_host) if probe_net else {
        "host": api_host, "ok": False, "error": "network probe skipped"}
    checks.append(_exchange_tls_check(api_host, probe))
    checks.append(_clock_check(probe))
    checks.append(_egress_check(public_ip() if probe_net else {}))
    checks.append(_account_check(cfg))
    checks.append(_bind_check(cfg))
    checks.append(_port_check(cfg))
    checks.append(_firewall_check(cfg))
    # a single json line makes the report scriptable without parsing the table
    checks.append(Check("report_json", OK, json.dumps(
        {"checks": [c.__dict__ for c in checks]}, ensure_ascii=False)))
    return checks


def _trust_check() -> Check:
    rep = tls.ca_report()
    if not rep["ok"]:
        return Check("trust store", FAIL,
                     "no CA certificates on this host (certifi: %s)"
                     % ("installed" if rep["certifi"] else "missing"),
                     tls.FIX_HINT)
    detail = "%d CA certificates from %s" % (rep["certs"], rep["source"])
    if rep["env_override"]:
        detail += " | override: %s" % rep["env_override"]
    return Check("trust store", OK, detail)


def _exchange_tls_check(host: str, probe: dict) -> Check:
    if probe.get("ok"):
        detail = ("https://%s verified | TLS %s | cert CN=%s by %s | HTTP %s"
                  % (host, probe.get("tls") or "?", probe.get("subject") or "?",
                     probe.get("issuer") or "?", probe.get("status")))
        expires = probe.get("expires_days")
        if expires is not None and expires < 7:
            return Check("exchange https", WARN, detail,
                         "the server certificate expires in %d days - update "
                         "the OS trust store before it does" % expires)
        return Check("exchange https", OK, detail)
    if probe.get("cert_error"):
        return Check("exchange https", FAIL,
                     "https://%s : %s" % (host, probe.get("error", "")),
                     tls.FIX_HINT)
    return Check("exchange https", FAIL,
                 "https://%s unreachable: %s" % (host, probe.get("error", "")),
                 "check the provider's network/outbound filter and that DNS "
                 "resolves; Nobitex also rejects foreign IPs during login")


def _clock_check(probe: dict) -> Check:
    skew = probe.get("skew_s")
    if skew is None:
        return Check("host clock", WARN,
                     "could not read the server Date header - skew unknown",
                     "make sure the clock is right: on Linux `timedatectl` / "
                     "`apt install -y ntp`; a wrong clock breaks TLS too")
    if abs(skew) > 300:
        return Check("host clock", FAIL,
                     "this host is %d s off the exchange (TLS/2FA will fail)"
                     % skew,
                     "enable NTP: `sudo timedatectl set-ntp true` "
                     "(`apt install -y systemd-timesyncd`)")
    return Check("host clock", OK, "%+d s vs the exchange clock" % skew)


def _egress_check(info: dict) -> Check:
    ip = info.get("ip") or ""
    if not ip:
        return Check("egress ip", WARN, "could not determine the public IP",
                     "the dashboard is still reachable by the provider's IP; "
                     "Nobitex login/orders need an IRANIAN exit IP")
    where = info.get("country") or "unknown location"
    detail = "%s (%s)" % (ip, where)
    if info.get("code") and info["code"] != "IR":
        return Check("egress ip", WARN, detail,
                     "Nobitex login and order endpoints usually answer 429 or "
                     "403 from a non-Iranian IP - use an Iranian VPS (or a "
                     "tunnel through one) for the live account")
    return Check("egress ip", OK, detail)


def _account_check(cfg: Config) -> Check:
    mode = (cfg.exchange.mode or "demo").lower()
    live = bool(getattr(cfg.exchange, "live_trading", False))
    if mode != "nobitex":
        return Check("exchange account", WARN, "mode=%s (simulator)" % mode,
                     "set exchange.mode=nobitex plus credentials to trade the "
                     "real account")
    has_login = bool(getattr(cfg.exchange, "nobitex_username", "") and
                     getattr(cfg.exchange, "nobitex_password", ""))
    has_key = bool(getattr(cfg.exchange, "nobitex_api_key", ""))
    data_dir = Path(getattr(cfg.engine, "data_dir", "data") or "data")
    # the real-account session keeps its token in <data_dir>/real (see
    # TradingEngine: NobitexSession(data_dir / "real"))
    candidates = (data_dir / "real" / "credentials.json",
                  data_dir / "credentials.json")
    creds = next((p for p in candidates if p.is_file()), candidates[0])
    stored = creds.is_file()
    if not (has_login or has_key or stored):
        return Check("exchange account", WARN,
                     "mode=nobitex but no credentials found in %s"
                     % " or ".join(str(p) for p in candidates),
                     "log in from the dashboard, or put nobitex_api_key in "
                     "config.json; without it the bot stays read-only")
    return Check("exchange account", OK,
                 "mode=nobitex | live_trading=%s | %s"
                 % (live, "token file present (%s)" % creds
                    if stored else "login from config/config.json"))


def _bind_check(cfg: Config) -> Check:
    host = getattr(cfg.engine, "server_host", "") or "127.0.0.1"
    port = int(getattr(cfg.engine, "server_port", 8787) or 8787)
    has_pw = bool(getattr(cfg.web, "password", "") or "")
    allow = bool(getattr(cfg.web, "allow_remote_without_password", False))
    remote = not is_loopback_host(host)
    if remote and not has_pw and not allow:
        return Check(
            "dashboard bind", FAIL,
            "engine.server_host=%s without a password: the server REFUSES to "
            "start and exits before listening" % host,
            "python -m nobitex_bot.webauth --set \"your-password\"   (or bind "
            "127.0.0.1 and use an SSH tunnel)")
    if remote and not has_pw:
        return Check(
            "dashboard bind", WARN,
            "%s:%d is open to the internet with NO password" % (host, port),
            "set web.password — anyone who reaches this port could place real "
            "orders")
    if not remote:
        return Check(
            "dashboard bind", WARN,
            "bound to %s:%d - reachable ONLY on this machine, so "
            "http://<server-ip>:%d from your PC answers nothing" % (
                host, port, port),
            "options: (a) keep 127.0.0.1 and put an authenticating TLS proxy "
            "in front - ready-made Caddy/nginx configs in deploy/ "
            "(recommended), (b) tunnel from your PC: ssh -L "
            "%d:127.0.0.1:%d user@<server-ip> then open "
            "http://127.0.0.1:%d, (c) expose it directly: set \"server_host\""
            ": \"0.0.0.0\" in config.json AND a dashboard password "
            "(python -m nobitex_bot.webauth --set \"...\")"
            % (port, port, port))
    return Check("dashboard bind", OK, "%s:%d with the password gate ON"
                 % (host, port))


def _probe_dashboard(host: str, port: int, timeout: float = 0.6) -> bool:
    """True when GET /api/auth/status on this address is answered by the bot.

    Used before the bind probe: a wildcard listener we cannot out-bind and
    cannot see would otherwise be reported as a free port (see _port_check).
    """
    addr = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        conn = http.client.HTTPConnection(addr, port, timeout=timeout)
        try:
            conn.request("GET", "/api/auth/status")
            resp = conn.getresponse()
            body = resp.read(4096).decode("utf-8", "replace")
        finally:
            conn.close()
    except Exception:
        return False
    return resp.status == 200 and "password_set" in body


def _port_check(cfg: Config) -> Check:
    host = getattr(cfg.engine, "server_host", "") or "127.0.0.1"
    port = int(getattr(cfg.engine, "server_port", 8787) or 8787)
    if _probe_dashboard(host, port):
        return Check("dashboard port", OK,
                     "%d on %s is already served by this bot's dashboard"
                     % (port, host))
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    addr = "0.0.0.0" if host in ("0.0.0.0", "::") else host
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR must NOT be used here on Windows: two sockets that both
        # set it may hold the same wildcard port, so a foreign owner (IIS
        # http.sys held 8787 on this host) was reported as "free".
        # SO_EXCLUSIVEADDRUSE actually fails, which is the answer we want.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((addr, port))
        return Check("dashboard port", OK, "%d is free on %s" % (port, host))
    except OSError as exc:
        return Check("dashboard port", WARN,
                     "%d on %s is already in use (%s)" % (port, host, exc),
                     "a process we do not recognize holds it - find the owner "
                     "(Linux: ss -ltnp | grep %d / Windows: netstat -ano | "
                     "findstr :%d then tasklist \"//FI PID eq <pid>\"), and "
                     "remember a Windows reserved port answers 10013 instead "
                     "of 10048" % (port, port))
    finally:
        sock.close()


def _firewall_check(cfg: Config) -> Check:
    """Advice for the architecture that is actually configured.

    The two paths in deploy/ have opposite failure modes, so a single generic
    paragraph misleads one of them. With ``server_host: 0.0.0.0`` the bot
    itself publishes the port and the only gate left is a firewall OUTSIDE the
    process; with loopback the TLS proxy owns the public port. Measured
    2026-10-11 on this host: IIS http.sys held 8787 and answered every
    external request with ERR_EMPTY_RESPONSE while a bind probe still called
    the port free - which is why the direct branch names that trap.
    """
    port = int(getattr(cfg.engine, "server_port", 8787) or 8787)
    host = str(getattr(cfg.engine, "server_host", "") or "127.0.0.1")
    if host in ("127.0.0.1", "localhost", "::1"):
        return Check(
            "remote reachability", WARN,
            "direct exposure is OFF (%s): from your PC open the URL your TLS "
            "proxy publishes - deploy/ puts https://<name>:%d on the public "
            "IP while the bot stays closed on loopback" % (host, port),
            "open the proxy's %d/tcp in the provider's firewall AND in the OS "
            "(ufw allow %d/tcp), and never let two services share one port - "
            "and do not \"fix\" an unreachable dashboard with \"server_host\": "
            "\"0.0.0.0\" while a proxy runs on the same port" % (port, port))
    return Check(
        "remote reachability", WARN,
        "direct exposure is ON (%s): http://<public-ip>:%d/ is served by the "
        "bot itself, behind the password gate and without TLS (deploy/ has "
        "the TLS proxy path)" % (host, port),
        "if it does not open from your PC while this report says the bind is "
        "OK, the drop is outside the bot: allow %d/tcp in the OS firewall "
        "(Windows: netsh advfirewall firewall add rule name=\"nobitex "
        "dashboard %d\" dir=in action=allow protocol=TCP localport=%d "
        "profile=any / Linux: ufw allow %d/tcp) AND in the provider's panel "
        "- and make sure no other service (IIS http.sys, Caddy) holds %d/tcp"
        % (port, port, port, port, port))


def verdict(checks: list[Check]) -> str:
    fails = [c for c in checks if c.status == FAIL]
    warns = [c for c in checks if c.status == WARN]
    oks = [c for c in checks if c.status == OK]
    line = "verdict: %d FAIL, %d WARN, %d OK" % (len(fails), len(warns), len(oks))
    if fails:
        line += "\nfix the FAIL lines above before starting the engine."
    return line


def render(checks: list[Check]) -> str:
    lines = ["=" * WRAP,
             " Nobitex bot - pre-flight check (python run.py --doctor)",
             "=" * WRAP]
    for c in checks:
        if c.name == "report_json":
            continue
        lines.append("[%-4s] %-18s %s" % (c.status, c.name, c.detail))
        if c.fix:
            lines.append(textwrap.fill(c.fix, WRAP, initial_indent=" " * 24,
                                       subsequent_indent=" " * 24))
    lines.append("-" * WRAP)
    lines.append(verdict(checks))
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 2 if any(c.status == FAIL for c in checks) else 0


def main(argv: list[str] | None = None) -> int:
    """`python -m nobitex_bot.doctor` — same report, standalone."""
    from .config import load_config
    cfg = load_config()
    checks = run_doctor(cfg)
    print(render(checks))
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
