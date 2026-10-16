"""Live console view — the cmd window start.bat opens.

The dashboard is a browser tab; this is the same bot in the window it was
started from: one line per trade/risk/health event, a heartbeat line with
the account numbers, and an explicit "stopped" line so «بستن پنجره =
توقف» is something you SEE rather than guess.

Nothing here can take the bot down: every callback is wrapped, the
heartbeat runs in a daemon thread, and a closed console only silences the
printer (the bot keeps trading). Output is plain ASCII with an explicit
flush so `python run.py --live > log.txt` also shows live progress.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime
from typing import Any

# Chatty per-market events: MARKET_DISABLED alone is ~270 lines per cycle
# and SNAPSHOT_CREATED / AI_ANALYSIS_STARTED are superseded by their own
# *_COMPLETED / _DECISION events on the next line.
SKIP = frozenset({
    "MARKET_DISABLED",
    "SNAPSHOT_CREATED",
    "AI_ANALYSIS_STARTED",
})

# events that mean money moved or the bot changed how it trades: always shown
HEADS = {
    "SYSTEM_ERROR": "!!",
    "SAFE_MODE_ENTERED": "!!",
    "RISK_REJECTED": "risk",
    "RISK_APPROVED": "risk",
    "ORDER_SUBMITTED": "order",
    "ORDER_OPENED": "order",
    "ORDER_FILLED": "order",
    "ORDER_PARTIALLY_FILLED": "order",
    "ORDER_CANCELLED": "order",
    "ORDER_REJECTED": "order",
    "POSITION_OPENED": "position",
    "POSITION_UPDATED": "position",
    "POSITION_CLOSED": "position",
    "AI_DECISION_CREATED": "decide",
    "SNAPSHOT_COMPLETED": "snapshot",
    "MARKET_DISCOVERED": "market",
    "EXCHANGE_ATTACHED": "exchange",
    "WALLET_SYNCED": "wallet",
    "LIVE_TRADING_CHANGED": "live",
    "LOGIN_SUCCESS": "login",
    "AUTO_STOP_SET": "stop",
    "EXIT_CONVERTED_BELOW_MIN": "exit-dust",
    "EXIT_STALE_POSITION_CLEARED": "exit-stale",
    "MANUAL_SELL": "sell",
    "MANUAL_BUY_EXECUTED": "buy",
    "PROFIT_TOPUP_FIRED": "topup",
    "DUST_CONVERTED": "dust",
    "DUST_CONVERT_FAILED": "dust",
    "RISK_MODE_CHANGED": "risk-mode",
    "RISK_MODE_SWEPT": "risk-mode",
    "RISK_MODE_SWEEP_FAILED": "risk-mode",
    "RISK_MODE_REGIME_BLOCKED": "risk-mode",
    "SAFE_MODE_EXITED": "safe-mode",
    "QUOTE_MARKETS_BOTH_OFF": "!!",
}

MAX_PART = 120          # keep one event on one line


def _num(value: Any, nd: int = 4) -> str:
    """Readable number, or the raw value when it is not numeric."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)[:24]
    if f != f or f in (float("inf"), float("-inf")):
        return str(value)[:24]
    if abs(f) >= 1000 or nd == 0:
        return ("{:,." + str(nd) + "f}").format(f)
    text = ("{:." + str(nd) + "f}").format(f)
    return text.rstrip("0").rstrip(".") or "0"


def _market(p: dict) -> str:
    return str(p.get("market") or p.get("symbol") or "?")


def _one_line(text: Any) -> str:
    flat = " ".join(str(text).split())
    return flat[:MAX_PART] + ("…" if len(flat) > MAX_PART else "")


def _clock(stamp: str) -> str:
    """Event UTC timestamp -> local HH:MM:SS (falls back to that text)."""
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except Exception:
        return str(stamp)[11:19] or "--:--:--"


