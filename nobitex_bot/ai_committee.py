"""Multi-model AI committee — several models vote, ONE logical decision.

درخواست کاربر: «تصمیم‌های AI منطقی باشد و طوری باشد که بعد از خرید، قیمت
توکن بالا برود؛ چند مدل AI در نظر بگیر و ساختارش را بساز.»

A single scorer cannot *show* that a buy will be followed by a higher price,
so this module turns the decision into an explicit forecast + expected value:

    decision models (independent, each votes)
    -----------------------------------------
    trend        EMA 9/20/50 alignment + market structure trend
    momentum     momentum + 24h momentum + MACD histogram sign
    breakout     detected breakout in the trade direction + liquidity
    meanrev      RSI zone + distance from EMA20 (never buy the top)
    liquidity *  liquidity score/status, spread, data quality  (VETO)
    volatility * ATR% regime — too dead or too wild       (VETO)
    llm:<name>   optional: every OpenAI-compatible model listed in
                 ai.llm_models gets one vote with a JSON forecast
    (*) veto models never vote BUY; they can only block.

Every voter returns:

    p_up               probability that the price is ABOVE the entry at the
                       horizon (ai.committee_horizon_s)
    expected_move_pct  signed expected move over that horizon, in percent

The committee aggregates the votes by weight and prices the trade:

    upside  = Σ w·max(expected, 0)          # how far it should go up
    downside= Σ w·max(-expected, 0)         # how far it may go against us
    net_edge_pct = p_up·upside - (1-p_up)·downside - cost_pct

A BUY is allowed only when ALL of these hold — the exact "will the price be
higher after the buy, by more than it costs?" test:

    1. no veto from any model
    2. quorum: at least ai.committee_min_votes models vote BUY
    3. p_up  >= ai.committee_min_p_up
    4. upside >= max(ai.committee_min_upside_pct, cost × 1.2)
    5. net_edge_pct >= ai.committee_min_net_edge_pct

Anything else is HOLD (or AVOID on a veto), with the blocking reasons kept
in the payload. The committee may only ever *block* an entry — it can never
bypass the risk engine, size limits or the SS32/33/34 safety rails.

`CommitteeScoreboard` closes the loop: every approved BUY is stored with its
forecast, and after the horizon the actual move is compared against it
(`rose`, `met_target`), so the user can see whether the models were right
instead of trusting them.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import Config

COMMITTEE_VERSION = "committee/1.0"
SCOREBOARD_KEY = "committee_scoreboard"

DEFAULT_WEIGHTS = {
    "trend": 1.2,
    "momentum": 1.1,
    "breakout": 0.9,
    "meanrev": 0.8,
    "liquidity": 0.0,
    "volatility": 0.0,
}
LLM_WEIGHT = 1.0

BUY, HOLD, AVOID, ABSTAIN = "BUY", "HOLD", "AVOID", "ABSTAIN"


# ----------------------------------------------------------------- helpers
def _f(x, default: float = 0.0) -> float:
    """Lenient float coercion — bad data never raises inside a voter."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v and abs(v) != float("inf") else default


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _p_from_score(s: float, span: float = 0.9) -> float:
    """Map a 0..1 score onto a 0.05..0.95 probability (0.5 stays 0.5)."""
    return round(_clamp(0.5 + (s - 0.5) * span, 0.05, 0.95), 4)


def _atr_pct_of(snap: dict) -> float:
    """ATR as a FRACTION of price (0.02 = 2%) — snapshot contract."""
    return max(0.0, _f((snap.get("volatility") or {}).get("atr_percent")))


def _price_of(snap: dict) -> float:
    return max(0.0, _f(snap.get("last_price")))


def _reason(verdict: str, code: str) -> str:
    return "%s:%s" % (verdict, code)


