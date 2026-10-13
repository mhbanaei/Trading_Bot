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

    def convert_spot(self, coin: str, rls_amount) -> dict:
        """Optional: convert a spot coin balance into rials through the
        exchange's convert market (panel/balance/convert). Adapters that
        support it override this; the default reports unsupported."""
        raise NotImplementedError(
            "این صرافی از تبدیل مستقیم (convert) پشتیبانی نمی‌کند")
