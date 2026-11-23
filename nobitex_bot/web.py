"""Web server + REST API — serves the Persian RTL dashboard and JSON API.

Standard library only (http.server) so the bot runs with zero installs.
Endpoints under /api/* are read-only views of engine state; a couple of
control endpoints (start/stop/safe-mode/reset) are POST-only.
"""
from __future__ import annotations

import gzip
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .diagnostics import record_error
from .engine import TradingEngine
from .nobitex_api import NobitexAPI
from .webauth import (COOKIE_NAME, DashboardAuth, host_header_allowed,
                      is_loopback_host)

STATIC_DIR = Path(__file__).resolve().parent / "web"


class BotHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, engine: TradingEngine, auth: DashboardAuth):
        self.engine = engine
        self.auth = auth
        super().__init__(addr, Handler)

    def handle_error(self, request, client_address) -> None:
        """A client that hung up mid-request is normal internet noise.

        Port scanners, closed tabs and half-open sockets used to make
        socketserver print a full traceback per drop («Exception occurred
        during processing of request from …»), flooding the very console
        the quiet mode exists to keep clean — on a public IP that spam
        never stopped. Connection-level errors are swallowed here; a real
        bug inside a request handler still prints its traceback.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "NobitexBot/1.0"
    # a scanner opens a socket and sends nothing; without a timeout that
    # connection keeps a worker thread for as long as it stays open. After
    # this many idle seconds the socket is closed (the quiet log swallows
    # the timeout notice), so abandoned connections cannot pile up.
    timeout = 30

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):  # quiet access log
        pass

    def _security_headers(self) -> None:
        """Baseline hardening for every response (SS101: no clickjacking,
        no MIME sniffing, no leaking the dashboard to other sites)."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")

    BANDWIDTH = True   # gzip on compressed-capable clients (metered users)

    def _wants_gzip(self) -> bool:
        ae = (self.headers.get("Accept-Encoding") or "").lower()
        return self.BANDWIDTH and "gzip" in ae

    def _json(self, obj, status: int = 200, extra: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        # /api/status (markets_view over ~500 live markets) is by far the
        # dashboard's heaviest payload and polls every few seconds — gzip
        # typically cuts it ~85-90% on the exchange's repetitive JSON rows
        if self._wants_gzip() and len(body) > 512:
            body = gzip.compress(body, 6)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, extra: dict | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _static(self, rel: str) -> None:
        path = (STATIC_DIR / rel).resolve()
        if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(path.suffix.lower(), "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        # no-store: a dashboard tab left open for days must never keep an
        # old JS copy after the HTML is updated (the reload then shows the
        # new editor immediately).
        self.send_header("Cache-Control",
                         "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self._security_headers()
        if self._wants_gzip() and path.suffix.lower() in (
                ".html", ".js", ".css") and len(body) > 512:
            body = gzip.compress(body, 6)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            pass
        return {}

    # ----------------------------------------------------------- the gate
    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _client_ip(self) -> str:
        try:
            return self.client_address[0] or ""
        except Exception:
            return ""

    def _authed(self) -> bool:
        """True when the dashboard password is off (local, no gate) or the
        request carries a valid server-side session cookie."""
        auth = self.server.auth
        if not auth.enabled():
            return True
        return auth.session_valid(self._cookie(COOKIE_NAME))

    def _same_origin(self) -> bool:
        """CSRF guard for mutating calls: when a browser sends Origin it must
        match the Host we were reached on (curl/CLI clients send none)."""
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        if origin.lower() == "null":
            return False
        try:
            netloc = urlparse(origin).netloc or ""
        except Exception:
            return False
        return netloc.lower() == (self.headers.get("Host") or "").lower()

    def _session_cookie(self, token: str, max_age: int) -> dict:
        """HttpOnly + SameSite=Strict: script cannot read the token and a
        cross-site page cannot ride it. Secure is added when the request
        arrived over HTTPS (the VPS usually terminates TLS in a proxy)."""
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or
                                "").lower() == "https" else ""
        value = ("%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s"
                 % (COOKIE_NAME, token, max_age, secure))
        return {"Set-Cookie": value}

    # -------------------------------------------------------------- routes
    def _host_ok(self) -> bool:
        """DNS-rebinding guard (see webauth.host_header_allowed)."""
        return host_header_allowed(
            self.server.server_address[0], self.headers.get("Host"),
            self.server.auth.enabled())

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        eng = self.server.engine
        q = parse_qs(parsed.query)
        try:
            if not self._host_ok():
                return self._json({"error": "invalid Host header"}, 400)
            authed = self._authed()
            # --- public: the login page needs this to render its state ---
            if path == "/api/auth/status":
                st = dict(self.server.auth.status())
                st["authenticated"] = authed
                return self._json(st)
            if path == "/login":
                if authed:
                    return self._redirect("/")
                return self._static("login.html")
            if not authed:
                # password gate: no session -> never reveal the app or any
                # API payload (the dashboard JS also bounces on 401)
                if path in ("/", "/index.html"):
                    return self._static("login.html")
                return self._json({"error": "authentication required",
                                   "auth_required": True}, 401)
            if path == "/" or path == "/index.html":
                return self._static("index.html")
            if path == "/api/status":
                return self._json(eng.status())
            if path == "/api/dust_preview":
                # SS101 confirmation dialog data: dust rows + values in
                # the TARGET unit (IRT تومان / USDT تتر) from the query
                # string (?target=irt|usdt) — NO side effects
                q = parse_qs(parsed.query)
                target = str((q.get("target") or ["IRT"])[0] or "IRT").upper()
                return self._json(eng.dust_preview(target=target))
            if path == "/api/wallet_convert_preview":
                # «تبدیل موجودی» dialog data: IRT/USDT cash balances + the
                # exchange's own Toman value of one USDT — NO side effects
                return self._json(eng.wallet_convert_preview())
            if path == "/api/events":
                q = parse_qs(parsed.query)
                return self._json(eng.recent_events(int(
                    q.get("limit", ["100"])[0])))
            if path == "/api/history":
                q = parse_qs(parsed.query)
                return self._json(eng.history(
                    q.get("kind", ["decisions"])[0],
                    int(q.get("limit", ["100"])[0])))
            if path == "/api/diagnostics":
                # error monitoring + failed-trade intelligence. One round
                # trip: counts for the cards, filtered rows for the tables.
                q = parse_qs(parsed.query)
                try:
                    limit = max(1, min(500, int(q.get("limit", ["100"])[0])))
                except (TypeError, ValueError):
                    limit = 100

                def pick(name: str) -> str:
                    return str((q.get(name) or [""])[0] or "")

                return self._json({
                    "summary": eng.diagnostics_summary(),
                    "errors": eng.diagnostics_errors(
                        limit, category=pick("category"),
                        severity=pick("severity"), symbol=pick("symbol"),
                        resolution=pick("resolution"), since=pick("since"),
                        search=pick("search")),
                    "failed_trades": eng.diagnostics_failed_trades(
                        limit, category=pick("failure_category"),
                        symbol=pick("symbol"), section=pick("section"),
                        strategy=pick("strategy"), since=pick("since")),
                    "lessons": eng.learning_lessons(
                        100, status=pick("lesson_status")),
                    "filters": {"category": pick("category"),
                                "severity": pick("severity"),
                                "symbol": pick("symbol"),
                                "section": pick("section")},
                })
            if path == "/api/diagnostics/error":
                # full record incl. stack trace: authenticated dashboard only
                eid = str((q.get("id") or [""])[0] or "")
                record = eng.diagnostics_error_detail(eid) if eid else None
                if not record:
                    return self._json({"error": "not found"}, 404)
                return self._json(record)
            if path == "/api/learning":
                q = parse_qs(parsed.query)
                return self._json({
                    "lessons": eng.learning_lessons(
                        int(q.get("limit", ["100"])[0] or 100)),
                    "reports": eng.learning_reports(5),
                })
            if path == "/api/config":
                return self._json(eng.cfg.to_public_dict())
            if path == "/api/login_status":
                return self._json(eng.login_status())
            if path == "/api/exchange_portfolio":
                return self._json(eng.exchange_status())
            if path == "/api/markets_universe":
                # LIVE catalog of every market the exchange lists (one cached
                # public call) + the trading-universe selection. The
                # dashboard's «همه بازارها» table reads `catalog`; the old
                # top-level keys are kept unchanged for existing clients.
                q = parse_qs(parsed.query)
                doc = getattr(eng.collector.registry, "doc", {}) or {}
                try:
                    limit = int(q.get("limit", ["0"])[0]) or None
                except (TypeError, ValueError):
                    limit = None
                flag = (q.get("refresh", [""])[0].lower() in
                        ("1", "true", "yes", "on"))
                live = eng.markets_universe(
                    sort=q.get("sort", ["volume"])[0],
                    quote=(q.get("quote", [""])[0] or None),
                    limit=limit,
                    search=q.get("search", [""])[0],
                    refresh=flag,
                    only_enabled=(q.get("enabled", [""])[0].lower() in
                                  ("1", "true", "yes", "on")))
                return self._json({
                    "universe": live.get("universe") or {},
                    "tradable": sorted(eng.collector.registry
                                       .tradable_symbols()),
                    "registered": len(doc.get("markets", {}) or {}),
                    "catalog": live.get("rows") or [],
                    "catalog_count": live.get("count", 0),
                    "catalog_error": live.get("error", ""),
                    "sort": live.get("sort"),
                    "search": live.get("search"),
                    "transfer": live.get("transfer"),
                    "generated_at": live.get("generated_at"),
                    "selection": doc.get("universe", {}),
                })
            if path == "/api/buy_suggestion":
                return self._json(eng.suggest_buy(
                    q.get("symbol", [""])[0]))
            if path == "/api/sell_preview":
                # «فروش» dialog data: what the wallet really holds of this
                # coin + the best bid a marketable SELL would get — no
                # side effects, so opening the dialog never trades.
                return self._json(eng.sell_preview(
                    q.get("symbol", [""])[0]))
            if path == "/api/real_orders":
                out = {"open": [], "recent": [], "error": ""}
                try:
                    if isinstance(eng.exchange, NobitexAPI):
                        out["open"] = eng.exchange.open_orders()
                        done = eng.exchange._get("/market/orders/list",
                                                 {"status": "done",
                                                  "details": 2,
                                                  "tradeType": "spot",
                                                  "pageSize": 20})
                        out["recent"] = done.get("orders") or []
                except Exception as exc:
                    out["error"] = str(exc)
                return self._json(out)
            if path == "/api/candles":
                q = parse_qs(parsed.query)
                sym = q.get("symbol", ["BTCUSDT"])[0]
                tf = q.get("timeframe", ["30m"])[0]
                return self._json(eng.exchange.candles(
                    sym, tf, int(q.get("limit", ["120"])[0])))
            if path == "/api/orderbook":
                q = parse_qs(parsed.query)
                return self._json(eng.exchange.orderbook(
                    q.get("symbol", ["BTCUSDT"])[0],
                    int(q.get("depth", ["12"])[0])))
            if path.startswith("/api/"):
                return self._json({"error": "unknown endpoint"}, 404)
            return self._static(path.lstrip("/") or "index.html")
        except Exception as exc:
            record_error("DASHBOARD", "ERROR",
                         "GET %s failed: %s" % (path, exc),
                         module="web.do_GET", exc=exc,
                         recovered=True,
                         recovery_action="500 returned to the dashboard")
            return self._json({"error": str(exc)}, 500)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        eng = self.server.engine
        body = self._body()
        auth = self.server.auth
        try:
            if not self._host_ok():
                return self._json({"ok": False,
                                   "error": "invalid Host header"}, 400)
            # --- auth endpoints (reachable before login) ---
            if path == "/api/auth/login":
                if not self._same_origin():
                    return self._json({"ok": False,
                                       "error": "درخواست از منبع نامعتبر"},
                                      403)
                res = auth.attempt(str(body.get("password") or ""),
                                   self._client_ip())
                payload = {"ok": res.ok, "status": res.status,
                           "message": res.message,
                           "attempts_left": res.attempts_left,
                           "locked": res.locked,
                           "lock_seconds_left": res.lock_seconds_left}
                if res.ok:
                    token = auth.create_session(
                        self._client_ip(), self.headers.get("User-Agent", ""))
                    payload["redirect"] = "/"
                    return self._json(payload,
                                      extra=self._session_cookie(
                                          token, int(auth.session_hours() * 3600)))
                code = 503 if res.status == "NO_PASSWORD" else (
                    423 if res.locked else 401)
                return self._json(payload, code)
            if path == "/api/auth/logout":
                auth.revoke(self._cookie(COOKIE_NAME))
                return self._json({"ok": True},
                                  extra=self._session_cookie("", 0))
            if not self._authed():
                return self._json({"error": "authentication required",
                                   "auth_required": True}, 401)
            if not self._same_origin():
                return self._json({"ok": False,
                                   "error": "درخواست از منبع نامعتبر"}, 403)
            if path == "/api/control/start":
                eng.start()
                return self._json({"ok": True, "running": eng.running()})
            if path == "/api/control/stop":
                eng.stop()
                return self._json({"ok": True, "running": eng.running()})
            if path == "/api/control/cycle" or path == "/api/control/run_cycle":
                summary = eng.run_cycle()
                return self._json({"ok": True, "summary": summary})
            if path == "/api/control/auto":
                eng.auto_trading = bool(body.get("enabled", True))
                return self._json({"ok": True,
                                   "auto_trading": eng.auto_trading})
            if path == "/api/control/safe_mode":
                if body.get("enter"):
                    eng.enter_safe_mode(body.get("reasons", ["MANUAL"]))
                else:
                    eng.exit_safe_mode()
                return self._json({"ok": True, "safe_mode": eng.safe_mode})
            if path == "/api/control/risk_mode":
                # حالت ریسک: خرید پرسودترین‌های ۲۴ساعته + فروش سریع هنگام
                # افت + چرخش سریع سرمایه
                return self._json(eng.set_risk_mode(
                    bool(body.get("enabled", False))))
            if path == "/api/control/reset_paper":
                bal = float(body.get("starting_balance", 10000) or 10000)
                eng.portfolio.reset(bal)
                return self._json({"ok": True, "starting_balance": bal})
            if path == "/api/control/config":
                return self._json(save_config(eng, body))
            if path == "/api/learning/analyze":
                # deterministic pattern analysis; AI explanation is opt-in and
                # can only WRITE a report — it never changes code or settings
                return self._json(eng.run_learning(
                    use_ai=bool(body.get("use_ai", False))))
            if path == "/api/diagnostics/sync_rtc":
                return self._json(eng.sync_diagnostics_rtc(force=True))
            if path == "/api/diagnostics/resolve":
                diag = getattr(eng, "diagnostics", None)
                eid = str(body.get("error_id") or "")
                status = str(body.get("resolution") or "resolved")
                ok = bool(diag and eid and diag.resolve_error(eid, status))
                return self._json({"ok": ok, "error_id": eid,
                                   "resolution": status})
            if path == "/api/control/login":
                return self._json(eng.account_login(
                    body.get("username", ""), body.get("password", ""),
                    body.get("otp", ""), body.get("totp_secret", ""),
                    body.get("remember", "yes")))
            if path == "/api/control/logout":
                return self._json(eng.account_logout())
            if path == "/api/control/sync_wallet":
                return self._json(eng.sync_real_wallet())
            if path == "/api/control/live_trading":
                return self._json(eng.set_live_trading(
                    bool(body.get("enabled", False))))
            if path == "/api/control/buy_best":
                return self._json(eng.set_buy_best(
                    bool(body.get("enabled", True))))
            if path == "/api/control/execute_best":
                return self._json(eng.execute_best_trade())
            if path == "/api/control/execute_decision":
                did = str(body.get("decision_id", ""))
                if not did:
                    return self._json({"ok": False,
                                       "error": "decision_id لازم است"})
                return self._json(eng.execute_decision(did))
            if path == "/api/control/sell_now":
                # never let a bug look like «موتور ریست»: a manual click
                # answers with a readable Persian error, never a bare 500.
                # `amount` is the COIN quantity from the sell dialog (None
                # or "all" = the whole holding).
                raw_amt = body.get("amount")
                amt = None
                try:
                    if raw_amt not in (None, "", "all", "ALL"):
                        amt = float(str(raw_amt).replace("٫", ".")
                                    .replace(",", "."))
                    return self._json(eng.sell_position_now(
                        str(body.get("symbol", "")), amt))
                except (TypeError, ValueError):
                    return self._json({"ok": False,
                                       "error": "مقدار فروش نامعتبر است"})
                except Exception as exc:
                    return self._json({
                        "ok": False,
                        "error": "فروش انجام نشد: " + str(exc)[:200]})
            if path == "/api/control/buy_now":
                # manual BUY from the holdings box: the amount may arrive
                # in the market's OWN currency (`amount_quote` — USDT for
                # *USDT markets, Toman for *IRT) or as a legacy Toman
                # `amount_irt`; None = engine suggestion from free cash
                amt = body.get("amount_irt")
                aq = body.get("amount_quote")
                try:
                    amt_f = float(amt) if amt is not None else None
                    aq_f = float(aq) if aq is not None else None
                except (TypeError, ValueError):
                    return self._json({"ok": False,
                                       "error": "مبلغ خرید نامعتبر است"})
                try:
                    return self._json(eng.manual_buy(
                        str(body.get("symbol", "")), amt_f, aq_f))
                except Exception as exc:
                    return self._json({
                        "ok": False,
                        "error": "خرید انجام نشد: " + str(exc)[:200]})
            if path == "/api/control/set_stop":
                # value may arrive as an absolute Toman number (stop) or as a
                # percent relative to my invested value (pct): negative = below
                # entry (stop-loss), positive = above entry (take-profit).
                if body.get("pct") is not None:
                    try:
                        pct_v = float(body.get("pct"))
                    except (TypeError, ValueError):
                        return self._json({"ok": False,
                                           "error": "درصد نامعتبر است"})
                    stop_v = 0.0
                else:
                    pct_v = None
                    try:
                        stop_v = float(body.get("stop", 0) or 0)
                    except (TypeError, ValueError):
                        return self._json({"ok": False,
                                           "error": "عدد حد نامعتبر است"})
                return self._json(eng.set_stop_loss(
                    str(body.get("symbol", "")), stop_v,
                    side="sell", pct=pct_v,
                    kind=str(body.get("kind", "stop")),
                    base_mode=str(body.get("base", "entry"))))
            if path == "/api/control/set_all_stops":
                # uniform stop percent for EVERY held coin at once; each
                # trigger = that coin's CURRENT price x (1 + pct/100).
                try:
                    pct_v = float(body.get("pct"))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "درصد نامعتبر است"})
                return self._json(eng.set_all_stops(
                    pct_v, kind=str(body.get("kind", "stop")),
                    base=str(body.get("base", "entry"))))
            if path == "/api/control/set_hadd":
                # manual strike counter / hadd price edit from the (n)
                # bracket in the holdings row
                try:
                    st = int(body.get("stage", 0))
                except Exception:
                    st = 0
                hv = body.get("hadd")
                return self._json(eng.set_hadd(
                    str(body.get("symbol", "")), st,
                    float(hv) if hv not in (None, "") else None))
            if path == "/api/control/set_deposited":
                # کل سرمایه واریزی: the total Toman the user has put in
                # — the سود کل KPI splits live profit against it
                return self._json(eng.set_deposited_capital(
                    float(body.get("amount_irt", 0) or 0)))
            if path == "/api/control/refresh_deposited":
                # Force a live re-fetch of the REAL turnover data
                # (Σdeposits − Σwithdraws from Nobitex) and report it.
                return self._json(eng.refresh_net_deposited())
            if path == "/api/control/cancel_bot_orders":
                return self._json(eng.cancel_bot_orders())
            if path == "/api/control/convert_dust":
                # SS101: convert tiny coin balances into the ACTIVE
                # market's cash. force=True (manual button) converts every
                # sub-threshold coin immediately; in «هر دو» (Both) mode the
                # body may pin the destination (target=IRT|USDT) exactly the
                # way the dialog picked it — None lets the engine's own
                # per-section rules decide.
                force = bool(body.get("force", False))
                target = (str(body.get("target") or "").strip().upper()
                          or None)
                if target not in (None, "IRT", "USDT"):
                    return self._json({"ok": False,
                                       "error": "مقصد تبدیل نامعتبر است "
                                                "(تومان یا تتر)"})
                return self._json(
                    {"ok": True, "result": eng._convert_dust_holdings(
                        force=force, target=target)})
            if path == "/api/control/convert_selected":
                # user-confirmed batch from the dialog: convert ONLY the
                # picked coins into the TARGET wallet (IRT تومان / USDT
                # تتر — each with its real value shown beforehand)
                coins = body.get("coins") or []
                if not isinstance(coins, list) or not coins:
                    return self._json({"ok": False,
                                       "error": "هیچ سکه‌ای انتخاب نشده است"})
                target = str(body.get("target") or body.get("dst")
                             or "IRT").strip().upper()
                if target not in ("IRT", "USDT"):
                    return self._json({"ok": False,
                                       "error": "مقصد تبدیل نامعتبر است "
                                                "(تومان یا تتر)"})
                return self._json({"ok": True,
                                   "result": eng.convert_selected(
                                       coins, target=target)})
            if path == "/api/control/wallet_convert":
                # «تبدیل موجودی» confirm step: target USDT (Toman→Tether) or
                # IRT (Tether→Toman); amount=None means the WHOLE balance
                target = str(body.get("target") or "").upper()
                amt = body.get("amount")
                try:
                    amt_f = float(amt) if amt not in (None, "") else None
                except (TypeError, ValueError):
                    return self._json({"ok": False,
                                       "error": "مبلغ تبدیل نامعتبر است"})
                res = eng.wallet_convert(target, amt_f)
                return self._json({"ok": bool(res.get("ok")),
                                   **res})
            if path == "/api/dust_preview":
                # list of dust holdings in the TARGET unit — NO side effects
                # (do_POST has no `parsed` local: the URL was reduced to
                # `path` above, so parse the raw request line here)
                q = parse_qs(urlparse(self.path).query)
                target = str((q.get("target") or ["IRT"])[0] or "IRT").upper()
                return self._json(eng.dust_preview(target=target))
            if path.startswith("/api/"):
                return self._json({"error": "unknown endpoint"}, 404)
            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            record_error("DASHBOARD", "ERROR",
                         "POST %s failed: %s" % (path, exc),
                         module="web.do_POST", exc=exc, recovered=True,
                         recovery_action="500 returned to the dashboard")
            return self._json({"error": str(exc)}, 500)