# ------------------------------------------------------------------ voters
def vote_trend(cand: dict, snap: dict, ctx: dict) -> dict:
    sig = cand.get("signals") or {}
    trend_s = _f(sig.get("trend"), 0.5)
    ema = (snap.get("technicals") or {}).get("ema") or {}
    e9, e20, e50 = _f(ema.get("9")), _f(ema.get("20")), _f(ema.get("50"))
    align = 0.5
    if e9 > 0 and e20 > 0 and e50 > 0:
        if e9 > e20 > e50:
            align = 1.0
        elif e9 > e20:
            align = 0.68
        elif e20 > e50:
            align = 0.32
        else:
            align = 0.0
    structure = str((snap.get("structure") or {}).get("trend") or "").upper()
    st = (1.0 if structure in ("UP", "UPTREND", "BULLISH") else
          0.0 if structure in ("DOWN", "DOWNTREND", "BEARISH") else 0.5)
    s = _clamp(0.45 * trend_s + 0.35 * align + 0.20 * st)
    p = _p_from_score(s)
    exp = ctx["atr_pct"] * 100 * 0.55 * (2 * s - 1)
    verdict = BUY if (s >= 0.62 and exp > 0) else (AVOID if s < 0.32 else HOLD)
    return {"p_up": p, "expected_move_pct": round(exp, 4),
            "verdict": verdict,
            "reason": _reason(verdict, "EMA_ALIGN_%s" % ("UP" if align > 0.6
                                                         else "DOWN"))}


def vote_momentum(cand: dict, snap: dict, ctx: dict) -> dict:
    sig = cand.get("signals") or {}
    mom = _f(sig.get("momentum"), 0.5)
    mom24 = _f(sig.get("momentum_24h"), 0.5)
    hist = _f(((snap.get("technicals") or {}).get("macd") or {}).get("histogram"))
    macd_s = 0.62 if hist > 0 else (0.38 if hist < 0 else 0.5)
    s = _clamp(0.40 * mom + 0.40 * mom24 + 0.20 * macd_s)
    p = _p_from_score(s, 0.8)
    exp = ctx["atr_pct"] * 100 * 0.6 * (2 * s - 1)
    verdict = BUY if (s >= 0.6 and exp > 0) else (AVOID if s < 0.3 else HOLD)
    return {"p_up": p, "expected_move_pct": round(exp, 4),
            "verdict": verdict,
            "reason": _reason(verdict, "MOM_%s" % ("UP" if hist >= 0 else
                                                   "DOWN"))}


def vote_breakout(cand: dict, snap: dict, ctx: dict) -> dict:
    sig = cand.get("signals") or {}
    liq_s = _f(sig.get("liquidity"), 0.0)
    bo = (snap.get("structure") or {}).get("breakout") or {}
    detected = bool(bo.get("detected"))
    direction = str(bo.get("direction") or "").upper()
    if detected and direction == "UP" and liq_s > 0.35:
        s, code = 0.88, "BREAKOUT_UP"
    elif detected and direction == "DOWN":
        s, code = 0.12, "BREAKOUT_DOWN"
    else:
        s, code = _clamp(0.5 + (liq_s - 0.5) * 0.2), "NO_BREAKOUT"
    p = _p_from_score(s, 1.0)
    exp = ctx["atr_pct"] * 100 * 0.9 * (2 * s - 1)
    verdict = BUY if (code == "BREAKOUT_UP" and exp > 0) else (
        AVOID if code == "BREAKOUT_DOWN" else HOLD)
    return {"p_up": p, "expected_move_pct": round(exp, 4),
            "verdict": verdict, "reason": _reason(verdict, code)}


def vote_meanrev(cand: dict, snap: dict, ctx: dict) -> dict:
    tech = snap.get("technicals") or {}
    rsi = tech.get("rsi")
    e20 = _f((tech.get("ema") or {}).get("20"))
    price = ctx["price"]
    if rsi is None or price <= 0:
        return {"p_up": 0.5, "expected_move_pct": 0.0, "verdict": ABSTAIN,
                "reason": "ABSTAIN:NO_RSI"}
    rsi = _f(rsi, 50.0)
    if rsi >= 72:
        s, code = 0.22, "OVERBOUGHT"
    elif rsi >= 65:
        s, code = 0.42, "EXTENDED"
    elif rsi >= 45:
        s, code = 0.68, "HEALTHY_PULLBACK"
    elif rsi >= 35:
        s, code = 0.50, "NEUTRAL"
    else:
        s, code = 0.30, "FALLING_KNIFE"
    # چسبیدن به EMA20 (حمایت) خوب است، فاصلهٔ زیاد بالای آن = خرید سقف
    if e20 > 0:
        ext = (price / e20 - 1.0) * 100
        if ext > 1.5:
            s -= min(0.25, (ext - 1.5) * 0.05)
            code = "CHASING_EMA20"
        elif -1.0 <= ext <= 0.3:
            s += 0.05
    s = _clamp(s)
    p = _p_from_score(s, 0.7)
    exp = ctx["atr_pct"] * 100 * 0.45 * (2 * s - 1)
    verdict = BUY if (s >= 0.62 and exp > 0) else (AVOID if s < 0.3 else HOLD)
    return {"p_up": p, "expected_move_pct": round(exp, 4),
            "verdict": verdict, "reason": _reason(verdict, code)}


