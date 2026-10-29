"""Nobitex real-market adapter with full login flow (SS66/67, SS81).

Implements the official API (docs: apidocs.nobitex.ir):

- Login:      POST /auth/login/   {username, password, captcha:"api", remember}
              headers: X-TOTP (2FA code), User-Agent: TraderBot/<name>
              -> {status:"success", key:<token>, device}
              Token is 4h (30d with remember=yes). Login must come from an
              Iranian IP (else 429).
- Auth:       Authorization: Token <key>
- Wallets:    GET /users/wallets/list        (spot balances, SS21)
- Orders:     POST /market/orders/add        (limit/market)
              GET  /market/orders/list       (open/done)
              POST /market/orders/status     (single order status)
              POST /market/orders/update-status (cancel)
- Market:     GET /market/stats              (ticker per market)
              GET /v3/orderbook/<SYMBOL>
              GET /market/udf/history        (OHLC candles)
              GET /v2/trades/<SYMBOL>

Security (SS81): the token and TOTP secret live only in
data/credentials.json (chmod 600 on POSIX). They are never logged, never
appear in snapshots/AI context/events, and are masked by the config API.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import tls
from .exchange import ExchangeAdapter
from .utils import canonical_base, d, split_symbol, utc_now_iso

API_BASE = "https://apiv2.nobitex.ir"


def _to_float_or(v, default: float = 0.0) -> float:
    """Best-effort float() over the exchange's decimal-string numbers."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
USER_AGENT = "TraderBot/NobitexSmartBot-1.0"

TF_TO_RESOLUTION = {"1m": "1", "5m": "5", "15m": "15", "30m": "30",
                    "1h": "60", "4h": "240", "1d": "D"}
RESOLUTION_SECONDS = {"1": 60, "5": 300, "15": 900, "30": 1800, "60": 3600,
                      "180": 10800, "240": 14400, "360": 21600,
                      "720": 43200, "D": 86400}

# Nobitex order status -> contract SS24 status
STATUS_MAP = {
    "active": "OPEN", "new": "PENDING", "inactive": "PENDING",
    "done": "FILLED", "canceled": "CANCELLED", "rejected": "REJECTED",
    "expired": "EXPIRED",
}