def _normalize_market_interaction(v) -> str:
    """«تعامل بین بازارها» (linked) یا «بازارهای مستقل» (independent)?

    The engine only accepts these two tokens; every other value (empty,
    junk, a stale UI label) becomes "independent" — the fail-safe answer
    that can never move money between the IRT and the USDT section.
    """
    raw = str(v if v is not None else "").strip().lower()
    if raw in ("linked", "link", "interact", "interaction", "connected",
               "transfer", "transferable", "both_linked", "on", "true",
               "yes", "1"):
        return "linked"
    return "independent"


def _coerce_config_value(v, old):
    """Keep a saved setting in the CURRENT field's type.

    The int branch deliberately keeps a FRACTIONAL value as a float:
    typing ‎-2.5 in «حد ضرر پیش‌فرض خرید» / «حد سود» / گام پله (fields
    stored as ints in config.json) was truncated to -2, so decimal
    percents could never be set from the settings panel (user request:
    «بشه درصد حد ضرر و حد سود رو به اعشار تعیین کرد»).
    """
    if isinstance(old, bool):
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on")
        return bool(v)
    if isinstance(old, int) and not isinstance(old, bool):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return old
        if fv != int(fv):
            return fv
        return int(fv)
    if isinstance(old, float):
        try:
            return float(v)
        except (TypeError, ValueError):
            return old
    return v