def vote_liquidity(cand: dict, snap: dict, ctx: dict) -> dict:
    """Veto model: never votes BUY — it only says «این بازار نقدشونده نیست»."""
    liq = snap.get("liquidity") or {}
    score = _f(liq.get("score"), 0.0)
    status = str(liq.get("status") or "").upper()
    spread = _f(snap.get("spread_percent"), 0.0)
    dq = _f((snap.get("data_quality") or {}).get("score"), 1.0)
    if status in ("CRITICAL", "UNKNOWN"):
        code = "LIQUIDITY_%s" % (status or "UNKNOWN")
    elif score < 0.30:
        code = "LIQUIDITY_SCORE_LOW"
    elif spread > 1.5:
        code = "WIDE_SPREAD"
    elif dq < 0.4:
        code = "LOW_DATA_QUALITY"
    else:
        return {"p_up": 0.5, "expected_move_pct": 0.0, "verdict": ABSTAIN,
                "reason": "ABSTAIN:TRADABLE"}
    return {"p_up": 0.05, "expected_move_pct": -ctx["atr_pct"] * 100,
            "verdict": AVOID, "reason": "AVOID:" + code, "veto": True}


def vote_volatility(cand: dict, snap: dict, ctx: dict) -> dict:
    """Veto model: ATR% outside the tradeable band kills the setup."""
    atr = ctx["atr_pct"]
    suit = _f((cand.get("signals") or {}).get("volatility"), 0.5)
    crash = max(1.0, _f(ctx.get("atr_crash_pct"), 8.0)) / 100.0
    if atr > crash:
        return {"p_up": 0.05, "expected_move_pct": -atr * 100,
                "verdict": AVOID, "reason": "AVOID:EXTREME_VOLATILITY",
                "veto": True}
    if 0 < atr < 0.002:
        return {"p_up": 0.1, "expected_move_pct": -0.05,
                "verdict": AVOID, "reason": "AVOID:DEAD_VOLATILITY",
                "veto": True}
    if suit < 0.25:
        return {"p_up": 0.1, "expected_move_pct": -atr * 100,
                "verdict": AVOID, "reason": "AVOID:UNSUITABLE_VOLATILITY",
                "veto": True}
    return {"p_up": 0.5, "expected_move_pct": 0.0, "verdict": ABSTAIN,
            "reason": "ABSTAIN:VOL_SUITABLE"}


_LLM_SYSTEM = (
    "You are one member of a trading committee. Reply ONLY with a JSON "
    "object: {\"p_up\": 0..1 (probability the price is HIGHER than the "
    "entry after the horizon), \"expected_move_pct\": signed percent move "
    "over that horizon, \"verdict\": \"BUY\"|\"HOLD\"|\"AVOID\", "
    "\"reason\": \"<=40 chars one code\"}. Be conservative: if the evidence "
    "for a higher price is weak or costs are not clearly covered, answer "
    "HOLD or AVOID. Never invent numbers that are not in the input.")