class NobitexError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def totp_now(secret_b32: str, period: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP from a base32 secret (Google Authenticator compatible).

    Standard-library implementation (hmac/sha1/base64) per the Nobitex docs
    note that the 2FA code can be generated programmatically.
    """
    secret = secret_b32.strip().replace(" ", "").upper()
    pad = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret + pad)
    counter = int(time.time()) // period
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) \
        % (10 ** digits)
    return str(code).zfill(digits)


def epoch_to_iso(epoch_seconds) -> str:
    """Epoch -> ISO. Accepts seconds or milliseconds (auto-detected)."""
    ts = int(epoch_seconds or 0)
    if ts > 1_000_000_000_000:   # ms timestamp (e.g. /v2/trades time)
        ts //= 1000
    return datetime.fromtimestamp(ts, tz=timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _api_error_text(payload: dict) -> str:
    """Extract the most detailed human-readable error from a failed
    response (message / jerror / nested errors dict)."""
    parts = []
    for key in ("message", "jerror", "detail"):
        v = payload.get(key)
        if v and str(v) not in parts:
            parts.append(str(v))
    errs = payload.get("errors") or payload.get("error")
    if isinstance(errs, dict):
        for k, v in errs.items():
            txt = "%s: %s" % (k, v) if not isinstance(v, str) else v
            if txt not in parts:
                parts.append(txt)
    elif isinstance(errs, list):
        for v in errs:
            if str(v) not in parts:
                parts.append(str(v))
    return " | ".join(parts) or "failed"


def map_status(raw: str) -> str:
    return STATUS_MAP.get((raw or "").strip().lower(), "UNKNOWN")


class NobitexSession:
    """Logged-in session with a persistent token and auto re-login."""

    def __init__(self, data_dir: Path, username: str = "",
                 password: str = "", totp_secret: str = "",
                 user_agent: str = USER_AGENT, timeout: float = 12.0):
        self.cred_path = Path(data_dir) / "credentials.json"
        self.cred_path.parent.mkdir(parents=True, exist_ok=True)
        self.username = username
        self.password = password
        self.totp_secret = totp_secret
        self.user_agent = user_agent
        self.timeout = timeout
        self.token = ""
        self.device = ""
        self.logged_in_at = ""
        self.remember = "yes"
        self._lock = threading.Lock()
        self._last_post_at = 0.0   # global min-gap between POSTs (rate limit)
        self._load_creds()

    # ------------------------------------------------------------ storage
    def _load_creds(self) -> None:
        if not self.cred_path.exists():
            return
        try:
            doc = json.loads(self.cred_path.read_text(encoding="utf-8"))
            self.username = doc.get("username", self.username)
            self.password = doc.get("password", self.password)
            self.totp_secret = doc.get("totp_secret", self.totp_secret)
            self.token = doc.get("token", "")
            self.device = doc.get("device", "")
            self.logged_in_at = doc.get("logged_in_at", "")
            self.remember = doc.get("remember", "yes")
        except Exception:
            pass

    def _save_creds(self) -> None:
        doc = {
            "username": self.username,
            "password": self.password,
            "totp_secret": self.totp_secret,
            "token": self.token,
            "device": self.device,
            "logged_in_at": self.logged_in_at,
            "remember": self.remember,
        }
        self.cred_path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        try:  # best-effort POSIX perms; on Windows the folder ACLs apply
            os.chmod(self.cred_path, 0o600)
        except OSError:
            pass

    def wipe(self) -> None:
        """Logout + forget everything (SS81)."""
        with self._lock:
            if self.token:
                try:
                    self._raw_request("POST", "/auth/logout/", {},
                                      token=self.token)
                except Exception:
                    pass
            self.token = ""
            self.device = ""
            self.logged_in_at = ""
            self.username = ""
            self.password = ""
            self.totp_secret = ""
            if self.cred_path.exists():
                try:
                    self.cred_path.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------ http
    def _raw_request(self, method: str, path: str, body: dict | None,
                     token: str = "", extra_headers: dict | None = None) -> dict:
        url = API_BASE + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json",
                   "User-Agent": self.user_agent}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Token " + token
        if extra_headers:
            headers.update(extra_headers)
        # gzip accepted here too: the authed wallet/portfolio/turnover
        # replies are large JSON the exchange happily compresses
        headers["Accept-Encoding"] = "gzip"
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=tls.context()) as resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                payload = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {}
            code = str(payload.get("code") or payload.get("message") or
                       "HTTP_%d" % exc.code)
            if exc.code == 401:
                raise NobitexError("AUTH_EXPIRED", "token invalid/expired", False)
            if exc.code == 429:
                raise NobitexError("RATE_LIMIT",
                                   "rate limited or non-Iran IP", True)
            raise NobitexError(code, str(payload.get("message") or code),
                               exc.code in (500, 502, 503, 504))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # A missing/empty trust store is a HOST problem, not a network
            # hiccup: say exactly how to repair it instead of letting the
            # raw OpenSSL line reach the dashboard.
            hint = tls.error_hint(exc)
            if hint:
                raise NobitexError("TLS_CA_MISSING", hint, True)
            raise NobitexError("NETWORK", str(exc), True)
        if isinstance(payload, dict) and payload.get("status") == "failed":
            raise NobitexError(
                str(payload.get("code") or "API_ERROR"),
                _api_error_text(payload), False)
        return payload

    # ------------------------------------------------------------ login
    def login(self, username: str | None = None, password: str | None = None,
              totp_secret: str | None = None, totp_code: str = "",
              remember: str = "yes") -> dict:
        """Full login flow (docs: /auth/login/)."""
        with self._lock:
            if username is not None:
                self.username = username
            if password is not None:
                self.password = password
            if totp_secret is not None:
                self.totp_secret = totp_secret
            self.remember = remember
            if not (self.username and self.password):
                raise NobitexError("AUTH_MISSING",
                                   "username/password required", False)
            if totp_code:
                otp = totp_code.strip()
            elif self.totp_secret:
                otp = totp_now(self.totp_secret)
            else:
                raise NobitexError(
                    "MissingOTP",
                    "2FA is required: provide TOTP secret or a live code",
                    False)
            payload = {
                "username": self.username,
                "password": self.password,
                "captcha": "api",       # docs: API mode, no image captcha
                "remember": remember,
            }
            try:
                resp = self._raw_request(
                    "POST", "/auth/login/", payload,
                    extra_headers={"X-TOTP": otp})
            except NobitexError as exc:
                if exc.code in ("MissingOTP",):
                    raise NobitexError(
                        "MissingOTP",
                        "2FA code rejected/required (X-TOTP)", False)
                raise
            if resp.get("status") != "success" or not resp.get("key"):
                raise NobitexError(
                    "LOGIN_FAILED", str(resp.get("message") or resp), False)
            self.token = resp["key"]
            self.device = str(resp.get("device") or "")
            self.logged_in_at = utc_now_iso()
            self._save_creds()
            return {"ok": True, "device": self.device,
                    "logged_in_at": self.logged_in_at,
                    "remember": remember}

    def ensure_token(self) -> str:
        """Return a valid token, logging in again if needed (SS67 resync)."""
        if self.token:
            return self.token
        return self.login(remember=self.remember)["ok"] and self.token

    # ------------------------------------------------ convert (swap) market
    def exchange_quote(self, src_coin: str, amount: str,
                       direction: str = "sell") -> dict:
        """Ask the Nobitex convert market (panel/balance/convert) for a quote.

        Reverse-engineered contract of POST /exchange/get-quote:
          type          : "sell" (crypto -> rls) or "buy" (rls -> crypto)
          baseCurrency  : lower-case crypto coin (e.g. "usdt"); with
                          type=buy this is the coin to RECEIVE and
                          `amount` is the rials to SPEND
          quoteCurrency : "rls" (rial market)
          refCurrency   : "rls"
          amount        : decimal string in RIALS (min notional enforced by
                          the exchange, e.g. USDT >= 12,500,000 rls)
        Response: {status:"ok", result:{quoteId, refAmount, destAmount,
                    isSell, expiresAt}} — the quote is EXECUTABLE only via
                    create_trade() within expiresAt.
        """
        body = {
            "type": "buy" if direction == "buy" else "sell",
            "baseCurrency": (src_coin or "").strip().lower(),
            "quoteCurrency": "rls",
            "refCurrency": "rls",
            "amount": str(amount),
        }
        data = self.authed_request("POST", "/exchange/get-quote", body)
        res = data.get("result") or data
        return {
            "quote_id": str(res.get("quoteId") or ""),
            "ref_amount": str(res.get("refAmount") or ""),
            "dest_amount": str(res.get("destAmount") or ""),
            "is_sell": bool(res.get("isSell")),
            "expires_at": str(res.get("expiresAt") or ""),
            "raw": res,
        }

    def exchange_create_trade(self, quote_id: str) -> dict:
        """Execute a previously-obtained quote (POST /exchange/create-trade).

        This actually converts the coin balance to rials, mirroring the
        final confirm step of the panel convert page.
        """
        data = self.authed_request("POST", "/exchange/create-trade",
                                   {"quoteId": quote_id})
        res = data.get("result") or data
        return {
            "status": str(res.get("status") or data.get("status") or ""),
            "raw": res,
        }

    def authed_request(self, method: str, path: str,
                       body: dict | None = None) -> dict:
        """Authed call; one transparent re-login on token expiry.

        Mutating calls (POST) are spaced by a global minimum gap so batch
        operations (e.g. converting 10 dust coins) never trip Nobitex's
        'rate limited or non-Iran IP' 429 wall."""
        if method.upper() == "POST":
            with self._lock:
                gap = 2.5
                wait = gap - (time.time() - self._last_post_at)
                if wait > 0:
                    time.sleep(wait)
                self._last_post_at = time.time()
        token = self.ensure_token()
        try:
            return self._raw_request(method, path, body, token=token)
        except NobitexError as exc:
            if exc.code == "AUTH_EXPIRED":
                self.token = ""
                token = self.ensure_token()
                return self._raw_request(method, path, body, token=token)
            raise

    # ------------------------------------------------------------ info
    def status(self) -> dict:
        return {
            "connected": bool(self.token),
            "username": self.username,
            "device": self.device,
            "logged_in_at": self.logged_in_at,
            "has_totp_secret": bool(self.totp_secret),
            "remember": self.remember,
        }


class NobitexAPI(ExchangeAdapter):
    """ExchangeAdapter backed by the real Nobitex API + session login."""

    name = "nobitex"

    def __init__(self, cfg, events=None):
        self.cfg = cfg
        self.events = events
        base = getattr(cfg, "nobitex_base_url", "") or API_BASE
        self.base = base.rstrip("/")
        self.session = NobitexSession(
            Path(cfg.data_dir) if hasattr(cfg, "data_dir") else Path("data"),
            username=getattr(cfg, "nobitex_username", ""),
            password=getattr(cfg, "nobitex_password", ""),
            totp_secret=getattr(cfg, "nobitex_totp_secret", ""),
            timeout=getattr(cfg, "rest_timeout", 12.0))
        self._stats_cache = {}
        self._stats_ts = 0.0
        self._stats_lock = threading.Lock()
        # IRT unit policy (single source of truth for the whole bot):
        # Nobitex quotes IRT markets in RIALS and holds wallets in Rials,
        # but the bot's IRT ledger is in TOMAN. The adapter therefore
        # converts every IRT-market price to Toman at the boundary so all
        # downstream math (valuation, sizing, min-notional, orders) uses
        # ONE consistent IRT unit. Divisor comes from config
        # (rls_to_irt_divisor, default 10) and can also be set after
        # construction via set_irt_divisor (used by the engine hot-attach).
        self._irt_divisor = float(getattr(cfg, "rls_to_irt_divisor", 10)
                                  or 10)

    def set_irt_divisor(self, div: float) -> None:
        """Set the Rial->Toman divisor (applies to IRT-market prices)."""
        if div and float(div) > 0:
            self._irt_divisor = float(div)

    @staticmethod
    def _client_order_id(coid: str) -> str:
        """Nobitex rejects clientOrderIds containing '_' (verified live:
        Invalid clientOrderId). Keep [A-Za-z0-9-] only, max 32 chars."""
        safe = "".join(ch if (ch.isalnum() or ch == "-") else "-"
                       for ch in str(coid))
        return safe[:32]

    @staticmethod
    def _plain_num(v) -> str:
        """Decimal -> plain-notation string (NEVER scientific).

        Decimal math on large IRT prices produces '2.1371E+11' via str();
        sending that as the order price is fragile at the API boundary.
        format(x, 'f') always renders plain digits."""
        return format(v, "f")

    def _sanitize_price(self, market: str, price) -> Decimal:
        """Round price to the market tick size (max 3 decimals for USDT,
        integer Rials for IRT markets) so the API never rejects precision."""
        p = d(str(price))
        if p <= 0:
            return p
        if market.endswith("USDT"):
            step = Decimal("0.0001") if p >= 100 else (
                Decimal("0.000001") if p >= 1 else Decimal("0.00000001"))
            return (p / step).to_integral_value(rounding="ROUND_DOWN") * step
        # IRT market: price sent in Rials -> integer
        rls = p * Decimal(str(self._irt_divisor))
        return rls.to_integral_value(rounding="ROUND_DOWN") / \
            Decimal(str(self._irt_divisor))

    def _sanitize_amount(self, market: str, amount: Decimal) -> Decimal:
        """Round amount down to 6 decimals (Nobitex max precision)."""
        a = d(str(amount))
        step = Decimal("0.000001")
        return (a / step).to_integral_value(rounding="ROUND_DOWN") * step

    def _to_irt_price(self, value) -> float:
        """Convert a Rial price to the bot's IRT (Toman) unit."""
        f = float(value or 0)
        return f / self._irt_divisor if self._irt_divisor else f

    def _rls_price_str(self, value) -> str:
        """Rial amount -> the bot's IRT (Toman) unit, plain notation.

        Rial quotes are divided at the boundary (see _to_irt_price) but a
        string is returned so callers never re-serialize as scientific
        notation; anything unparsable passes through untouched."""
        try:
            p = d(str(value))
        except Exception:
            return value
        if self._irt_divisor:
            p = p / d(str(self._irt_divisor))
        return format(p, "f")

    def _usdtirt_ticker(self, st: dict) -> dict:
        """USDTIRT (Tether-Toman) ticker in Toman units.

        Stats endpoint reports Rials; USDTIRT is the Rial->Toman bridge so
        it MUST be divided like every other IRT-market price (verified:
        raw latest 2,308,320 Rls = 230,832 Toman, matching real USDT/IRT).
        """
        last = self._to_irt_price(st.get("latest"))
        best_ask = self._to_irt_price(st.get("bestSell"))
        best_bid = self._to_irt_price(st.get("bestBuy"))
        spread = (best_ask - best_bid) if (best_ask and best_bid) else 0.0
        return {
            "symbol": "USDTIRT",
            "last_price": last,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_percent": (spread / last * 100) if last else 0.0,
            "volume_24h": float(st.get("volumeSrc") or 0),
            "quote_volume_24h": float(st.get("volumeDst") or 0),
            "change_24h": float(st.get("dayChange") or 0),
            "high_24h": float(st.get("dayHigh") or 0),
            "low_24h": float(st.get("dayLow") or 0),
            "timestamp": utc_now_iso(),
            "received_at": utc_now_iso(),
        }

    # ------------------------------------------------------------ plumbing
    def _get(self, path: str, params: dict | None = None) -> dict:
        if params:
            path += "?" + urllib.parse.urlencode(params)
        return self.session.authed_request("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        return self.session.authed_request("POST", path, body)

    def _public_get(self, path: str, params: dict | None = None) -> dict:
        """Public market-data call (no token needed).

        gzip accepted on every call: /market/stats (the whole-catalog tick
        feeding the universe) is a LARGE JSON and Nobitex compresses it —
        without `Accept-Encoding: gzip` the same download is several times
        bigger on a metered connection.
        """
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": self.session.user_agent})
        try:
            with urllib.request.urlopen(
                    req, timeout=self.session.timeout,
                    context=tls.context()) as resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError) as exc:
            hint = tls.error_hint(exc)
            if hint:
                raise NobitexError("TLS_CA_MISSING", hint, True)
            raise

    # ------------------------------------------------------------ discovery
    def markets(self) -> list:
        """All tradable markets in the bot's canonical symbol space.

        Nobitex quotes in Rial (BTC-RLS) or Tether (BTC-USDT); the bot uses
        ...USDT / ...IRT names everywhere (BTCIRT), so RLS pairs are mapped
        to IRT here once at discovery.
        """
        stats = self._stats_all()
        out = []
        now = utc_now_iso()
        for pair, st in stats.items():
            src, _, dst = pair.partition("-")
            dst_u = dst.upper()
            if dst_u == "USDT":
                sym = src.upper() + "USDT"
                quote = "USDT"
            elif dst_u == "RLS":
                sym = src.upper() + "IRT"
                quote = "IRT"
            else:
                continue
            tradable = not st.get("isClosed", False)
            out.append({
                "symbol": sym,
                "base_asset": src.upper(),
                "quote_asset": quote,
                "status": "ACTIVE" if tradable else "SUSPENDED",
                "tradable": tradable,
                "price_precision": "1",
                "amount_precision": "0.00000001",
                "min_amount": "0.0001",
                # Nobitex global minimum order value: 2,000,000 Rials
                # (200,000 Toman). The risk engine enforces this via
                # exchange.min_order_usdt converted at the live USDT-IRT
                # price; this field documents it per market.
                "min_notional": "2000000",
                "market_type": "SPOT",
                "source": self.name,
                "updated_at": now,
            })
        return out

    def _stats_all(self) -> dict:
        with self._stats_lock:
            now = time.time()
            if self._stats_cache and now - self._stats_ts < 10:
                return self._stats_cache
            data = self._public_get("/market/stats")
            self._stats_cache = data.get("stats", {})
            self._stats_ts = now
            return self._stats_cache

    # ------------------------------------------------- whole-market index
    def all_tickers(self) -> dict:
        """EVERY listed market in ONE public call (the live universe).

        `GET /market/stats` with no parameters returns all pairs, so the
        bot can price/scan the whole exchange without a snapshot and with
        no per-market round trip. Rial-quoted pairs are converted to the
        bot's Toman unit and Tether pairs stay in USDT (same boundary rule
        as ticker()); everything is mapped into the canonical symbol space
        (BTC-RLS -> BTCIRT, BTC-USDT -> BTCUSDT, usdt-rls -> USDTIRT).
        """
        stats = self._stats_all()
        now = utc_now_iso()
        out: dict = {}
        for pair, st in (stats or {}).items():
            src, _, dst = str(pair).partition("-")
            dst_u = dst.upper()
            src_u = src.upper()
            if not src_u or not dst_u:
                continue
            if dst_u == "USDT":
                sym, quote = src_u + "USDT", "USDT"
                conv = float
            elif dst_u in ("RLS", "IRT"):
                sym, quote = src_u + "IRT", "IRT"
                conv = self._to_irt_price
            else:
                continue
            try:
                last = conv(st.get("latest") or 0)
                bid = conv(st.get("bestBuy") or 0)
                ask = conv(st.get("bestSell") or 0)
            except Exception:
                continue
            if not last or last <= 0:
                continue
            spread = (ask - bid) if (ask and bid) else 0.0
            out[sym] = {
                "symbol": sym,
                "base_asset": src_u,
                "quote_asset": quote,
                "last_price": last,
                "best_bid": bid,
                "best_ask": ask,
                "spread": spread,
                "spread_percent": (spread / last * 100) if last else 0.0,
                "volume_24h": float(st.get("volumeSrc") or 0),
                "quote_volume_24h": float(st.get("volumeDst") or 0),
                "change_24h": float(st.get("dayChange") or 0),
                "high_24h": conv(st.get("dayHigh") or 0),
                "low_24h": conv(st.get("dayLow") or 0),
                "tradable": not st.get("isClosed", False),
                "timestamp": now,
                "received_at": now,
            }
        return out

    # --------------------------------------------------------------- ticker
    def ticker(self, symbol: str) -> dict:
        src, dst = split_symbol(symbol)
        dst_param = "usdt" if dst == "USDT" else "rls"
        stats = self._stats_all()
        # NEVER mix an RLS-market price into a USDT-quoted ticker: the RLS
        # price is 10^7x larger and would poison sizing/min-notional math.
        st = stats.get("%s-%s" % (src.lower(), dst_param)) or {}
        if not st:
            data = self._public_get("/market/stats", {
                "srcCurrency": src.lower(), "dstCurrency": dst_param})
            st = (data.get("stats") or {}).get(
                "%s-%s" % (src.lower(), dst_param)) or {}
        if src.upper() == "USDT" and dst.upper() == "IRT":
            return self._usdtirt_ticker(st)
        if dst == "USDT":
            # USDT-quoted market: the exchange reports prices in USDT.
            # Do NOT apply the Rial->Toman divisor here — dividing tether
            # prices by 10 made every limit BUY rest 10x below the market
            # so orders never filled (verified live: DAIUSDT 0.998 ->
            # 0.0998, BTCUSDT 83550 -> 8355).
            last = float(st.get("latest") or 0)
            best_ask = float(st.get("bestSell") or 0)
            best_bid = float(st.get("bestBuy") or 0)
        else:
            last = self._to_irt_price(st.get("latest"))
            best_ask = self._to_irt_price(st.get("bestSell"))  # docs: bestSell = ask
            best_bid = self._to_irt_price(st.get("bestBuy"))   # docs: bestBuy = bid
        spread = (best_ask - best_bid) if (best_ask and best_bid) else 0.0
        return {
            "symbol": symbol,
            "last_price": last,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_percent": (spread / last * 100) if last else 0.0,
            "volume_24h": float(st.get("volumeSrc") or 0),
            "quote_volume_24h": float(st.get("volumeDst") or 0),
            "change_24h": float(st.get("dayChange") or 0),
            "high_24h": float(st.get("dayHigh") or 0),
            "low_24h": float(st.get("dayLow") or 0),
            "timestamp": utc_now_iso(),
            "received_at": utc_now_iso(),
        }

    # ------------------------------------------------------------ orderbook
    def orderbook(self, symbol: str, depth: int = 20) -> dict:
        src, dst = split_symbol(symbol)
        data = self._public_get("/v3/orderbook/%s" % symbol.upper())
        # IRT-market orderbook prices arrive in Rials -> convert to Toman;
        # USDT-market prices arrive in USDT and must stay untouched.
        conv = (lambda p: float(p or 0)) if dst == "USDT" \
            else (lambda p: self._to_irt_price(p))
        bids = [{"price": conv(p), "amount": float(a),
                 "notional": round(conv(p) * float(a), 2)}
                for p, a in (data.get("bids") or [])]
        asks = [{"price": conv(p), "amount": float(a),
                 "notional": round(conv(p) * float(a), 2)}
                for p, a in (data.get("asks") or [])]
        bids.sort(key=lambda x: -x["price"])
        asks.sort(key=lambda x: x["price"])
        bids, asks = bids[:depth], asks[:depth]
        bid_vol = sum(x["amount"] for x in bids)
        ask_vol = sum(x["amount"] for x in asks)
        return {
            "symbol": symbol,
            "timestamp": utc_now_iso(),
            "sequence": int(time.time() * 1000),
            "bids": bids, "asks": asks,
            "depth_levels": depth,
            "bid_volume": bid_vol, "ask_volume": ask_vol,
            "imbalance": (bid_vol - ask_vol) / max(bid_vol + ask_vol, 1e-9),
        }

    # -------------------------------------------------------------- candles
    def candles(self, symbol: str, timeframe: str = "30m", limit: int = 120) -> list:
        resolution = TF_TO_RESOLUTION.get(timeframe, "30")
        step = RESOLUTION_SECONDS.get(resolution, 1800)
        now = int(time.time())
        # the udf endpoint needs an explicit from/to window (countback alone
        # returns no_data); cap the window at `limit` bars
        params = {"symbol": symbol.upper(), "resolution": resolution,
                  "from": now - step * max(2, min(limit, 3000)), "to": now}
        data = self._public_get("/market/udf/history", params)
        if data.get("s") != "ok":
            return []
        out = []
        times = data.get("t") or []
        opens, highs = data.get("o") or [], data.get("h") or []
        lows, closes = data.get("l") or [], data.get("c") or []
        volumes = data.get("v") or [0] * len(times)
        # udf candles arrive pre-divided (Toman for IRT markets AND USDTIRT
        # — verified: USDTIRT candle 230,900 vs stats 2,308,320 Rls) — the
        # ONLY price feed that is NOT raw Rials. Identity conv.
        conv = lambda v: float(v or 0)  # noqa: E731
        for i in range(len(times)):
            out.append({
                "timestamp": epoch_to_iso(times[i]),
                "open": conv(opens[i] if i < len(opens) else closes[i]),
                "high": conv(highs[i] if i < len(highs) else closes[i]),
                "low": conv(lows[i] if i < len(lows) else closes[i]),
                "close": conv(closes[i]),
                "volume": volumes[i] if i < len(volumes) else 0,
            })
        return out

    # --------------------------------------------------------------- trades
    def trades(self, symbol: str, limit: int = 30) -> list:
        src, dst = split_symbol(symbol)
        data = self._public_get("/v2/trades/%s" % symbol.upper())
        out = []
        for t in (data.get("trades") or [])[-limit:]:
            price = float(t.get("price") or 0) if dst == "USDT" \
                else self._to_irt_price(t.get("price"))
            amount = float(t.get("amount") or 0)
            ts = int(t.get("time") or 0)
            out.append({
                "trade_id": str(t.get("trade_id") or t.get("id") or ""),
                "timestamp": epoch_to_iso(ts),
                "price": price,
                "amount": amount,
                "side": (t.get("side") or "").upper(),
                "notional": round(price * amount, 2),
            })
        return out

    # ------------------------------------------------------------ portfolio
    def portfolio(self) -> dict:
        """Real spot wallet (SS21), valued by the EXCHANGE where possible.

        Nobitex returns ``rialBalance``/``rialBalanceSell`` on every spot
        wallet: the exchange's own current value of that balance in Rials.
        That is the number its wallet page shows, and it is authoritative
        for coins with no live ticker (XIRT) too — valuing those from a
        missing/old market price made their Toman value wrong (and silently
        dropped them from equity). We therefore prefer ``rialBalance`` and
        only fall back to the USDT-market/USDTIRT ticker when it is absent
        or zero (fresh wallet, exchange glitch).

        RLS balances are divided by rls_to_irt_divisor (Rial->Toman). The
        returned per-asset ``value_irt``/``price_irt`` are the exact
        exchange valuations the holdings box now uses.
        """
        data = self._get("/users/wallets/list", {"type": "spot"})
        div = float(getattr(self.cfg, "rls_to_irt_divisor", 10) or 10)

        # ---- live fallback prices (best effort; zeros on failure)
        usdt_irt_toman = 0.0   # one USDT = this many Toman
        asset_usdt = {}
        try:
            stats = self._stats_all()
            for pair, st in stats.items():
                src, _, dst = pair.partition("-")
                if dst.lower() == "usdt":
                    asset_usdt[src.upper()] = float(st.get("latest") or 0)
                elif dst.lower() == "rls" and src.lower() == "usdt":
                    # one USDT in Toman (Rials / divisor)
                    usdt_irt_toman = self._to_irt_price(st.get("latest"))
        except Exception:
            pass

        assets = []
        total_usdt = 0.0
        available_usdt = 0.0
        total_irt = 0.0
        available_irt = 0.0
        for w in (data.get("wallets") or []):
            cur = (w.get("currency") or "").upper()   # rls -> RLS
            asset = "IRT" if cur == "RLS" else cur
            available = float(w.get("activeBalance") or 0)
            locked = float(w.get("blockedBalance") or 0)
            total = available + locked
            # the exchange's own valuation (Rials -> Toman), if present
            try:
                value_irt = float(w.get("rialBalance") or 0) / div
            except (TypeError, ValueError):
                value_irt = 0.0
            value_source = "wallet" if value_irt > 0 else "market"
            if asset == "IRT":
                # rls balances are in Rial; Toman = /10 (configurable)
                available, locked, total = (available/div, locked/div,
                                            total/div)
                if value_irt <= 0:
                    value_irt = total
                rate_usdt = (1.0 / usdt_irt_toman) if usdt_irt_toman else 0.0
                total_usdt += value_irt * rate_usdt
                available_usdt += available * rate_usdt
                total_irt += value_irt
                available_irt += available
            else:
                rate_usdt = asset_usdt.get(asset, 1.0 if asset == "USDT" else 0.0)
                if (value_irt <= 0 and total > 0 and rate_usdt > 0
                        and usdt_irt_toman > 0):
                    value_irt = total * rate_usdt * usdt_irt_toman
                    value_source = "market"
                value_usdt = (value_irt / usdt_irt_toman
                              if usdt_irt_toman else total * rate_usdt)
                total_usdt += value_usdt
                available_usdt += available * rate_usdt
                total_irt += value_irt
            value_usdt = (value_irt / usdt_irt_toman if usdt_irt_toman
                          else total * rate_usdt)
            assets.append({
                "asset": asset,
                "available": str(available),
                "locked": str(locked),
                "total": str(total),
                "quote_asset": "USDT",
                "price_usdt": (value_usdt / total if total > 0 else rate_usdt),
                "value_usdt": round(value_usdt, 2),
                # exact exchange valuation (Toman) + Toman per unit — the
                # holdings/portfolio rows read these instead of re-pricing
                # the coin from a ticker that may be stale or missing.
                "value_irt": round(value_irt, 2),
                "price_irt": (value_irt / total if total > 0 else 0.0),
                "value_source": value_source,
            })
        equity_irt = total_irt if total_irt > 0 else 0.0
        equity_usdt = (equity_irt / usdt_irt_toman if usdt_irt_toman
                       else total_usdt)
        return {
            "timestamp": utc_now_iso(),
            "account": {"equity_irt": round(equity_irt, 2),
                        "equity_usdt": round(equity_usdt, 2),
                        "available_irt": round(available_irt, 2),
                        "available_usdt": round(available_usdt, 2)},
            "assets": assets,
        }

    # ------------------------------------------------ account daily P&L
    def portfolio_daily_profit(self) -> dict:
        """The exchange's own daily portfolio P&L (wallet P&L panel).

        ``POST /users/portfolio/last-week-daily-profit`` returns one row
        per day: the day's profit and its percentage, plus the account's
        total balance after that day. These are the exact numbers the
        Nobitex wallet/portfolio view prints; the local equity history is
        only a fallback when this call is unavailable.

        The API reports Rials, so the Toman P&L/balance are divided by the
        configured rial->Toman divisor.
        """
        data = self._post("/users/portfolio/last-week-daily-profit", {})
        rows = data.get("data") or []
        if not rows:
            return {}
        latest = max(rows, key=lambda r: str(r.get("report_date") or ""))
        div = float(getattr(self.cfg, "rls_to_irt_divisor", 10) or 10)
        try:
            profit_irt = float(latest.get("total_profit") or 0) / div
        except (TypeError, ValueError):
            profit_irt = None
        try:
            balance_irt = float(latest.get("total_balance") or 0) / div
        except (TypeError, ValueError):
            balance_irt = None
        try:
            pct = float(latest.get("total_profit_percentage"))
        except (TypeError, ValueError):
            pct = None
        return {"report_date": str(latest.get("report_date") or ""),
                "profit_irt": profit_irt, "pct": pct,
                "balance_irt": balance_irt,
                "source": "nobitex-portfolio-daily"}

    # ------------------------------------------ exchange wallet cost basis
    #: «خرید 0.3 اتم به قیمت واحد 447,284 تومان» / «... 2.041 تتر»
    _TX_PRICE_RE = re.compile(
        r"به قیمت واحد\s*([\d,]+(?:\.\d+)?)\s*(تومان|تتر|USDT|دلار|ریال|﷼)",
        re.IGNORECASE)
    #: /users/transactions-history pages at 50 by default. Read up to 300
    #: rows (3 x 100) for an exact remaining lot; 14 held coins still stay
    #: well under the endpoint's 60-requests/hour limit.
    WALLET_TX_PAGE_SIZE = 100
    WALLET_TX_MAX_PAGES = 3

    def wallet_cost_bases(self, usdt_toman: float | None = None) -> dict:
        """Rebuild every held coin's cost from the exchange's OWN ledger.

        Nobitex has no public per-asset P&L endpoint, but its wallet shows
        exactly what its transaction history says: the remaining quantity
        and the Toman amount that quantity cost. This method reads
        ``/users/transactions-history`` per held currency and applies
        average-cost accounting to the real BUYs/SELLs (including manual
        trades the bot never placed).

        The result is keyed by the wallet ticker and contains ``amount``,
        ``cost_irt``, ``avg_irt``, the quote side the remaining quantity
        was really bought in (``quote``), the most recent BUY time/price
        and per-quote net quantities. The engine uses it to repair the
        local ledger's average entry — the local weighted average stayed
        wrong after partial sells/re-buys, which made both the Toman P&L
        and its percentage disagree with the Nobitex wallet.

        Tether-priced transactions are converted with ``usdt_toman`` (the
        live Toman value of one USDT) so a Tether-bought coin can be
        compared with its wallet rialBalance. Transactions whose price is
        missing are counted in ``missing_price`` and make ``complete``
        false; callers may keep their previous basis in that case.
        """
        div = float(getattr(self.cfg, "rls_to_irt_divisor", 10) or 10)
        data = self._get("/users/wallets/list", {"type": "spot"})
        wallets = data.get("wallets") or []
        if not usdt_toman or float(usdt_toman) <= 0:
            for w in wallets:
                if (w.get("currency") or "").lower() == "usdt":
                    amt = float(w.get("balance") or 0)
                    val = float(w.get("rialBalance") or 0) / div
                    if amt > 0 and val > 0:
                        usdt_toman = val / amt
                    break
        usdt_toman = float(usdt_toman or 0)
        out: dict = {}
        for w in wallets:
            cur = (w.get("currency") or "").strip().lower()
            if not cur or cur == "rls":
                continue
            try:
                held = float(w.get("balance") or 0)
            except (TypeError, ValueError):
                held = 0.0
            if held <= 0:
                continue
            txs = []
            seen_ids = set()
            has_next = False
            pages = 0
            try:
                while pages < self.WALLET_TX_MAX_PAGES:
                    pages += 1
                    hist = self._get(
                        "/users/transactions-history",
                        {"currency": cur, "page": pages,
                         "pageSize": self.WALLET_TX_PAGE_SIZE})
                    batch = hist.get("transactions") or []
                    for tx in batch:
                        tid = tx.get("id")
                        if tid is not None:
                            key = str(tid)
                            if key in seen_ids:
                                continue
                            seen_ids.add(key)
                        txs.append(tx)
                    has_next = bool(hist.get("hasNext"))
                    if not has_next or not batch:
                        break
            except Exception:
                # Keep the rows already read, but NEVER call a partial
                # history complete: a remaining lot priced from only its
                # newest trades is the exact mismatch this reader fixes.
                has_next = True
            if not txs:
                continue
            qty = Decimal("0")
            cost = Decimal("0")
            realized = Decimal("0")
            q_by_quote = {"IRT": Decimal("0"), "USDT": Decimal("0")}
            last_buy_at = ""
            last_buy_price = Decimal("0")
            last_buy_price_irt = Decimal("0")
            last_quote = ""
            missing = 0
            for t in sorted(txs, key=lambda z: str(z.get("created_at") or "")):
                tp = str(t.get("tp") or "").lower()
                if tp not in ("buy", "sell"):
                    continue
                try:
                    q = abs(Decimal(str(t.get("amount") or 0)))
                except Exception:
                    continue
                if q <= 0:
                    continue
                m = self._TX_PRICE_RE.search(str(t.get("description") or ""))
                if not m:
                    missing += 1
                    continue
                try:
                    price = Decimal(m.group(1).replace(",", ""))
                except Exception:
                    missing += 1
                    continue
                unit = m.group(2).strip()
                is_usdt = unit in ("تتر", "USDT", "دلار")
                if unit in ("ریال", "﷼"):
                    price = price / Decimal(str(div))
                price_irt = (price * Decimal(str(usdt_toman))
                             if is_usdt else price)
                q_side = "USDT" if is_usdt else "IRT"
                if tp == "buy":
                    qty += q
                    cost += q * price_irt
                    q_by_quote[q_side] += q
                    last_buy_at = str(t.get("created_at") or last_buy_at)
                    last_buy_price = price
                    last_buy_price_irt = price_irt
                    last_quote = q_side
                else:
                    if qty > 0:
                        avg = cost / qty
                        cost -= avg * q
                        realized += q * price_irt - avg * q
                        qty -= q
                        q_by_quote[q_side] -= q
                        if qty <= Decimal("0.000000000001"):
                            # the lot is fully closed: a later re-buy opens
                            # a NEW lot, so the old quote mix must not
                            # decide where the new coins are filed.
                            qty = Decimal("0")
                            cost = Decimal("0")
                            q_by_quote = {"IRT": Decimal("0"),
                                          "USDT": Decimal("0")}
            if qty <= 0 or cost <= 0:
                continue
            irt_q = max(Decimal("0"), q_by_quote.get("IRT", Decimal("0")))
            usdt_q = max(Decimal("0"), q_by_quote.get("USDT", Decimal("0")))
            quote = ("IRT" if irt_q >= usdt_q else "USDT") \
                if (irt_q > 0 or usdt_q > 0) else last_quote
            out[cur.upper()] = {
                "amount": float(qty),
                "cost_irt": float(cost),
                "avg_irt": float(cost / qty),
                "quote": quote,
                "qty_by_quote": {"IRT": float(irt_q),
                                  "USDT": float(usdt_q)},
                "realized_irt": float(realized),
                "last_buy_at": last_buy_at or None,
                "last_buy_price": float(last_buy_price),
                "last_buy_price_irt": float(last_buy_price_irt),
                "last_quote": last_quote or quote,
                "transactions": len(txs),
                "pages": pages,
                "missing_price": missing,
                "complete": (not has_next) and missing == 0,
                "source": "exchange-history",
            }
        return out

    # ------------------------------------------------- deposits / withdraws
    @staticmethod
    def _turnover_key(item: dict, kind: str) -> str:
        """Stable identity of one turnover row, for page de-duplication."""
        rid = item.get("id")
        if rid not in (None, ""):
            return "%s:%s" % (kind, rid)
        return "%s:%s:%s:%s:%s" % (
            kind, item.get("createdAt") or item.get("date"),
            item.get("amount"), item.get("currency"), item.get("status"))

    def deposits_withdraws(self, wallet_id: str | None = None,
                           max_pages: int = 50) -> dict:
        """Panel turnover data: POST /users/wallets/deposits/list.

        Returns {"deposits": [...], "withdraws": [...]} — each item has an
        amount (string) and currency (lowercase, e.g. "rls", "usdt").
        These are the same numbers the dashboard's
        /panel/turnover/deposit and /panel/turnover/withdraw pages show.

        PAGINATED: the endpoint returns 20 deposits per page plus a
        `hasNext` flag, and reading only page 1 silently TRUNCATES the
        history — older deposits vanish from Σdeposits while every
        withdrawal is still listed. The missing capital then reads as
        profit (user report: «الان واریز کردم، جزو سود حساب شد»). Here we
        follow `hasNext` and merge every page; withdrawals are repeated on
        each page, so rows are de-duplicated by id (or a content key).
        """
        deposits: list = []
        withdraws: list = []
        seen_d: set = set()
        seen_w: set = set()
        pages = 0
        truncated = False
        while True:
            body: dict = {"page": pages + 1}
            if wallet_id:
                body["wallet"] = str(wallet_id)
            data = self._post("/users/wallets/deposits/list", body)
            if str((data or {}).get("status", "ok")).lower() != "ok":
                raise NobitexError("DEPOSITS_LIST_FAILED",
                                   _api_error_text(data or {}), retryable=True)
            pages += 1
            added = 0
            d_page = data.get("deposits") or []
            w_page = data.get("withdraws") or []
            for item in d_page:
                key = self._turnover_key(item, "deposit")
                if key in seen_d:
                    continue
                seen_d.add(key)
                deposits.append(item)
                added += 1
            for item in w_page:
                key = self._turnover_key(item, "withdraw")
                if key in seen_w:
                    continue
                seen_w.add(key)
                withdraws.append(item)
                added += 1
            has_next = data.get("hasNext")
            if isinstance(has_next, str):
                has_next = has_next.strip().lower() in ("1", "true", "yes")
            if not has_next:
                break
            if pages >= max_pages:
                # honest flag instead of pretending the totals are complete
                truncated = True
                break
            if added == 0:
                # a server that ignores `page` must not loop forever
                truncated = True
                break
        return {
            "deposits": deposits,
            "withdraws": withdraws,
            "pages": pages,
            "truncated": truncated,
        }

    # ------------------------------------------------------ diagnostic lock
    @staticmethod
    def is_read_only() -> bool:
        """Hard read-only lock for diagnostics (env ``NOBITEX_READ_ONLY=1``).

        WHY THIS EXISTS: a saved login token in data/real hot-attaches the
        real adapter when the engine is constructed, so a script whose only
        intent was to PRINT holdings reached a fully live adapter (old
        builds even force-enabled ``live_trading`` there). Every
        wallet-moving call checks this lock at the ADAPTER boundary, so a
        read-only process cannot move money through ANY engine path
        (executor, dust sweep, exit conversion, order reaper, cancel).

        Read side (tickers, orderbooks, portfolio, candles) is unaffected.
        """
        raw = (os.environ.get("NOBITEX_READ_ONLY") or "").strip().lower()
        return raw not in ("", "0", "false", "no", "off")

    def _refuse_read_only(self, what: str) -> None:
        """Raise instead of moving money while NOBITEX_READ_ONLY is set."""
        if self.is_read_only():
            raise NobitexError(
                "READ_ONLY",
                "NOBITEX_READ_ONLY is set: refusing to %s" % what)

    # --------------------------------------------------------------- orders
    def place_limit_order(self, market: str, side: str, price, amount,
                          client_order_id: str) -> dict:
        self._refuse_read_only("place a real order on %s" % market)
        src, dst = split_symbol(market)
        # the bot's prices are in Toman for IRT markets; Nobitex expects
        # Rials — convert back at the boundary (×divisor)
        price_out = price
        if dst != "USDT":
            price_out = d(str(price)) * Decimal(str(self._irt_divisor))
        price_out = self._sanitize_price(market, price_out)
        amount_out = self._sanitize_amount(market, d(str(amount)))
        body = {
            "type": side.lower(),                 # buy | sell
            "execution": "limit",
            "srcCurrency": src.lower(),
            "dstCurrency": "usdt" if dst == "USDT" else "rls",
            "amount": self._plain_num(amount_out),
            "price": self._plain_num(price_out),
            "clientOrderId": self._client_order_id(client_order_id),
        }
        data = self._post("/market/orders/add", body)
        order = data.get("order") or {}
        oid = str(order.get("id") or "")
        status = map_status(order.get("status", "Active"))
        matched = str(order.get("matchedAmount") or "0")
        return {
            "order_id": oid,
            "client_order_id": client_order_id,
            "market": market,
            "side": side,
            "order_type": "LIMIT",
            "price": str(price),
            "amount": str(amount),
            "matched_amount": matched,
            "unmatched_amount": str(order.get("unmatchedAmount") or amount),
            "status": status,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }

    def order_status(self, order_id: str, market: str = "") -> dict:
        data = self._post("/market/orders/status", {"id": order_id})
        o = data.get("order") or {}
        # averagePrice arrives in Rials ONLY for Rial-quoted (IRT) markets;
        # USDT-quoted orders report USDT. Dividing those by the Rial->Toman
        # divisor recorded tether fills at 1/10 of the real price (fake
        # 10x PnL), so only convert when the order is actually rls-quoted.
        dst_raw = (str(o.get("dstCurrency") or "")).lower()
        if dst_raw:
            rls_quoted = not ("usdt" in dst_raw or "tether" in dst_raw)
        elif market:
            rls_quoted = split_symbol(market)[1] != "USDT"
        else:
            rls_quoted = True   # legacy default: IRT orders
        avg = o.get("averagePrice") or 0
        try:
            avg = float(avg) / self._irt_divisor if rls_quoted else float(avg)
        except Exception:
            avg = 0.0
        return {
            "order_id": str(o.get("id") or order_id),
            "status": map_status(o.get("status", "")),
            "matched_amount": str(o.get("matchedAmount") or "0"),
            "average_price": str(avg),
            "fee": str(o.get("fee") or "0"),
        }

    def open_orders(self, market=None) -> list:
        params = {"status": "open", "details": 2, "tradeType": "spot"}
        if market:
            src, dst = split_symbol(market)
            params["srcCurrency"] = src.lower()
            params["dstCurrency"] = "usdt" if dst == "USDT" else "rls"
        data = self._get("/market/orders/list", params)
        out = []
        for o in (data.get("orders") or []):
            # the exchange echoes its own coin code (PAXGold, Bio Protocol)
            # and calls the Tether quote "Tether" (not "USDT") — resolve
            # both to the bot's canonical symbol space. Comparing the
            # UPPERCASED dst string against "Tether" never matched, so
            # every tether-quoted order was reported as an IRT market with a
            # 1/10 (Rial->Toman) missing conversion.
            src = canonical_base(o.get("srcCurrency") or "")
            dst_raw = str(o.get("dstCurrency") or "").lower()
            rls_quoted = not ("usdt" in dst_raw or "tether" in dst_raw
                              or "dollar" in dst_raw)
            dst = "IRT" if rls_quoted else "USDT"
            # Rial-quoted orders report their price in RIALS while the bot
            # prices in TOMAN — convert so resting-order math (limit chase,
            # ledger/history) is unit-consistent with the rest of the bot.
            price = (self._rls_price_str(o.get("price"))
                     if rls_quoted else o.get("price"))
            # the REAL placement time from the exchange (ISO string) — the
            # stale-order reaper depends on it; falling back to "now" here
            # would make every resting order look brand-new forever
            created = o.get("created_at") or utc_now_iso()
            out.append({
                "order_id": str(o.get("id") or ""),
                "client_order_id": o.get("clientOrderId"),
                "market": "%s%s" % (src, dst),
                "side": (o.get("type") or "").upper(),
                "order_type": "LIMIT",
                "price": price,
                "amount": o.get("amount"),
                "matched_amount": str(o.get("matchedAmount") or "0"),
                "unmatched_amount": str(o.get("unmatchedAmount") or "0"),
                "status": map_status(o.get("status", "")),
                "created_at": created,
                "updated_at": created,
            })
        return out

    def cancel_order(self, order_id: str) -> dict:
        self._refuse_read_only("cancel order %s" % order_id)
        data = self._post("/market/orders/update-status",
                          {"order": int(order_id), "status": "canceled"})
        return {"order_id": str(order_id),
                "status": "CANCELLED",
                "raw_status": data.get("updatedStatus")}

    def health(self) -> dict:
        t0 = time.time()
        try:
            self._public_get("/market/stats",
                             {"srcCurrency": "btc", "dstCurrency": "rls"})
            return {"api": {"status": "HEALTHY",
                            "latency_ms": int((time.time() - t0) * 1000)}}
        except NobitexError as exc:
            return {"api": {"status": "DEGRADED",
                            "latency_ms": int((time.time() - t0) * 1000),
                            "error": exc.code}}

    # ------------------------------------------------ convert (swap) market
    def convert_wallet_balance(self, coin: str) -> dict:
        """Convert the WHOLE spot balance of `coin` into IRT via
        POST /users/wallets/convert — the "تبدیل موجودی‌های خرد" page.

        Discovered live: body {'srcCurrency': '<lower-coin>',
        'dstCurrency': 'rls'} converts the ENTIRE available balance with
        NO minimum-notional floor (verified: XLM worth ~4k toman converted
        fine while the exchange/get-quote market rejects anything below
        25k toman). Response: {'status':'ok','converts':{'xlm':'0.08875'}}.

        SAFETY: the convert market has no minimum and moves the WHOLE
        balance, so it is the single most destructive call in the adapter —
        it obeys the NOBITEX_READ_ONLY lock like every other write.
        """
        self._refuse_read_only("convert the whole %s balance" %
                              (coin or "").upper())
        data = self.session.authed_request("POST", "/users/wallets/convert",
            {"srcCurrency": (coin or "").strip().lower(),
             "dstCurrency": "rls"})
        converts = data.get("converts") or {}
        amount = converts.get((coin or "").lower())
        return {
            "converted": True,
            "coin": coin.upper(),
            "converted_amount": amount,
        }

    def convert_spot(self, coin: str, rls_amount) -> dict:
        """Convert `rls_amount` (rials worth of `coin`) into IRT via the
        Nobitex convert market — same flow as the panel convert page:
        get-quote then create-trade. Raises NobitexError on failure."""
        quote = self.session.exchange_quote(coin, str(rls_amount))
        if not quote.get("quote_id"):
            raise NobitexError("CONVERT_NO_QUOTE",
                               "پیش‌فاکتور تبدیل دریافت نشد", False)
        res = self.session.exchange_create_trade(quote["quote_id"])
        status = (res.get("status") or "").lower()
        if status and status not in ("ok", "succeeded", "success", "done"):
            raise NobitexError("CONVERT_FAILED",
                               "تبدیل ناموفق: %s" % status, False)
        return {
            "converted": True,
            "coin": coin.upper(),
            "ref_amount_rls": quote.get("ref_amount"),
            "dest_amount": quote.get("dest_amount"),
            "quote_id": quote.get("quote_id"),
            "expires_at": quote.get("expires_at"),
        }

    # ------------------------------------ Toman ⇄ Tether (manual convert)
    def convert_irt_to_usdt(self, toman_amount) -> dict:
        """Convert `toman_amount` (TOMAN) into USDT via the exchange's own
        convert market: get-quote (type=buy, baseCurrency=usdt, amount in
        RIALS to spend) then create-trade. Returns the USDT received.

        The exchange enforces its own minimum notional here (USDT ≈
        12.5M rials); a smaller amount raises NobitexError with the
        exchange's code so the dashboard can show the exact reason.
        """
        self._refuse_read_only("convert Toman to USDT")
        div = Decimal(str(self._irt_divisor or 10))
        rls_amount = (Decimal(str(toman_amount)) * div)
        quote = self.session.exchange_quote(
            "usdt", str(rls_amount.quantize(Decimal("1"))),
            direction="buy")
        if not quote.get("quote_id"):
            raise NobitexError("CONVERT_NO_QUOTE",
                               "پیش‌فاکتور تبدیل دریافت نشد", False)
        res = self.session.exchange_create_trade(quote["quote_id"])
        status = (res.get("status") or "").lower()
        if status and status not in ("ok", "succeeded", "success", "done"):
            raise NobitexError("CONVERT_FAILED",
                               "تبدیل ناموفق: %s" % status, False)
        # destAmount = USDT received; refAmount = rials actually spent
        usdt_got = _to_float_or(quote.get("dest_amount"), 0.0)
        rls_spent = _to_float_or(quote.get("ref_amount"), 0.0)
        return {"converted": True,
                "converted_amount": usdt_got,
                "unit": "USDT",
                "spent_toman": rls_spent / (self._irt_divisor or 10),
                "quote_id": quote.get("quote_id"),
                "expires_at": quote.get("expires_at")}

    def convert_usdt_to_irt(self, usdt_amount, usdt_toman: float = 0.0) -> dict:
        """Convert `usdt_amount` USDT into Toman via the convert market:
        get-quote (type=sell, baseCurrency=usdt, amount = the RIAL value of
        the USDT being sold) then create-trade.

        usdt_toman = the Toman price of one USDT (from the wallet read);
        when it is missing we fall back to the cached public USDTIRT
        ticker so the rls value still lands in the right ballpark.
        """
        self._refuse_read_only("convert USDT to Toman")
        price = Decimal(str(usdt_toman or 0))
        if price <= 0:
            cached = self._cached_usdtirt_toman()
            price = Decimal(str(cached or 0))
        if price <= 0:
            raise NobitexError("CONVERT_NO_PRICE",
                               "قیمت تتر برای محاسبهٔ مبلغ ریالی پیدا نشد",
                               False)
        div = Decimal(str(self._irt_divisor or 10))
        rls_value = (Decimal(str(usdt_amount)) * price * div)
        quote = self.session.exchange_quote(
            "usdt", str(rls_value.quantize(Decimal("1"))),
            direction="sell")
        if not quote.get("quote_id"):
            raise NobitexError("CONVERT_NO_QUOTE",
                               "پیش‌فاکتور تبدیل دریافت نشد", False)
        res = self.session.exchange_create_trade(quote["quote_id"])
        status = (res.get("status") or "").lower()
        if status and status not in ("ok", "succeeded", "success", "done"):
            raise NobitexError("CONVERT_FAILED",
                               "تبدیل ناموفق: %s" % status, False)
        # destAmount = rials received -> Toman; refAmount = order rls value
        usdt_sold = _to_float_or(quote.get("dest_amount"), 0.0) / (self._irt_divisor or 10)
        return {"converted": True,
                "converted_amount": usdt_sold,
                "unit": "IRT",
                "usdt_sold": float(usdt_amount),
                "quote_id": quote.get("quote_id"),
                "expires_at": quote.get("expires_at")}

    def _cached_usdtirt_toman(self):
        """Last cached USDTIRT price (Toman) from the public stats poll,
        or None when no cached value exists yet."""
        st = (self._stats_cache or {}).get("usdt-rls") or {}
        try:
            return self._to_irt_price(st.get("latest") or 0)
        except Exception:
            return None