def save_config(engine: TradingEngine, body: dict) -> dict:
    """Persist config.json; existing *** masks keep their stored values.

    EVERY saved field is also applied LIVE on the running engine — no
    restart needed. All engine components share one Config object, so
    setattr on engine.cfg reaches collector/executor/scanner instantly;
    exchange changes additionally re-make the adapter via _set_exchange.
    """
    from .config import (DEFAULT_CONFIG_PATH, ConfigError, config_from_dict,
                         load_config)
    # strict=False: a config the bots refuses to START from must still be
    # repairable from the settings page (this is the recovery path)
    current = load_config(strict=False)
    from dataclasses import asdict
    cur = asdict(current)
    clean = {}
    for sec, fields in (body or {}).items():
        if not isinstance(fields, dict) or sec not in cur:
            continue
        clean[sec] = {}
        for k, v in fields.items():
            if k not in cur[sec]:
                continue
            if isinstance(v, str) and v.strip() == "***":
                clean[sec][k] = cur[sec][k]  # keep masked secret as-is
            elif sec == "exchange" and k == "trade_market_interaction":
                # only the two documented tokens ever reach config.json
                clean[sec][k] = _normalize_market_interaction(v)
            else:
                clean[sec][k] = v
    # MERGE into the existing file instead of replacing it: a partial body
    # (e.g. the dashboard-security card posting only the web section) must
    # never wipe the untouched sections — that could drop the dashboard
    # password or the exchange credentials.
    from contextlib import nullcontext
    # the live-trading toggle writes the same file from the engine thread
    write_lock = getattr(engine, "_config_write_lock", None) or nullcontext()
    try:
        with write_lock:
            doc: dict = {}
            if DEFAULT_CONFIG_PATH.exists():
                try:
                    loaded = json.loads(DEFAULT_CONFIG_PATH.read_text(
                        encoding="utf-8"))
                    if isinstance(loaded, dict):
                        doc = loaded
                except Exception:
                    doc = {}
            for sec_name, fields in clean.items():
                base = doc.get(sec_name)
                base = dict(base) if isinstance(base, dict) else {}
                base.update(fields)
                doc[sec_name] = base
            # VALIDATE BEFORE WRITING: a positive stop-loss, an
            # out-of-range percent or an unknown quote mode must never reach
            # the file the next start trusts. The old flow wrote first and
            # applied later.
            config_from_dict(doc, strict=True)
            DEFAULT_CONFIG_PATH.write_text(
                json.dumps(doc, ensure_ascii=False, indent=2),
                encoding="utf-8")
    except ConfigError as exc:
        return {"ok": False,
                "error": "تنظیمات نامعتبر است و ذخیره نشد: " +
                         "؛ ".join(exc.problems),
                "failed": [{"field": "config", "error": p}
                           for p in exc.problems]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    # ---- apply live: coerce each value to the current field's type ----
    applied, failed = [], []
    # credentials live in the login SESSION, not in config.json — the form
    # posts them as empty strings. NEVER let an empty value wipe a
    # non-empty live one (that killed the real adapter once: empty creds
    # -> make_exchange fell back to the DEMO simulator -> fake prices).
    secret_fields = {"nobitex_username", "nobitex_password",
                     "nobitex_totp_secret", "nobitex_api_key"}
    for sec_name, fields in clean.items():
        target = getattr(engine.cfg, sec_name, None)
        if target is None:
            continue
        for k, v in fields.items():
            if not hasattr(target, k):
                continue
            if sec_name == "exchange" and k in secret_fields:
                if isinstance(v, str) and not v.strip():
                    continue          # empty -> keep the live credential
                try:
                    eng_val = getattr(target, k, "")
                    if isinstance(eng_val, str) and eng_val.strip() and \
                            str(v).strip() != eng_val.strip():
                        # genuinely NEW credential from the form — mirror it
                        # into the live session too
                        try:
                            ses = getattr(engine, "nobitex_session", None)
                            attr = {"nobitex_username": "username",
                                    "nobitex_password": "password",
                                    "nobitex_totp_secret": "totp_secret",
                                    "nobitex_api_key": None}.get(k)
                            if ses is not None and attr:
                                setattr(ses, attr, str(v).strip())
                        except Exception:
                            pass
                except Exception:
                    pass
            try:
                old = getattr(target, k)
                setattr(target, k, _coerce_config_value(v, old))
                applied.append("%s.%s" % (sec_name, k))
            except Exception as exc:
                failed.append({"field": "%s.%s" % (sec_name, k),
                               "error": str(exc)})

    # exchange section: adapter reconnection needs its own path.
    # GUARD: a config save must NEVER detach the real account — if the
    # factory would fall back to the DEMO simulator while we are attached
    # to Nobitex, keep the current adapter and just report it.
    reattached = False
    try:
        ex_fields = set((clean.get("exchange") or {}).keys())
        if ex_fields:
            from .exchange_factory import make_exchange
            new_ex = make_exchange(engine.cfg)
            if isinstance(new_ex, NobitexAPI):
                # share the live session (token) so no re-login is needed
                new_ex.session = getattr(engine, "nobitex_session",
                                         new_ex.session)
                new_ex.set_irt_divisor(float(getattr(
                    engine.cfg.exchange, "rls_to_irt_divisor", 10) or 10))
                if type(new_ex) is not type(engine.exchange):
                    engine._set_exchange(new_ex)
                    reattached = True
                else:
                    engine.exchange = new_ex
                    engine.collector.exchange = new_ex
                    engine.executor.exchange = new_ex
            else:
                # factory returned the demo simulator while (possibly)
                # attached to the real account -> keep current adapter
                if isinstance(engine.exchange, NobitexAPI):
                    failed.append({
                        "field": "exchange.reconnect",
                        "error": ("اعمال تغییر صرافی متوقف شد: اعتبار "
                                  "ورود موجود نیست — برای جلوگیری از سقوط "
                                  "به حالت آزمایشی، اتصال واقعی حفظ شد")})
                else:
                    engine.exchange = new_ex
                    engine.collector.exchange = new_ex
                    engine.executor.exchange = new_ex
    except Exception as exc:
        failed.append({"field": "exchange.reconnect", "error": str(exc)})

    # The Both-mode interaction switch is what grants (or denies) transfers
    # between the two sections, so log the newly effective gate: the change
    # must be visible in the event history, not only in the settings tab.
    gate = None
    try:
        fn = getattr(engine, "_market_transfer_gate", None)
        if callable(fn):
            gate = fn()
        events = getattr(engine, "events", None)
        ex_clean = clean.get("exchange") or {}
        if gate is not None and events is not None and (
                "trade_market_interaction" in ex_clean or
                "trade_quote_markets" in ex_clean):
            events.publish("MARKET_TRANSFER_GATE_CHANGED", "web", "", gate)
    except Exception as exc:
        failed.append({"field": "exchange.market_transfer_gate",
                       "error": str(exc)})

    return {"ok": True, "path": str(DEFAULT_CONFIG_PATH),
            "applied_live": applied,
            "failed": failed,
            "exchange_reattached": reattached,
            "market_transfer": gate,
            "note": ("all saved settings applied live — no restart needed" if
                     not failed else
                     "applied with exceptions; see failed")}


def serve(engine: TradingEngine, host: str, port: int,
          auth: DashboardAuth | None = None) -> None:
    """Start the dashboard with the password gate attached.

    Safety rule (SS101 fail-safe): a NON-loopback bind without a configured
    password is refused, because anyone reaching that port could start the
    engine and place real orders. Set ``web.password`` in config.json (or
    flip ``web.allow_remote_without_password`` when another layer — e.g. an
    authenticating reverse proxy — already guards it).
    """
    auth = auth or DashboardAuth(engine.cfg, events=getattr(engine, "events", None))
    remote = not is_loopback_host(host)
    allow_remote_open = bool(getattr(engine.cfg.web,
                                     "allow_remote_without_password", False))
    if remote and not auth.enabled() and not allow_remote_open:
        raise SystemExit(
            "[web] refusing to expose the dashboard on %s:%d WITHOUT a password.\n"
            "      The dashboard can place real orders, so an open port is a "
            "real risk.\n      Set a password in config.json -> web.password "
            "(a PBKDF2 hash from\n      `python -m nobitex_bot.webauth --hash YOUR_PASSWORD` "
            "also works), or bind\n      127.0.0.1 and put an authenticating proxy "
            "in front of it. To accept the\n      risk anyway set "
            "web.allow_remote_without_password = true." % (host, port))
    server = BotHTTPServer((host, port), engine, auth)
    print("[web] dashboard on http://%s:%d" % (host, port))
    if auth.enabled():
        st = auth.status()
        print("[web] password gate: ON | max %s wrong tries | %s"
              % (st["max_attempts"],
                 ("LOCKED right now" if st["locked"]
                  else "session %s h" % st["session_hours"])))
        # operator-only hint: the login PAGE deliberately shows no recovery
        # path (it is visible to anyone who can reach the port)
        print("[web] locked out or forgot the password? change web.password "
              "in config.json — it applies instantly, no restart "
              "(helper: python -m nobitex_bot.webauth --hash)")
        print("[web] locked out after wrong passwords? set web.lock_panel to 0 "
              "in config.json to unlock the panel (1 = lockout armed again)")
    else:
        print("[web] password gate: OFF (loopback only) — set web.password "
              "in config.json before exposing this port")
    server.serve_forever()