def _decode_payload(p: dict) -> str:
    """A readable line for payloads the contract has no event type for.

    The engine publishes a few event names (WALLET_SYNCED, the cross-market
    gate, …) that are not in EVENT_TYPES, so the bus files them as
    SYSTEM_ERROR with no code/component and a plain data payload. Printing
    «? ERROR:» for a normal wallet sync is worse than useless, so those two
    shapes are decoded by their fields.
    """
    if "assets" in p and "positions_seeded" in p:
        return "wallet synced: %d assets, %d positions seeded, %d stale removed" % (
            len(p.get("assets") or []), p.get("positions_seeded") or 0,
            p.get("positions_stale_removed") or 0)
    if p.get("reason"):
        coins = p.get("coins") or []
        tail = " (" + ", ".join(str(c) for c in list(coins)[:6]) + ")" if coins else ""
        return "%s%s" % (p["reason"], tail)
    return " ".join("%s=%s" % (k, _one_line(v))
                    for k, v in list(p.items())[:4]) or "?"


def _reasons(p: dict) -> str:
    r = p.get("reasons") or p.get("rejection_reasons") or p.get("reason")
    if isinstance(r, (list, tuple)):
        return ", ".join(str(x) for x in r)
    return str(r or "")


def render_event(event: dict) -> str | None:
    """One console line for an event, or None when it is not worth a line."""
    etype = str(event.get("event_type") or "")
    if etype in SKIP:
        return None
    p = event.get("payload") or {}
    if not isinstance(p, dict):
        p = {"value": p}

    if etype == "SYSTEM_ERROR":
        if p.get("code") or p.get("component"):
            body = "%s %s: %s" % (p.get("component") or "?",
                                  p.get("code") or "ERROR",
                                  _one_line(p.get("message") or ""))
        else:
            body = _decode_payload(p)
    elif etype == "SNAPSHOT_COMPLETED":
        body = "%s: %s markets (%s valid)" % (
            p.get("snapshot_id") or "?", p.get("market_count"),
            p.get("valid_market_count"))
    elif etype == "AI_DECISION_CREATED":
        body = "%s %s conf %s" % (_market(p), p.get("action"),
                                  _num(p.get("confidence"), 2))
    elif etype in ("RISK_APPROVED", "RISK_REJECTED"):
        tail = _reasons(p)
        verb = (p.get("status") or
                ("BLOCKED" if etype == "RISK_REJECTED" else "approved"))
        body = "%s %s%s" % (verb,
                            _market(p) if p.get("market") else
                            str(p.get("decision_id") or "")[-8:],
                            (" - " + tail) if tail else "")
    elif etype.startswith("ORDER_"):
        bits = [_market(p), p.get("side"), _num(p.get("amount"))]
        if p.get("price"):
            bits.append("@ " + _num(p.get("price")))
        bits.append(etype.split("_", 1)[1].lower())
        body = " ".join(str(b) for b in bits if b)
    elif etype.startswith("POSITION_"):
        bits = [_market(p), _num(p.get("amount")) if p.get("amount") else ""]
        if p.get("realized_pnl") is not None:
            bits.append("pnl " + _num(p.get("realized_pnl")))
        if p.get("avg_entry") is not None:
            bits.append("entry " + _num(p.get("avg_entry")))
        body = "%s %s" % (etype.split("_", 1)[1].lower(),
                          " ".join(str(b) for b in bits if b))
    elif etype == "MARKET_DISCOVERED":
        body = _market(p)
    elif etype == "WALLET_SYNCED":
        body = "%s assets" % (p.get("assets") or p.get("count") or "?")
    elif etype in ("LIVE_TRADING_CHANGED", "RISK_MODE_CHANGED"):
        body = "enabled=%s" % (p.get("enabled", p.get("risk_mode", "?")))
    elif etype == "DUST_CONVERTED":
        body = "%s %s converted (dust)" % (_market(p), _num(p.get("amount")))
    elif etype in ("SAFE_MODE_ENTERED", "QUOTE_MARKETS_BOTH_OFF"):
        body = _one_line(_reasons(p) or p)
    elif etype == "LOGIN_SUCCESS":
        body = str(p.get("username") or p.get("mode") or "ok")
    elif etype == "EXCHANGE_ATTACHED":
        body = str(p.get("mode") or p.get("exchange") or "ok")
    elif etype == "AUTO_STOP_SET":
        body = "%s %s%%" % (_market(p), _num(p.get("stop_loss_pct"), 2))
    else:
        # anything not listed above (SAFE_MODE_EXITED, sweeps, topups, …):
        # type + the first few payload fields, still one line
        shown = " ".join("%s=%s" % (k, _one_line(v))
                         for k, v in list(p.items())[:4])
        body = shown or etype.lower()

    head = HEADS.get(etype, etype.lower())
    if etype == "SYSTEM_ERROR" and not (p.get("code") or p.get("component")):
        head = "info"          # a status payload, not a failure
    return "%s %-9s %s" % (_clock(event.get("timestamp", "")), head, body)