def vote_llm(model: str, cand: dict, snap: dict, ctx: dict,
             llm) -> dict:
    """One optional LLM voter. Any failure is an ABSTAIN, never a BUY."""
    payload = {
        "symbol": cand.get("symbol"),
        "horizon_minutes": round(ctx.get("horizon_s", 3600) / 60.0, 1),
        "price": ctx["price"],
        "atr_percent": round(ctx["atr_pct"] * 100, 3),
        "cost_percent": round(ctx["cost_pct"], 3),
        "spread_percent": _f(snap.get("spread_percent")),
        "change_24h_percent": _f(snap.get("change_24h")),
        "signals": cand.get("signals") or {},
        "score": cand.get("score"),
        "structure": snap.get("structure"),
        "technicals": {
            "rsi": (snap.get("technicals") or {}).get("rsi"),
            "macd_histogram": ((snap.get("technicals") or {}).get("macd")
                               or {}).get("histogram"),
            "ema": (snap.get("technicals") or {}).get("ema"),
        },
        "liquidity": {k: (snap.get("liquidity") or {}).get(k)
                      for k in ("score", "status")},
        "data_quality": (snap.get("data_quality") or {}).get("score"),
    }
    raw = llm.complete_json(_LLM_SYSTEM, json.dumps(payload, default=str),
                            model=model)
    if not isinstance(raw, dict):
        return {"p_up": 0.5, "expected_move_pct": 0.0, "verdict": ABSTAIN,
                "reason": "ABSTAIN:LLM_INVALID", "model": model}
    p = _f(raw.get("p_up"), -1.0)
    move = _f(raw.get("expected_move_pct"), None)
    verdict = str(raw.get("verdict") or "").upper()
    if not (0.0 <= p <= 1.0) or move is None or not (-50 <= move <= 50) \
            or verdict not in (BUY, HOLD, AVOID):
        return {"p_up": 0.5, "expected_move_pct": 0.0, "verdict": ABSTAIN,
                "reason": "ABSTAIN:LLM_INVALID", "model": model}
    # منطق اجباری: «خرید» با حرکت انتظاری منفی یا صفر تناقض است
    if verdict == BUY and move <= 0:
        verdict = HOLD
    if verdict == AVOID and move > 0:
        verdict = HOLD
    code = str(raw.get("reason") or "LLM")[:40].replace(" ", "_")
    return {"p_up": round(p, 4), "expected_move_pct": round(move, 4),
            "verdict": verdict, "reason": "LLM:%s" % code, "model": model}


# --------------------------------------------------------------- committee
def combine(votes: list, *, cost_pct: float, atr_pct: float,
            min_p_up: float = 0.60, min_upside_pct: float = 1.0,
            min_net_edge_pct: float = 0.2, min_votes: int = 3) -> dict:
    """Weighted vote + expected-value gate. Pure function (easy to test)."""
    vetoes = [v for v in votes if v.get("veto")]
    active = [v for v in votes
              if not v.get("veto") and v.get("verdict") != ABSTAIN]
    wsum = sum(max(0.0, _f(v.get("weight"), 1.0)) for v in active)
    if wsum <= 0:
        wsum = 1.0

    def avg(key, pick):
        return sum(max(0.0, _f(v.get("weight"), 1.0)) * pick(v)
                   for v in active) / wsum

    p_up = avg("p_up", lambda v: _f(v.get("p_up"), 0.5))
    upside = avg("up", lambda v: max(0.0, _f(v.get("expected_move_pct"))))
    downside = avg("down", lambda v: max(0.0, -_f(v.get("expected_move_pct"))))
    if downside <= 1e-9:
        # هیچ مدلی حرکت منفی پیش‌بینی نکرده: ریسک را از نوسان واقعی بگیر
        downside = max(atr_pct * 100 * 0.5, 0.1)
    min_upside = max(_f(min_upside_pct, 1.0), cost_pct * 1.2)
    net = p_up * upside - (1.0 - p_up) * downside - cost_pct

    buys = [v for v in active if v.get("verdict") == BUY]
    need = min(max(1, int(min_votes)), len(active)) if active else max(
        1, int(min_votes))
    blocked = []
    if vetoes:
        blocked.append(vetoes[0].get("reason") or "VETO")
    if len(buys) < need:
        blocked.append("NO_QUORUM(%d/%d)" % (len(buys), need))
    if p_up < min_p_up:
        blocked.append("P_UP_TOO_LOW")
    if upside < min_upside:
        blocked.append("UPSIDE_TOO_SMALL")
    if net < min_net_edge_pct:
        blocked.append("NEGATIVE_EXPECTED_VALUE")
    allow = bool(active) and not blocked
    consensus = BUY if allow else (AVOID if vetoes else HOLD)

    return {
        "engine": COMMITTEE_VERSION,
        "consensus": consensus,
        "allow_buy": allow,
        "blocked_by": blocked,
        "models": [
            {"model": v.get("model", "?"), "weight": _f(v.get("weight"), 1.0),
             "p_up": _f(v.get("p_up"), 0.5),
             "expected_move_pct": _f(v.get("expected_move_pct")),
             "verdict": v.get("verdict", ABSTAIN),
             "veto": bool(v.get("veto")),
             "reason": v.get("reason", "")}
            for v in votes],
        "votes_buy": len(buys),
        "votes_active": len(active),
        "votes_needed": need,
        "p_up": round(p_up, 4),
        "expected_up_pct": round(upside, 4),
        "expected_down_pct": round(downside, 4),
        "cost_pct": round(cost_pct, 4),
        "expected_net_edge_pct": round(net, 4),
        "min_upside_pct": round(min_upside, 4),
        "min_p_up": round(_f(min_p_up, 0.6), 4),
        "min_net_edge_pct": round(_f(min_net_edge_pct, 0.2), 4),
        "horizon_s": 0,
    }


