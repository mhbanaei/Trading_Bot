"""Shared utilities — IDs (sections 84/85), UTC ISO-8601 (section 2),
Decimal helpers (section 3), checksums (section 57)."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any, Iterable, Iterator

getcontext().prec = 28


# ---------------------------------------------------------------- timestamps

def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with milliseconds and Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        "%03dZ" % (datetime.now(timezone.utc).microsecond // 1000)
    )


def utc_iso(dt: datetime) -> str:
    """Render a datetime as contract ISO-8601 (UTC, Z suffix)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + ("%03dZ" % (dt.microsecond // 1000))


def parse_iso(value: str) -> datetime:
    """Parse ISO-8601 (Z or offset) into an aware UTC datetime."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return utc_iso(datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc))


# ------------------------------------------------------------------- decimal

def d(x) -> Decimal:
    """Convert to Decimal exactly (str/int exact; float via repr)."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, str):
        return Decimal(x)
    if isinstance(x, bool):
        raise TypeError("bool is not a financial value")
    if isinstance(x, int):
        return Decimal(x)
    if isinstance(x, float):
        return Decimal(repr(x))
    raise TypeError("cannot convert %s to Decimal" % type(x).__name__)


def d_str(x, places: int | None = None) -> str:
    v = d(x)
    if places is not None:
        v = v.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(v, "f")


def quantize_to_step(value, step) -> Decimal:
    """Round value to the nearest multiple of step (market precision)."""
    v = d(value)
    s = d(step)
    if s <= 0:
        return v
    return (v / s).to_integral_value(rounding=ROUND_HALF_UP) * s


# ------------------------------------------------------------------ identity

def new_id(prefix: str) -> str:
    """Unique, stable, traceable, non-secret id (sections 84/85)."""
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


def new_event_id() -> str:
    return new_id("evt")


def new_error_id() -> str:
    return new_id("err")


# ----------------------------------------------------------------- integrity

def sha256_json(obj: Any) -> str:
    canonical = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


# -------------------------------------------------------------------- misc

def split_symbol(symbol: str) -> tuple:
    """BTCUSDT -> (BTC, USDT); BTCIRT -> (BTC, IRT)."""
    s = symbol.strip().upper()
    if s.endswith("USDT"):
        return s[:-4], "USDT"
    if s.endswith("IRT"):
        return s[:-3], "IRT"
    return s, "USDT"


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def chunked(seq: Iterable, n: int) -> Iterator[list]:
    buf = []
    for item in seq:
        buf.append(item)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def clamp01(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return max(0.0, min(1.0, float(x)))


# ------------------------------------------------------- market-name canonical
# Nobitex echoes COIN FULL NAMES ("Pyth Network", "Biconomy") in several
# endpoints while the bot's canonical symbols are the short tickers
# (PYTHIRT, BICOIRT). Every name that crosses the exchange boundary passes
# through canonical_symbol() so fills, orders, positions and exit plans
# always share ONE key (the registry symbol the UI displays).

_TICKER_ALIASES = {
    # exchange full name (uppercased, no spaces) -> canonical base ticker
    "PYTHNETWORK": "PYTH",
    "BICONOMY": "BICO",
    "SSVNETWORK": "SSV",
    "CURVEDAO": "CRV",
    "LIDODAO": "LDO",
    "ETHEREUMNAMESERVICE": "ENS",
    "HAMSTERKOMBAT": "HMSTR",
    "PANCAKESWAP": "CAKE",
    "TETHERGOLD": "XAUT",
    # the order/echo endpoints spell PAX Gold "PAXGold" while /market/stats
    # (and the bot's registry) uses the short "paxg" code
    "PAXGOLD": "PAXG",
    # Nobitex's wallet/order echo says "XEmpire" for the XIRT market.
    # Without this alias the bot invented a separate XEMPIREIRT row, so
    # the wallet's X balance had no live price/24h change and its P&L used
    # a different market's ticker.
    "XEMPIRE": "X",
    "HEDERAHASHGRAPH": "HBAR",
    "ADVENTUREGOLD": "AGLD",
    "JASMYCOIN": "JASMY",
    "RIPPLE": "XRP",
    "POLKADOT": "DOT",
    "FILECOIN": "FIL",
    "BITTENSOR": "TAO",
    "HYPERLIQUID": "HYPE",
    "DOGECOIN": "DOGE",
    "LITECOIN": "LTC",
    "AVALANCHE": "AVAX",
    "ALGORAND": "ALGO",
    "STELLAR": "XLM",
    "POLYGON": "POL",
    "NOTCOIN": "NOT",
    "JUST": "JST",
    "GRAPH": "GRT",
    "SONIC": "S",
    "ETHERFI": "ETHFI",
    "MOVEMENT": "MOVE",
    "STARKNET": "STRK",
    "ZKSYNC": "ZK",
    "SUCCINCT": "PROVE",
    "IO.NET": "IO",
    "IO": "IO",
    "USD.AI": "USDA",
    "CATIZEN": "CATI",
    "ESPRESSO": "ESP",
    "DOODLES": "DOOD",
    "LAYERZERO": "ZRO",
    "BITCOIN": "BTC",
    "ETHEREUM": "ETH",
    "SOLANA": "SOL",
    "CARDANO": "ADA",
    "CHAINLINK": "LINK",
    "UNISWAP": "UNI",
    "AVALANCHE2": "AVAX",
    "SHIBAINU": "SHIB",
    "THEOPENNETWORK": "TON",
    "TRON": "TRX",
    "NEAR": "NEAR",
    "COSMOS": "ATOM",
    "LITECOIN2": "LTC",
    "BINANCECOIN": "BNB",
    "BINANCEUSD": "BUSD",
    "GENIUSTERMINAL": "GENIUS",
    "DEFIAPP": "DEFIAPP",
    "FABRICPROTOCOL": "FABRIC",
    "NVIDIACORPSTOCK": "NVDA",
    "GOPLUSSECURITY": "GPS",
}

_canon_registry: dict = {}      # alias-ticker -> canonical symbol
_canon_loaded = False


def _load_canon_registry() -> None:
    """One-time load of the market registry so aliases resolve to symbols
    that actually exist (PYTHNETWORKIRT -> PYTHIRT when tradable)."""
    global _canon_loaded
    if _canon_loaded:
        return
    _canon_loaded = True
    try:
        from pathlib import Path as _P
        import json as _json
        p = _P(__file__).resolve().parent.parent / "data" / "markets" / \
            "market_registry.json"
        if p.exists():
            doc = _json.loads(p.read_text(encoding="utf-8"))
            mk = doc.get("markets") or {}
            # prefer tradable entries when a ticker maps to several markets
            for sym, entry in mk.items():
                base = sym[:-4] if sym.endswith("USDT") else \
                    (sym[:-3] if sym.endswith("IRT") else sym)
                prev = _canon_registry.get(base)
                if prev is None or (entry.get("tradable") and
                                    not prev.get("t")):
                    _canon_registry[base] = {"s": sym, "t": bool(
                        entry.get("tradable"))}
    except Exception:
        pass


def canonical_base(base: str) -> str:
    """Exchange full-name base ("PYTH NETWORK") -> canonical ticker
    ("PYTH") when known; anything already-canonical passes through."""
    b = (base or "").strip().upper().replace(" ", "")
    if not b:
        return b
    _load_canon_registry()
    alias = _TICKER_ALIASES.get(b)
    if alias:
        return alias
    # a full name that IS a valid registry base passes unchanged
    return b


def canonical_symbol(symbol: str) -> str:
    """Any market spelling -> the canonical bot symbol.

    "PYTH NETWORKIRT"/"PYTH NETWORK-RLS" -> "PYTHIRT";
    "BICONOMYIRT" -> "BICOIRT"; "pythirt" -> "PYTHIRT";
    already-canonical symbols pass through untouched.
    """
    s = (symbol or "").strip().upper().replace(" ", "")
    if not s:
        return s
    for suffix in ("USDT", "IRT"):
        if s.endswith(suffix):
            return canonical_base(s[:-len(suffix)]) + suffix
    return canonical_base(s)


def quote_of_market(market: str) -> str | None:
    """Money side of a market symbol: "USDT", "IRT" or None.

    The holdings box splits its two sections (تتری / تومانی) by THIS, and
    the wallet sync decides which market a coin belongs to by it — a coin
    bought with Tether must never be shown or filed inside the Toman
    section (and the reverse). USDTIRT is the Tether-priced-in-Toman
    market, so its money side is IRT.
    """
    s = (market or "").strip().upper().replace(" ", "")
    if s.endswith("USDT"):
        return "USDT"
    if s.endswith("IRT"):
        return "IRT"
    return None
