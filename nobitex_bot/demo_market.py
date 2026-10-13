"""Demo market simulator — deterministic (seeded) synthetic exchange.

Generates realistic price paths (GBM with regime shifts), order books with
realistic microstructure (spread, depth imbalance), recent trades, and
candles so the full pipeline runs without network access or credentials.

All numbers are Decimal-exact where it matters; float64 internally is fine
because this is *simulated* data that then goes through the same validation
as real exchange data.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from .exchange import ExchangeAdapter
from .utils import d, ms_to_iso, utc_now_iso


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class DemoMarket(ExchangeAdapter):
    name = "demo"

    def __init__(self, cfg, events=None):
        self.cfg = cfg
        self.events = events
        rng = random.Random(cfg.demo_seed)
        self._rng = rng
        self._t0 = _now_ms()
        self.step = 0

        self.markets_list = []
        for sym in cfg.demo_base_prices.keys():
            base = sym[:-4] if sym.endswith("USDT") else sym[:-3]
            quote = "USDT" if sym.endswith("USDT") else "IRT"
            self.markets_list.append({
                "symbol": sym,
                "base_asset": base,
                "quote_asset": quote,
                "status": "ACTIVE",
                "tradable": True,
                "price_precision": self._precision_of(cfg.demo_base_prices[sym]),
                "amount_precision": "0.00000001",
                "min_amount": "0.0001",
                "min_notional": "1.0",
                "market_type": "SPOT",
                "source": self.name,
                "updated_at": utc_now_iso(),
            })
        # BTCUSDT trends up, ETHUSDT ranges, DOGE dips — regime variety.
        # Drift is per-minute; ~+2%/day for a clear but not absurd trend.
        self._drift = {
            "BTCUSDT": 0.000014, "ETHUSDT": 0.000000, "ADAUSDT": 0.000008,
            "SOLUSDT": 0.000010, "DOGEUSDT": -0.000012, "SHIBIRT": 0.000006,
            "BTCIRT": 0.000012, "USDTIRT": 0.000000,
        }
        self._vol = {sym: 0.006 for sym in cfg.demo_base_prices}
        self._vol["BTCUSDT"] = 0.004
        self._vol["SHIBIRT"] = 0.012
        # Deterministic price walk cached per minute, from genesis (7d back)
        self._genesis_ms = self._t0 - 7 * 24 * 3600 * 1000
        self._walk_rng = {}
        self._walk = {}  # symbol -> list of per-minute prices
        self._live = {}  # symbol -> live tick price (moves every poll)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _precision_of(price: float) -> str:
        if price >= 100000:
            return "1"
        if price >= 1000:
            return "0.01"
        if price >= 1:
            return "0.0001"
        if price >= 0.01:
            return "0.000001"
        return "0.00000001"

    def _price_at(self, symbol: str, ms: int) -> float:
        """Deterministic per-minute price (cached GBM walk from genesis)."""
        n = max(0, int((ms - self._genesis_ms) / 60000))
        walk = self._walk.get(symbol)
        if walk is None:
            walk = [float(self.cfg.demo_base_prices[symbol])]
            self._walk[symbol] = walk
            self._walk_rng[symbol] = random.Random(
                hash((self.cfg.demo_seed, symbol)) & 0xFFFFFFFF)
        rng = self._walk_rng[symbol]
        drift = self._drift.get(symbol, 0.000005)
        vol = self._vol.get(symbol, 0.006)
        # historical path is damped (x0.25) so the 7-day cumulative walk
        # stays near the configured base price; the live tick layer adds
        # short-term movement on top
        vol_hist = vol * 0.25
        while len(walk) <= n:
            u1 = rng.random()
            u2 = rng.random()
            z = math.sqrt(-2 * math.log(u1 + 1e-12)) * math.cos(2 * math.pi * u2)
            walk.append(max(1e-12, walk[-1] * (1 + drift + vol_hist * z)))
        return walk[n]

    def _tick(self, symbol: str) -> dict:
        ms = _now_ms()
        base = self._price_at(symbol, ms)
        # live tick layer: every poll moves the price slightly and mean-
        # reverts toward the minute-walk anchor, so back-to-back cycles
        # (tests, fast demo) still see a living market
        live = self._live.get(symbol)
        if live is None:
            live = base
        live *= (1 + self._rng.gauss(0, 0.004))
        live += (base - live) * 0.05
        self._live[symbol] = live
        return {"symbol": symbol, "price": live, "ms": ms}

    # ------------------------------------------------------------- markets
    def markets(self) -> list:
        return list(self.markets_list)

    # -------------------------------------------------------------- ticker
    def ticker(self, symbol: str) -> dict:
        t = self._tick(symbol)
        price = t["price"]
        rng = self._rng
        spread_frac = rng.uniform(0.0002, 0.0009)
        half = price * spread_frac / 2
        bid = price - half
        ask = price + half
        day_open = self._price_at(symbol, t["ms"] - 24 * 3600 * 1000)
        chg = (price / day_open - 1) * 100 if day_open else 0.0
        high = max(price, day_open) * (1 + rng.uniform(0.001, 0.01))
        low = min(price, day_open) * (1 - rng.uniform(0.001, 0.01))
        vol_base = rng.uniform(500, 5000) / max(price, 1e-9) * 100
        return {
            "symbol": symbol,
            "last_price": price,
            "best_bid": bid,
            "best_ask": ask,
            "spread": ask - bid,
            "spread_percent": spread_frac * 100,
            "volume_24h": vol_base,
            "quote_volume_24h": vol_base * price,
            "change_24h": chg,
            "high_24h": high,
            "low_24h": low,
            "timestamp": ms_to_iso(t["ms"]),
            "received_at": utc_now_iso(),
        }

    # ----------------------------------------------------------- orderbook
    def orderbook(self, symbol: str, depth: int = 20) -> dict:
        t = self._tick(symbol)
        price = t["price"]
        rng = self._rng
        spread_frac = rng.uniform(0.0002, 0.0008)
        levels = max(5, depth)
        bids, asks = [], []
        bid_p = price * (1 - spread_frac / 2)
        ask_p = price * (1 + spread_frac / 2)
        step = price * 0.0004
        for i in range(levels):
            b_amt = rng.uniform(0.02, 2.5) * (1 + i * 0.15)
            a_amt = rng.uniform(0.02, 2.5) * (1 + i * 0.15)
            bids.append({
                "price": round(bid_p, 8),
                "amount": round(b_amt, 8),
                "notional": round(bid_p * b_amt, 2),
            })
            asks.append({
                "price": round(ask_p, 8),
                "amount": round(a_amt, 8),
                "notional": round(ask_p * a_amt, 2),
            })
            bid_p -= step * (1 + i * 0.2)
            ask_p += step * (1 + i * 0.2)
        bid_vol = sum(b["amount"] for b in bids)
        ask_vol = sum(a["amount"] for a in asks)
        return {
            "symbol": symbol,
            "timestamp": ms_to_iso(t["ms"]),
            "sequence": int(t["ms"]),
            "bids": bids,
            "asks": asks,
            "depth_levels": levels,
            "bid_volume": bid_vol,
            "ask_volume": ask_vol,
            "imbalance": (bid_vol - ask_vol) / max(bid_vol + ask_vol, 1e-9),
        }

    # ------------------------------------------------------------- candles
    def candles(self, symbol: str, timeframe: str = "1m", limit: int = 120) -> list:
        """Synthetic OHLC. Timeframe minutes parsed from string like 1m/5m/30m."""
        tf_min = self._tf_minutes(timeframe)
        now = _now_ms()
        bucket = tf_min * 60 * 1000
        out = []
        for i in range(limit, 0, -1):
            end = now - (i - 1) * bucket
            start = end - bucket
            o = self._price_at(symbol, start)
            c = self._price_at(symbol, end)
            hi = max(o, c) * (1 + self._rng.uniform(0.0005, 0.003))
            lo = min(o, c) * (1 - self._rng.uniform(0.0005, 0.003))
            v = self._rng.uniform(50, 900) / max(c, 1e-9) * 10
            out.append({
                "timestamp": ms_to_iso(start),
                "open": round(o, 8), "high": round(hi, 8),
                "low": round(lo, 8), "close": round(c, 8),
                "volume": round(v, 8),
            })
        return out

    @staticmethod
    def _tf_minutes(tf: str) -> int:
        table = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        return table.get(tf, 1)

    # -------------------------------------------------------------- trades
    def trades(self, symbol: str, limit: int = 30) -> list:
        t = self._tick(symbol)
        price = t["price"]
        out = []
        for i in range(limit):
            ms = t["ms"] - i * int(2000 + self._rng.random() * 3000)
            side = "BUY" if self._rng.random() > 0.5 else "SELL"
            amt = self._rng.uniform(0.001, 0.5) / max(price, 1e-9) * 50
            out.append({
                "trade_id": "%s-%d" % (symbol, ms),
                "timestamp": ms_to_iso(ms),
                "price": round(price * (1 + self._rng.gauss(0, 0.0004)), 8),
                "amount": round(amt, 8),
                "side": side,
                "notional": round(amt * price, 2),
            })
        return out

    # ------------------------------------------------------------ portfolio
    def portfolio(self) -> dict:
        return {
            "timestamp": utc_now_iso(),
            "account": {
                "equity_irt": 0.0,
                "equity_usdt": 10000.0,
                "available_irt": 0.0,
                "available_usdt": 10000.0,
            },
            "assets": [
                {"asset": "USDT", "available": "10000.0", "locked": "0.0",
                 "total": "10000.0", "quote_asset": "USDT",
                 "current_price": 1.0, "market_value": "10000.0",
                 "portfolio_weight": 1.0},
            ],
        }

    # -------------------------------------------------------------- orders
    def place_limit_order(self, market: str, side: str, price, amount,
                          client_order_id: str) -> dict:
        ms = _now_ms()
        return {
            "order_id": "demo-%d-%s" % (ms, client_order_id[-8:]),
            "client_order_id": client_order_id,
            "market": market,
            "side": side,
            "order_type": "LIMIT",
            "price": str(price),
            "amount": str(amount),
            "matched_amount": "0",
            "unmatched_amount": str(amount),
            "status": "OPEN",
            "created_at": ms_to_iso(ms),
            "updated_at": ms_to_iso(ms),
        }

    def open_orders(self, market=None):
        return []

    def cancel_order(self, order_id: str) -> dict:
        return {"order_id": order_id, "status": "CANCELLED"}

    def health(self) -> dict:
        return {"api": {"status": "HEALTHY", "latency_ms": 1},
                "websocket": {"status": "SIMULATED", "last_message_at": utc_now_iso()}}