class AICommittee:
    """Runs every voter for one candidate and returns the committee verdict."""

    def __init__(self, cfg: Config, llm=None):
        self.cfg = cfg
        self.llm = llm

    # ------------------------------------------------------------- helpers
    def enabled(self) -> bool:
        return bool(getattr(self.cfg.ai, "committee_enabled", True))

    def decide_mode(self) -> bool:
        mode = str(getattr(self.cfg.ai, "committee_mode", "gate") or "gate")
        return mode.strip().lower() == "decide"

    def llm_models(self) -> list:
        models = getattr(self.cfg.ai, "llm_models", None) or []
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",") if m.strip()]
        models = [str(m).strip() for m in models if str(m).strip()]
        if not models and getattr(self.cfg.ai, "llm_model", ""):
            models = [self.cfg.ai.llm_model]
        if self.llm is None:
            return []
        ready = getattr(self.llm, "ready", None)
        if callable(ready):
            if not ready():
                return []
        elif not getattr(self.llm, "available", lambda: False)():
            return []
        return models

    def _weights(self) -> dict:
        w = dict(DEFAULT_WEIGHTS)
        override = getattr(self.cfg.ai, "committee_weights", None) or {}
        if isinstance(override, dict):
            for k, v in override.items():
                w[str(k)] = _f(v, w.get(str(k), 1.0))
        return w

    def _thresholds(self) -> dict:
        return {
            "min_p_up": _f(getattr(self.cfg.ai, "committee_min_p_up", 0.60),
                           0.60),
            "min_upside_pct": _f(getattr(
                self.cfg.ai, "committee_min_upside_pct", 1.0), 1.0),
            "min_net_edge_pct": _f(getattr(
                self.cfg.ai, "committee_min_net_edge_pct", 0.2), 0.2),
            "min_votes": int(_f(getattr(
                self.cfg.ai, "committee_min_votes", 3), 3)),
        }

    # ------------------------------------------------------------ evaluate
    def evaluate(self, candidate: dict, market_snap: dict,
                 allow_llm: bool = True) -> dict:
        snap = market_snap or {}
        price = _price_of(snap)
        atr_pct = _atr_pct_of(snap)
        if atr_pct <= 0 and price > 0:
            atr_pct = 0.012
        slip = _f((snap.get("liquidity") or {}).get(
            "estimated_buy_slippage"), 0.001) or 0.001
        cost_pct = (2.0 * _f(getattr(self.cfg.fee, "taker_rate", 0.0025))
                    + slip) * 100.0
        horizon = int(_f(getattr(self.cfg.ai, "committee_horizon_s", 14400),
                         14400))
        ctx = {
            "price": price,
            "atr_pct": atr_pct,
            "cost_pct": cost_pct,
            "horizon_s": horizon,
            "atr_crash_pct": _f(getattr(
                self.cfg.risk, "risk_mode_regime_atr_crash_pct", 8.0), 8.0),
        }
        weights = self._weights()
        votes = []
        for name, fn in (("trend", vote_trend),
                         ("momentum", vote_momentum),
                         ("breakout", vote_breakout),
                         ("meanrev", vote_meanrev),
                         ("liquidity", vote_liquidity),
                         ("volatility", vote_volatility)):
            v = fn(candidate, snap, ctx)
            v["model"] = name
            v["weight"] = _f(weights.get(name), 1.0)
            votes.append(v)
        for model in (self.llm_models() if allow_llm else []):
            v = vote_llm(model, candidate, snap, ctx, self.llm)
            v["weight"] = _f(weights.get("llm"), LLM_WEIGHT)
            votes.append(v)

        verdict = combine(votes, cost_pct=cost_pct, atr_pct=atr_pct,
                          **self._thresholds())
        verdict["horizon_s"] = horizon
        verdict["symbol"] = candidate.get("symbol")
        verdict["price"] = price
        return verdict