class LiveConsole:
    """Subscribes to the engine's event bus and prints the live view."""

    def __init__(self, engine, out=None, interval: float | None = None):
        self.engine = engine
        self.out = out if out is not None else sys.stdout
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- printing
    def _write(self, line: str) -> None:
        try:
            self.out.write(line + "\n")
            self.out.flush()
        except Exception:
            # console closed / pipe gone: stay silent instead of raising
            # inside the engine's publish loop
            pass

    def _on_event(self, event: dict) -> None:
        try:
            line = render_event(event)
        except Exception:
            line = None
        if line:
            self._write(line)

    # ------------------------------------------------------------ heartbeat
    def heartbeat(self) -> str:
        """One line with the account numbers the dashboard would show."""
        st: dict = {}
        try:
            st = self.engine.status() or {}
        except Exception as exc:
            return "[%s] cycle %s | status unavailable (%s)" % (
                datetime.now().strftime("%H:%M:%S"),
                getattr(self.engine, "_cycles_run", "?"), _one_line(exc))
        p = st.get("portfolio") or {}
        acct = p.get("account") or {}
        risk = p.get("risk") or {}
        snap = st.get("snapshot") or {}
        hp = st.get("holdings_pnl") or {}
        # the SAME numbers the dashboard's KPI cards show: «ارزش کل حساب»
        # (Toman, with its dollar equivalent) and «سود کل» (growth_pct)
        eq_irt, eq_usd = acct.get("equity_irt"), acct.get("equity_usdt")
        toman = "equity %s toman" % _num(eq_irt, 0) if eq_irt is not None else ""
        dollar = "~$%s" % _num(eq_usd, 2) if eq_usd is not None else ""
        equity_bit = (toman + " " + dollar).strip()

        bits = ["cycle %s" % st.get("cycles_run", "?")]
        if equity_bit:
            bits.append(equity_bit)
        if hp.get("growth_pct") is not None:
            bits.append("growth %+.2f%%" % float(hp["growth_pct"]))
        if risk.get("exposure_ratio") is not None:
            bits.append("exp %.1f%% %s" % (float(risk["exposure_ratio"]) * 100,
                                           risk.get("risk_status") or ""))
        # short labels: the line has to fit a 120-column console without
        # wrapping, or a heartbeat reads as two lines
        bits.append("pos %d" % len(p.get("positions") or []))
        open_orders = len(st.get("open_orders") or [])
        if open_orders:
            bits.append("orders %d" % open_orders)
        if snap.get("market_count"):
            bits.append("mkt %s" % snap.get("market_count"))

        head = "[%s] " % datetime.now().strftime("%H:%M:%S")
        # keep ONE line on a standard 120-column console: a very large
        # equity or a busy order book drops the dollar equivalent first
        body = " | ".join(bits)
        if len(head) + len(body) > 118 and toman and dollar:
            bits[bits.index(equity_bit)] = toman
            body = " | ".join(bits)
        return head + body

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                interval = float(self._interval or
                                 getattr(self.engine.cfg.engine, "cycle_seconds", 30))
            except Exception:
                interval = 30.0
            if self._stop.wait(max(5.0, interval)):
                return
            self._write(self.heartbeat())

    # -------------------------------------------------------------- control
    def attach(self) -> "LiveConsole":
        try:
            self.engine.events.subscribe(self._on_event)
        except Exception:
            pass
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="console-heartbeat")
        self._thread.start()
        self._write("[%s] live view on - Ctrl+C (or closing this window) "
                    "stops the bot" % datetime.now().strftime("%H:%M:%S"))
        return self

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._write("[%s] stopped - engine thread joined, dashboard port "
                    "released" % datetime.now().strftime("%H:%M:%S"))
