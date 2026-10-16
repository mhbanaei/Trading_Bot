"""Exchange adapter interface — section 67.

REST for initial sync/recovery/portfolio/orders/trades/discovery;
WebSocket would push real-time data (not needed in demo; the demo adapter
simulates a live stream internally).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class ExchangeAdapter(ABC):
    """Minimal synchronous adapter surface used by the collector/broker."""

    name: str = "base"

    @abstractmethod
    def markets(self) -> list:
        """Discover markets (SS5/SS6): symbol, base_asset, quote_asset,
        status, tradable, precision, limits."""

    @abstractmethod
    def ticker(self, symbol: str) -> dict:
        """Last price / 24h stats with timestamp + received_at."""

    @abstractmethod
    def orderbook(self, symbol: str, depth: int = 20) -> dict:
        """ bids descending, asks ascending (SS11)."""

    @abstractmethod
    def candles(self, symbol: str, timeframe: str, limit: int) -> list:
        """OHLC list oldest-first (SS13)."""

    @abstractmethod
    def trades(self, symbol: str, limit: int) -> list:
        """Recent trades with trade_id preserved (SS14)."""

    @abstractmethod
    def portfolio(self) -> dict:
        """Balances per SS21."""

    @abstractmethod
    def place_limit_order(self, market: str, side: str, price, amount,
                          client_order_id: str) -> dict:
        """Returns {order_id, status, price, amount, matched_amount...}."""

    @abstractmethod
    def open_orders(self, market: Optional[str] = None) -> list:
        """Open/partially-filled orders (SS23)."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict:
        """Best-effort cancel."""

    @abstractmethod
    def health(self) -> dict:
        """SS49 system health of the API connection."""

    def all_tickers(self) -> dict:
        """Optional: ONE index of EVERY market the exchange lists.

        {SYMBOL: ticker} in the bot's canonical symbol space. The engine's
        live market universe (universe.py) uses this to price holdings and
        scan the WHOLE exchange without depending on a snapshot file. The
        default here sweeps markets()+ticker() (correct everywhere, just
        more calls); adapters whose API returns all tickers in a single
        request (Nobitex /market/stats) override it for speed.
        """
        out = {}
        try:
            markets = self.markets()
        except Exception:
            return out
        for m in markets or []:
            sym = (m or {}).get("symbol")
            if not sym:
                continue
            try:
                t = self.ticker(sym)
            except Exception:
                continue
            if t and t.get("last_price"):
                out[sym] = {
                    **t,
                    "base_asset": (m or {}).get("base_asset"),
                    "quote_asset": (m or {}).get("quote_asset"),
                    "tradable": bool((m or {}).get("tradable", True)),
                }
        return out

    def convert_spot(self, coin: str, rls_amount) -> dict:
        """Optional: convert a spot coin balance into rials through the
        exchange's convert market (panel/balance/convert). Adapters that
        support it override this; the default reports unsupported."""
        raise NotImplementedError(
            "این صرافی از تبدیل مستقیم (convert) پشتیبانی نمی‌کند")