# --------------------------------------------------------------- scoreboard
class CommitteeScoreboard:
    """Did the models' forecast actually happen?  Records + resolves BUYs."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.max_entries = 200

    def _horizon(self) -> int:
        return int(_f(getattr(self.cfg.ai, "committee_horizon_s", 14400),
                      14400))

    def record(self, storage, symbol: str, price: float, verdict: dict,
               now: Optional[datetime] = None) -> dict:
        if not symbol or _f(price) <= 0:
            return {}
        data = storage.kv_get(SCOREBOARD_KEY, {}) or {}
        entries = list(data.get("entries") or [])
        now = now or datetime.now(timezone.utc)
        entries.append({
            "symbol": symbol,
            "ts": now.isoformat(),
            "entry": _f(price),
            "p_up": _f(verdict.get("p_up"), 0.5),
            "expected_up_pct": _f(verdict.get("expected_up_pct")),
            "expected_net_edge_pct": _f(verdict.get("expected_net_edge_pct")),
            "horizon_s": self._horizon(),
            "last_pct": 0.0,
            "best_pct": 0.0,
            "resolved": False,
            "rose": None,
            "met_target": None,
        })
        data["entries"] = entries[-self.max_entries:]
        storage.kv_set(SCOREBOARD_KEY, data)
        return data["entries"][-1]

    def update(self, storage, prices: dict,
               now: Optional[datetime] = None) -> dict:
        data = storage.kv_get(SCOREBOARD_KEY, {}) or {}
        entries = list(data.get("entries") or [])
        if not entries:
            return self.summary(entries)
        now = now or datetime.now(timezone.utc)
        changed = False
        for e in entries:
            if e.get("resolved"):
                continue
            px = _f((prices or {}).get(e.get("symbol")))
            entry = _f(e.get("entry"))
            if px <= 0 or entry <= 0:
                continue
            pct = (px / entry - 1.0) * 100.0
            e["last_pct"] = round(pct, 4)
            e["best_pct"] = round(max(_f(e.get("best_pct")), pct), 4)
            changed = True
            try:
                started = datetime.fromisoformat(str(e.get("ts")))
            except (TypeError, ValueError):
                started = now
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if now - started >= timedelta(seconds=_f(e.get("horizon_s"),
                                                     self._horizon())):
                e["resolved"] = True
                e["rose"] = pct > 0
                e["met_target"] = pct >= _f(e.get("expected_up_pct"))
        if changed:
            data["entries"] = entries
            storage.kv_set(SCOREBOARD_KEY, data)
        return self.summary(entries)

    @staticmethod
    def summary(entries: list) -> dict:
        done = [e for e in entries if e.get("resolved")]
        n = len(done)
        return {
            "attempts": len(entries),
            "resolved": n,
            "win_rate": round(sum(1 for e in done if e.get("rose")) / n, 4)
            if n else None,
            "target_rate": round(sum(1 for e in done if e.get("met_target"))
                                 / n, 4) if n else None,
            "avg_pct": round(sum(_f(e.get("last_pct")) for e in done) / n, 4)
            if n else None,
        }
