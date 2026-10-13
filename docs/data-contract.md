<!-- INSERTION TARGET: Add the following section to the main Agent prompt -->

# DATA CONTRACT & SCHEMA SPECIFICATION

## قرارداد رسمی داده‌ها و Schemaهای ربات هوشمند Nobitex

این بخش قرارداد رسمی داده میان تمام اجزای سیستم است.

تمام سرویس‌ها، ماژول‌ها و Agentها MUST بر اساس این قرارداد داده کار کنند.

هیچ ماژولی مجاز نیست Schema اختصاصی و ناسازگار با این قرارداد تولید کند.

---

# 1. اصول عمومی Data Contract

## 1.1 قوانین اجباری

تمام داده‌های سیستم MUST دارای موارد زیر باشند:

* `schema_version`
* `source`
* `created_at`
* `collected_at` یا timestamp مناسب
* `data_status`
* شناسه یکتا در صورت وجود Entity
* واحد اندازه‌گیری برای مقادیر حساس
* currency/quote asset برای قیمت‌ها و ارزش‌ها
* precision مناسب
* قابلیت تشخیص داده stale
* قابلیت trace کردن داده تا Snapshot اصلی

هیچ مقدار عددی حساس نباید بدون واحد ذخیره شود.

مثلاً:

```json
{
  "price": 68450.12,
  "quote_asset": "USDT"
}
```

نه:

```json
{
  "price": 68450.12
}
```

---

# 2. قوانین Timestamp

تمام timestampها MUST به UTC و ISO-8601 ذخیره شوند.

فرمت:

```text
2026-09-10T12:30:00.123Z
```

قوانین:

* زمان داخلی سیستم: UTC
* زمان API: همان مقدار اصلی API در صورت معتبر بودن
* زمان دریافت: `received_at`
* زمان ایجاد داده: `created_at`
* زمان Snapshot: `snapshot_at`
* timestamp بدون timezone ممنوع
* timestamp آینده غیرمنطقی باید reject شود
* داده stale باید علامت‌گذاری شود

---

# 3. Data Type Rules

## 3.1 Decimal

برای موارد مالی:

* price
* amount
* volume
* fee
* balance
* PnL
* cost
* notional

از `Decimal` یا representation دقیق decimal استفاده شود.

استفاده از floating-point برای محاسبات مالی داخلی ممنوع است.

---

## 3.2 Integer

برای:

* counts
* sequence
* levels
* durations برحسب millisecond در صورت نیاز

---

## 3.3 Boolean

برای flagها:

```json
true
false
```

از `"true"` و `"false"` به‌عنوان string استفاده نشود.

---

# 4. Asset Contract

Asset با Market متفاوت است.

مثال:

```text
BTC = Asset
BTCIRT = Market
BTCUSDT = Market
```

Schema:

```json
{
  "schema_version": "1.0",
  "asset": {
    "symbol": "BTC",
    "name": "Bitcoin",
    "asset_type": "CRYPTO",
    "decimals": 8,
    "status": "ACTIVE",
    "tradable": true,
    "deposit_enabled": true,
    "withdraw_enabled": true,
    "source": "nobitex",
    "updated_at": "2026-09-10T12:30:00Z"
  }
}
```

Fields:

```text
symbol
name
asset_type
decimals
status
tradable
deposit_enabled
withdraw_enabled
source
updated_at
```

---

# 5. Market Contract

Market نماینده یک trading pair است.

مثال:

```text
BTCIRT
BTCUSDT
ETHIRT
ETHUSDT
```

Schema:

```json
{
  "schema_version": "1.0",
  "market": {
    "symbol": "BTCIRT",
    "base_asset": "BTC",
    "quote_asset": "IRT",
    "status": "ACTIVE",
    "tradable": true,
    "price_precision": 0.1,
    "amount_precision": 0.00000001,
    "min_amount": 0.00001,
    "min_notional": null,
    "market_type": "SPOT",
    "updated_at": "2026-09-10T12:30:00Z"
  }
}
```

قوانین:

* Market بدون base asset معتبر نیست.
* Market بدون quote asset معتبر نیست.
* `base_asset != quote_asset`
* precision باید از منبع رسمی استخراج شود.
* limits نباید hardcode شوند اگر API اطلاعات معتبر ارائه می‌دهد.
* Market جدید باید Dynamic Discovery شود.
* Market حذف‌شده از Registry پاک نشود؛ فقط:

```json
"tradable": false
```

---

# 6. Market Registry Contract

فایل:

```text
data/markets/market_registry.json
```

ساختار:

```json
{
  "schema_version": "1.0",
  "registry_version": 42,
  "updated_at": "2026-09-10T12:30:00Z",
  "source": "nobitex",
  "markets": []
}
```

هر Market باید شامل:

```text
symbol
base_asset
quote_asset
status
tradable
precision
limits
created_at
updated_at
first_seen_at
last_seen_at
```

Registry باید append-compatible باشد.

---

# 7. Snapshot Contract

هر چرخه 30 دقیقه‌ای یک Snapshot مستقل ایجاد می‌کند.

نام:

```text
snapshot_000001.json
snapshot_000002.json
snapshot_000003.json
...
```

Schema:

```json
{
  "schema_version": "1.0",
  "snapshot_id": "snap_20260910_123000_000001",
  "cycle_id": "cycle_20260910_123000",
  "created_at": "2026-09-10T12:30:00Z",
  "collection_started_at": "2026-09-10T12:30:01Z",
  "collection_finished_at": "2026-09-10T12:30:18Z",
  "status": "COMPLETE",
  "source": "nobitex",
  "market_count": 184,
  "valid_market_count": 181,
  "data_quality_score": 0.97,
  "markets": [],
  "portfolio": {},
  "open_orders": [],
  "recent_trades": [],
  "system_state": {}
}
```

---

# 8. Snapshot Status

مقادیر مجاز:

```text
COLLECTING
PARTIAL
COMPLETE
INVALID
CORRUPTED
```

تنها Snapshotهای:

```text
COMPLETE
```

برای AI Analysis به‌صورت عادی قابل استفاده هستند.

Snapshotهای PARTIAL فقط در صورت رعایت threshold کیفیت قابل استفاده‌اند.

---

# 9. Snapshot Lineage

هر داده باید قابلیت trace شدن به Snapshot را داشته باشد.

حداقل:

```json
{
  "snapshot_id": "snap_...",
  "cycle_id": "cycle_..."
}
```

در صورت نیاز:

```text
source_timestamp
received_at
source_endpoint
source_sequence
```

---

# 10. Market Snapshot Schema

برای هر Market:

```json
{
  "symbol": "BTCUSDT",
  "base_asset": "BTC",
  "quote_asset": "USDT",

  "timestamp": "2026-09-10T12:30:00Z",
  "received_at": "2026-09-10T12:30:01Z",

  "last_price": 113245.12,

  "best_bid": 113240.00,
  "best_ask": 113250.00,

  "spread": 10.00,
  "spread_percent": 0.0088,

  "volume_24h": 1250.45,
  "quote_volume_24h": 141000000.0,

  "change_24h": 2.41,
  "high_24h": 114500.00,
  "low_24h": 109800.00,

  "orderbook": {},
  "trades": [],
  "ohlc": {},

  "liquidity": {},
  "volatility": {},

  "data_quality": {}
}
```

---

# 11. Order Book Contract

Schema:

```json
{
  "timestamp": "2026-09-10T12:30:00Z",
  "sequence": 18273921,

  "bids": [
    {
      "price": 113240.0,
      "amount": 0.15,
      "notional": 16986.0
    }
  ],

  "asks": [
    {
      "price": 113250.0,
      "amount": 0.12,
      "notional": 13590.0
    }
  ],

  "depth_levels": 50,

  "bid_volume": 125.5,
  "ask_volume": 118.3,

  "imbalance": 0.0295
}
```

قوانین:

* bids باید descending باشند.
* asks باید ascending باشند.
* best bid < best ask در orderbook سالم.
* price <= 0 ممنوع.
* amount <= 0 ممنوع.
* duplicate price level باید merge شود.
* orderbook ناقص باید `data_quality.status` مناسب داشته باشد.

---

# 12. Order Book Derived Features

سیستم MUST بتواند این موارد را محاسبه کند:

```text
best_bid
best_ask
spread
spread_percent
bid_volume
ask_volume
bid_ask_imbalance
depth_1pct
depth_2pct
depth_5pct
buy_slippage_estimate
sell_slippage_estimate
estimated_market_impact
liquidity_score
```

Orderbook به‌تنهایی نباید سیگنال BUY/SELL ایجاد کند.

---

# 13. OHLC Contract

ساختار:

```json
{
  "timeframe": "30m",
  "candles": [
    {
      "timestamp": "2026-09-10T12:00:00Z",
      "open": 112500.0,
      "high": 113400.0,
      "low": 112100.0,
      "close": 113245.0,
      "volume": 184.2
    }
  ]
}
```

Timeframeهای استاندارد:

```text
5m
15m
30m
1h
4h
1d
```

در صورت کمبود داده، timeframe نباید جعل یا extrapolate شود.

---

# 14. Recent Trades Contract

```json
{
  "trades": [
    {
      "trade_id": "123456",
      "timestamp": "2026-09-10T12:29:58Z",
      "price": 113240.0,
      "amount": 0.015,
      "side": "BUY",
      "notional": 1698.6
    }
  ]
}
```

`trade_id` در صورت ارائه توسط API باید preserve شود.

Duplicate trade نباید دوباره وارد Storage شود.

---

# 15. Liquidity Contract

```json
{
  "score": 0.91,
  "spread_score": 0.96,
  "depth_score": 0.88,
  "volume_score": 0.94,
  "slippage_score": 0.86,
  "market_impact_score": 0.89,

  "estimated_buy_slippage": 0.0012,
  "estimated_sell_slippage": 0.0014,

  "status": "GOOD"
}
```

Status:

```text
EXCELLENT
GOOD
MEDIUM
POOR
CRITICAL
UNKNOWN
```

اگر liquidity:

```text
CRITICAL
UNKNOWN
```

باشد، Market به‌صورت پیش‌فرض قابل معامله نیست.

---

# 16. Volatility Contract

```json
{
  "atr": 1245.2,
  "atr_percent": 0.011,
  "realized_volatility": 0.32,
  "historical_volatility": 0.29,
  "volatility_regime": "MEDIUM"
}
```

Regime:

```text
VERY_LOW
LOW
MEDIUM
HIGH
EXTREME
UNKNOWN
```

---

# 17. Technical Features Contract

```json
{
  "ema": {
    "9": 112900.0,
    "20": 112500.0,
    "50": 111800.0,
    "100": 109900.0,
    "200": 106500.0
  },

  "sma": {
    "20": 112400.0,
    "50": 111700.0,
    "200": 106300.0
  },

  "rsi": 64.2,

  "macd": {
    "value": 412.3,
    "signal": 351.2,
    "histogram": 61.1
  },

  "stochastic": {
    "k": 78.2,
    "d": 74.1
  },

  "roc": 3.2,

  "bollinger": {
    "upper": 114200.0,
    "middle": 112400.0,
    "lower": 110600.0,
    "bandwidth": 0.032
  },

  "obv": 182739123.0,

  "volume_ratio": 1.42
}
```

Indicatorهای بدون داده کافی MUST مقدار:

```text
null
```

داشته باشند و نباید صفر شوند.

---

# 18. Market Structure Contract

```json
{
  "trend": "UPTREND",

  "structure": {
    "higher_high": true,
    "higher_low": true,
    "lower_high": false,
    "lower_low": false
  },

  "support_levels": [
    111500.0,
    109800.0
  ],

  "resistance_levels": [
    114500.0,
    116200.0
  ],

  "breakout": {
    "detected": true,
    "direction": "UP",
    "strength": 0.82
  }
}
```

---

# 19. Market Regime Contract

مقادیر:

```text
TRENDING_UP
TRENDING_DOWN
RANGING
BREAKOUT
BREAKDOWN
HIGH_VOLATILITY
LOW_VOLATILITY
UNCERTAIN
```

مثال:

```json
{
  "regime": "TRENDING_UP",
  "confidence": 0.84,
  "detected_at": "2026-09-10T12:30:00Z"
}
```

---

# 20. Data Quality Contract

برای هر Market:

```json
{
  "status": "GOOD",
  "score": 0.98,

  "checks": {
    "price_valid": true,
    "timestamp_valid": true,
    "orderbook_valid": true,
    "ohlc_valid": true,
    "trades_valid": true,
    "fresh": true
  },

  "warnings": []
}
```

Status:

```text
GOOD
WARNING
STALE
INVALID
CRITICAL
```

---

# 21. Portfolio Snapshot Contract

```json
{
  "timestamp": "2026-09-10T12:30:00Z",

  "account": {
    "equity_irt": 1250000000.0,
    "equity_usdt": 18500.0,
    "available_irt": 350000000.0,
    "available_usdt": 4500.0
  },

  "assets": [
    {
      "asset": "BTC",
      "available": 0.125,
      "locked": 0.02,
      "total": 0.145,

      "quote_asset": "USDT",
      "current_price": 113245.0,
      "market_value": 16420.525,

      "portfolio_weight": 0.887,

      "average_entry_price": 107500.0,

      "unrealized_pnl": 833.125,
      "realized_pnl": 120.0
    }
  ]
}
```

قوانین:

* balance منفی بدون دلیل معتبر reject شود.
* total باید با available + locked سازگار باشد.
* ارزش Portfolio باید با price timestamp مشخص محاسبه شود.
* قیمت تخمینی باید source داشته باشد.

---

# 22. Portfolio Risk Contract

```json
{
  "total_equity": 18500.0,

  "exposure": {
    "BTC": 0.887,
    "ETH": 0.06,
    "USDT": 0.053
  },

  "cash_ratio": 0.053,

  "unrealized_pnl": 850.0,
  "realized_pnl": 1200.0,

  "drawdown": {
    "current": 0.034,
    "max": 0.082
  },

  "correlation_risk": 0.72,

  "risk_status": "NORMAL"
}
```

---

# 23. Open Order Contract

```json
{
  "order_id": "123456789",
  "client_order_id": "bot_20260910_123000_001",

  "market": "BTCUSDT",

  "side": "BUY",
  "type": "LIMIT",
  "execution": "LIMIT",

  "price": 112900.0,
  "amount": 0.01,

  "matched_amount": 0.004,
  "unmatched_amount": 0.006,

  "status": "PARTIALLY_FILLED",

  "fee": {
    "amount": 4.52,
    "asset": "USDT"
  },

  "created_at": "2026-09-10T12:25:00Z",
  "updated_at": "2026-09-10T12:30:00Z"
}
```

---

# 24. Order Status Enum

```text
PENDING
OPEN
PARTIALLY_FILLED
FILLED
CANCEL_REQUESTED
CANCELLED
REJECTED
EXPIRED
UNKNOWN
```

`UNKNOWN` یک وضعیت خطرناک است و MUST باعث جلوگیری از ایجاد order جدید روی همان exposure شود تا وضعیت مشخص شود.

---

# 25. Trade / Fill Contract

هر execution واقعی باید به‌صورت Fill ذخیره شود.

```json
{
  "trade_id": "987654",
  "order_id": "123456789",

  "market": "BTCUSDT",
  "side": "BUY",

  "price": 113000.0,
  "amount": 0.004,

  "gross_value": 452.0,

  "fee": {
    "amount": 0.452,
    "asset": "USDT"
  },

  "timestamp": "2026-09-10T12:30:05Z"
}
```

---

# 26. Historical Trade Contract

Historical tradeها باید immutable باشند.

```json
{
  "trade_id": "987654",
  "order_id": "123456789",
  "market": "BTCUSDT",
  "side": "BUY",
  "price": 113000.0,
  "amount": 0.004,
  "fee_amount": 0.452,
  "fee_asset": "USDT",
  "timestamp": "2026-09-10T12:30:05Z",
  "source": "nobitex"
}
```

---

# 27. Fee Contract

Fee نباید hardcode شود.

```json
{
  "fee_type": "TRADING",
  "side": "BUY",
  "rate": 0.0025,
  "asset": "USDT",
  "source": "NOBITEX_API",
  "effective_at": "2026-09-10T12:30:00Z"
}
```

سیستم باید بتواند feeهای متفاوت را در محاسبات لحاظ کند.

---

# 28. Trading Cost Contract

قبل از هر معامله:

```json
{
  "gross_expected_return": 0.018,

  "costs": {
    "buy_fee": 0.0025,
    "sell_fee": 0.0025,
    "spread": 0.0009,
    "slippage": 0.0018,
    "market_impact": 0.0012
  },

  "estimated_total_cost": 0.0089,

  "expected_net_return": 0.0091
}
```

قانون:

```text
Expected Net Return =
Expected Gross Return - Total Estimated Cost
```

اگر Net Return از threshold لازم کمتر باشد:

```text
NO_TRADE
```

---

# 29. AI Context Contract

LLM نباید Snapshot خام کامل را دریافت کند.

ساختار:

```json
{
  "context_version": "1.0",

  "snapshot_id": "snap_...",
  "cycle_id": "cycle_...",

  "account_summary": {},

  "market_candidates": [],

  "portfolio_risk": {},

  "active_orders": [],

  "recent_decisions": [],

  "historical_summary": {},

  "global_market_context": {},

  "system_constraints": {}
}
```

---

# 30. Candidate Market Contract

Quant Scanner قبل از AI باید Marketها را رتبه‌بندی کند.

```json
{
  "symbol": "BTCUSDT",

  "rank": 1,

  "score": 0.91,

  "signals": {
    "trend": 0.88,
    "momentum": 0.83,
    "liquidity": 0.95,
    "volatility": 0.72,
    "orderbook": 0.76,
    "risk": 0.81
  },

  "reasons": [
    "TREND_ALIGNMENT",
    "POSITIVE_MOMENTUM",
    "HIGH_LIQUIDITY"
  ]
}
```

AI فقط Top-N را دریافت کند.

N باید configurable باشد.

مثلاً:

```text
TOP 10
TOP 20
TOP 30
```

---

# 31. AI Decision Contract

AI MUST فقط JSON معتبر تولید کند.

Schema:

```json
{
  "decision_id": "dec_20260910_123000_BTCUSDT_001",

  "snapshot_id": "snap_20260910_123000_000001",
  "cycle_id": "cycle_20260910_123000",

  "symbol": "BTCUSDT",

  "action": "BUY",

  "confidence": 0.84,

  "time_horizon": "INTRADAY",

  "entry": {
    "price": 113000.0,
    "min": 112800.0,
    "max": 113300.0
  },

  "stop_loss": {
    "price": 110900.0,
    "reason": "ATR_SUPPORT"
  },

  "take_profit": [
    {
      "price": 115500.0,
      "percentage": 0.5
    },
    {
      "price": 117800.0,
      "percentage": 0.3
    },
    {
      "price": 120000.0,
      "percentage": 0.2
    }
  ],

  "position_size": {
    "amount": 0.012,
    "quote_value": 1356.0
  },

  "risk": {
    "max_loss": 25.0,
    "risk_reward": 2.7
  },

  "cost": {
    "estimated_total_cost": 0.0085,
    "expected_gross_return": 0.022,
    "expected_net_return": 0.0135
  },

  "invalidation_conditions": [
    "BREAK_SUPPORT",
    "SPREAD_EXPANSION",
    "LIQUIDITY_COLLAPSE"
  ],

  "reason_codes": [
    "TREND_UP",
    "MOMENTUM_POSITIVE",
    "LIQUIDITY_GOOD"
  ],

  "created_at": "2026-09-10T12:30:00Z",
  "expires_at": "2026-09-10T13:00:00Z"
}
```

---

# 32. AI Action Enum

فقط:

```text
BUY
SELL
HOLD
AVOID
```

معنای:

```text
BUY   = احتمال خرید وجود دارد ولی اجرای واقعی هنوز باید توسط Risk Engine تأیید شود.

SELL  = احتمال فروش وجود دارد ولی اجرای واقعی هنوز باید توسط Risk Engine تأیید شود.

HOLD  = موقعیت فعلی حفظ شود.

AVOID = وارد این Market نشو.
```

AI حق ارسال مستقیم Order ندارد.

---

# 33. Risk Decision Contract

Risk Engine باید تصمیم AI را validate کند.

```json
{
  "risk_decision_id": "risk_...",
  "decision_id": "dec_...",

  "status": "APPROVED",

  "checks": {
    "data_quality": true,
    "liquidity": true,
    "max_risk": true,
    "position_limit": true,
    "exposure_limit": true,
    "drawdown_limit": true,
    "fee_check": true,
    "slippage_check": true,
    "duplicate_order_check": true,
    "decision_freshness": true
  },

  "approved_position_size": 0.01,

  "approved_max_loss": 22.5,

  "approved_entry_range": {
    "min": 112900.0,
    "max": 113300.0
  },

  "rejection_reasons": [],

  "created_at": "2026-09-10T12:30:05Z"
}
```

---

# 34. Risk Rejection Contract

اگر Risk Engine رد کند:

```json
{
  "status": "REJECTED",

  "rejection_reasons": [
    "EXCESSIVE_PORTFOLIO_EXPOSURE",
    "INSUFFICIENT_NET_EXPECTED_RETURN"
  ]
}
```

AI مجاز به override نیست.

---

# 35. Position Size Contract

Position size باید مستقل از AI validate شود.

Inputs:

```text
account_equity
available_balance
risk_per_trade
stop_loss_distance
volatility
liquidity
existing_exposure
correlation
max_position_size
```

محاسبه پایه:

```text
Position Size =
Maximum Allowed Loss / Stop Loss Distance
```

سپس با liquidity، exposure و سایر محدودیت‌ها محدود شود.

---

# 36. Stop Loss Contract

Stop Loss باید شامل دلیل باشد.

```json
{
  "price": 110900.0,

  "method": "ATR_SUPPORT",

  "atr": 1245.2,

  "support": 111000.0,

  "distance_percent": 0.0189,

  "validated": true
}
```

روش‌ها:

```text
ATR
SUPPORT_RESISTANCE
MARKET_STRUCTURE
VOLATILITY
COMBINATION
```

Stop Loss تصادفی ممنوع.

---

# 37. Take Profit Contract

چند مرحله‌ای مجاز است:

```json
{
  "targets": [
    {
      "price": 115500.0,
      "close_percent": 50
    },
    {
      "price": 117800.0,
      "close_percent": 30
    },
    {
      "price": 120000.0,
      "close_percent": 20
    }
  ]
}
```

مجموع `close_percent` باید دقیقاً 100 باشد.

---

# 38. Execution Order Contract

پس از تأیید Risk:

```json
{
  "execution_id": "exec_...",
  "decision_id": "dec_...",
  "risk_decision_id": "risk_...",

  "market": "BTCUSDT",
  "side": "BUY",

  "order_type": "LIMIT",
  "execution_type": "LIMIT",

  "price": 113100.0,
  "amount": 0.01,

  "client_order_id": "bot_20260910_123000_001",

  "max_slippage": 0.001,

  "status": "READY"
}
```

---

# 39. Execution Status

```text
READY
SUBMITTING
SUBMITTED
OPEN
PARTIALLY_FILLED
FILLED
CANCEL_PENDING
CANCELLED
REJECTED
FAILED
UNKNOWN
```

---

# 40. Client Order ID Contract

هر Order باید:

```text
unique
deterministic
traceable
```

باشد.

فرمت پیشنهادی:

```text
bot_<cycle>_<symbol>_<sequence>
```

مثال:

```text
bot_20260910_1230_BTCUSDT_001
```

طول باید با محدودیت API مقصد سازگار باشد.

---

# 41. Decision Idempotency

هر تصمیم دارای:

```text
decision_id
snapshot_id
cycle_id
```

است.

قبل از Order:

```text
decision_id
+
symbol
+
side
+
recent orders
+
recent trades
```

بررسی شود.

اگر همان تصمیم قبلاً اجرا شده:

```text
DO_NOT_DUPLICATE
```

---

# 42. Decision Expiration

هر Decision:

```text
created_at
expires_at
```

دارد.

اگر:

```text
now > expires_at
```

باشد:

```text
EXPIRED
```

و نباید اجرا شود.

قبل از execution باید Market دوباره validate شود.

---

# 43. Revalidation Contract

بین AI Decision و Order Execution ممکن است قیمت تغییر کند.

بنابراین قبل از Execution:

```text
price
spread
orderbook
liquidity
balance
open orders
risk
```

دوباره بررسی شود.

اگر شرایط از محدوده مجاز خارج شده:

```text
REJECT_EXECUTION
```

---

# 44. Order Update Contract

هر تغییر Order باید Event ایجاد کند.

```json
{
  "event_id": "evt_...",
  "order_id": "123456789",

  "previous_status": "OPEN",
  "new_status": "PARTIALLY_FILLED",

  "timestamp": "2026-09-10T12:31:00Z",

  "matched_amount": 0.004,
  "remaining_amount": 0.006
}
```

Order history نباید overwrite شود.

---

# 45. PnL Contract

```json
{
  "market": "BTCUSDT",

  "realized_pnl": 125.5,
  "unrealized_pnl": 83.2,

  "fees": 14.2,
  "slippage_cost": 5.8,

  "gross_pnl": 142.0,
  "net_pnl": 119.7,

  "currency": "USDT",

  "calculated_at": "2026-09-10T12:30:00Z"
}
```

---

# 46. AI Performance Contract

```json
{
  "decision_id": "dec_...",

  "prediction": {
    "action": "BUY",
    "expected_return": 0.018
  },

  "outcome": {
    "actual_return": 0.012,
    "actual_pnl": 18.4
  },

  "metrics": {
    "direction_correct": true,
    "return_error": 0.006,
    "profitable": true
  }
}
```

Metrics:

```text
direction_accuracy
win_rate
profit_factor
average_return
average_loss
false_positive_rate
false_negative_rate
expected_vs_actual_error
performance_by_market
performance_by_regime
performance_by_confidence
```

---

# 47. Historical AI Summary Contract

به‌جای ارسال Raw History به AI:

```json
{
  "period": "30d",

  "total_decisions": 1840,

  "buy_accuracy": 0.61,
  "sell_accuracy": 0.58,

  "win_rate": 0.57,

  "avg_expected_return": 0.014,
  "avg_actual_return": 0.009,

  "best_regimes": [
    "TRENDING_UP",
    "BREAKOUT"
  ],

  "weak_regimes": [
    "RANGING",
    "HIGH_VOLATILITY"
  ]
}
```

---

# 48. Global Market Context Contract

در صورت استفاده:

```json
{
  "timestamp": "2026-09-10T12:30:00Z",

  "btc_dominance": null,

  "global_market": {
    "trend": "RISK_ON",
    "volatility": "MEDIUM"
  },

  "external_prices": [],

  "source": "external_market_data"
}
```

هر External Data باید source و timestamp داشته باشد.

---

# 49. System Health Contract

```json
{
  "timestamp": "2026-09-10T12:30:00Z",

  "api": {
    "status": "HEALTHY",
    "latency_ms": 120
  },

  "websocket": {
    "status": "HEALTHY",
    "last_message_at": "2026-09-10T12:29:59Z"
  },

  "data_collector": {
    "status": "RUNNING"
  },

  "ai": {
    "status": "HEALTHY",
    "last_analysis_at": "2026-09-10T12:00:00Z"
  },

  "risk_engine": {
    "status": "HEALTHY"
  },

  "execution_engine": {
    "status": "READY"
  }
}
```

---

# 50. Error Contract

تمام Errorها باید machine-readable باشند.

```json
{
  "error_id": "err_...",
  "timestamp": "2026-09-10T12:30:00Z",

  "component": "DATA_COLLECTOR",

  "code": "STALE_MARKET_DATA",

  "severity": "HIGH",

  "message": "Market data exceeded freshness threshold",

  "retryable": true,

  "safe_mode_required": true
}
```

---

# 51. Error Severity

```text
INFO
WARNING
HIGH
CRITICAL
FATAL
```

---

# 52. Event Contract

تمام Eventها:

```json
{
  "event_id": "evt_...",
  "event_type": "ORDER_FILLED",

  "timestamp": "2026-09-10T12:31:00Z",

  "source": "execution_engine",

  "cycle_id": "cycle_...",

  "payload": {}
}
```

Eventها immutable باشند.

---

# 53. Event Types

حداقل:

```text
SNAPSHOT_CREATED
SNAPSHOT_COMPLETED
SNAPSHOT_INVALID
MARKET_DISCOVERED
MARKET_DISABLED

AI_ANALYSIS_STARTED
AI_ANALYSIS_COMPLETED
AI_DECISION_CREATED

RISK_APPROVED
RISK_REJECTED

ORDER_SUBMITTED
ORDER_OPENED
ORDER_PARTIALLY_FILLED
ORDER_FILLED
ORDER_CANCELLED
ORDER_REJECTED

POSITION_OPENED
POSITION_UPDATED
POSITION_CLOSED

SAFE_MODE_ENTERED
SAFE_MODE_EXITED

SYSTEM_ERROR
```

---

# 54. Snapshot Cycle Contract

چرخه اصلی:

```text
T0
|
|-- Collect Snapshot N
|
T0 + 30m
|
|-- Analyze Snapshot N
|-- Collect Snapshot N+1
|
T0 + 60m
|
|-- Analyze Snapshot N+1
|-- Collect Snapshot N+2
|
...
```

Analysis و Collection نباید به‌صورت blocking به یکدیگر وابسته باشند.

---

# 55. Snapshot Naming Contract

فرمت:

```text
snapshot_<zero_padded_sequence>.json
```

مثال:

```text
snapshot_000001.json
snapshot_000002.json
snapshot_000003.json
```

Metadata داخلی باید timestamp واقعی را نیز داشته باشد.

---

# 56. Atomic Snapshot Write

Snapshot ابتدا:

```text
snapshot_000001.json.tmp
```

ذخیره شود.

پس از:

```text
schema validation
integrity validation
flush
fsync در صورت نیاز
```

به:

```text
snapshot_000001.json
```

rename شود.

Snapshot ناقص نباید با نام نهایی دیده شود.

---

# 57. Snapshot Integrity

هر Snapshot می‌تواند دارای checksum باشد:

```json
{
  "integrity": {
    "algorithm": "SHA-256",
    "checksum": "..."
  }
}
```

Checksum بعد از serialization نهایی محاسبه شود.

---

# 58. Data Freshness Contract

هر داده باید threshold freshness داشته باشد.

مثلاً:

```text
orderbook: seconds
ticker: seconds
OHLC: timeframe dependent
portfolio: short-lived
historical data: long-lived
```

Thresholdها configurable باشند.

اگر داده stale باشد:

```text
DO_NOT_TRADE
```

مگر اینکه Risk Policy صراحتاً شرایط دیگری تعیین کند.

---

# 59. Nullability Rules

`null` یعنی:

```text
داده موجود نیست / قابل محاسبه نیست
```

و با:

```text
0
```

تفاوت دارد.

مثلاً:

```json
{
  "rsi": null
}
```

یعنی RSI قابل محاسبه نیست.

این:

```json
{
  "rsi": 0
}
```

یک مقدار واقعی است و نباید برای missing data استفاده شود.

---

# 60. Enum Validation

تمام Enumها MUST validation شوند.

مقدار ناشناخته:

```text
UNKNOWN
```

یا rejection، بسته به Entity.

Agent نباید Enum جدیدی را خودش invent کند.

---

# 61. Schema Versioning

تمام Schemaها دارای:

```text
schema_version
```

هستند.

مثال:

```text
1.0
1.1
2.0
```

قوانین:

```text
MAJOR = breaking change
MINOR = backward-compatible addition
PATCH = validation/documentation fix
```

---

# 62. Backward Compatibility

Readerها باید تا حد امکان نسخه‌های قبلی را بخوانند.

برای تغییر breaking:

```text
migration layer
```

اجباری است.

هیچ فایل قدیمی بدون migration نباید silently reinterpret شود.

---

# 63. Schema Validation

هر ورودی باید قبل از استفاده validate شود.

Validation شامل:

```text
type
required fields
enum
range
precision
timestamp
cross-field consistency
business rules
source integrity
```

---

# 64. Cross-Field Validation

مثال:

```text
total_balance = available_balance + locked_balance
```

و:

```text
best_bid < best_ask
```

و:

```text
matched_amount + unmatched_amount = amount
```

و:

```text
take_profit percentages = 100
```

و:

```text
base_asset != quote_asset
```

---

# 65. Financial Validation

این موارد MUST کنترل شوند:

```text
price > 0
amount > 0
fee >= 0
balance >= 0
position_size > 0
stop_loss logically valid
take_profit logically valid
```

مقادیر NaN و Infinity ممنوع.

---

# 66. Data Provenance

برای داده‌های مهم:

```json
{
  "source": "nobitex",
  "source_type": "REST",
  "source_endpoint": "...",
  "received_at": "...",
  "source_timestamp": "..."
}
```

برای WebSocket:

```text
source_type = WEBSOCKET
```

---

# 67. REST + WebSocket Contract

معماری پیشنهادی:

```text
WebSocket
    |
    +--> Real-time Market Data
    |
    +--> Orderbook updates
    |
    +--> Candle updates

REST
    |
    +--> Initial synchronization
    +--> Recovery
    +--> Portfolio
    +--> Orders
    +--> Trades
    +--> Market discovery
```

WebSocket نباید تنها منبع حقیقت برای state حساس باشد.

در صورت desynchronization:

```text
REST RESYNC
```

انجام شود.

---

# 68. Data Reconciliation Contract

سیستم باید periodically بررسی کند:

```text
Local Portfolio
vs
Exchange Portfolio
```

و:

```text
Local Orders
vs
Exchange Orders
```

و:

```text
Local Trades
vs
Exchange Trades
```

هر اختلاف مهم:

```text
SAFE_MODE
```

را فعال می‌کند تا وضعیت بررسی شود.

---

# 69. AI Input Compression Contract

LLM نباید این موارد را بدون ضرورت دریافت کند:

```text
raw full orderbook
raw thousands of trades
all historical candles
all snapshots
duplicate indicators
duplicate portfolio data
```

Pipeline:

```text
RAW DATA
   ↓
NORMALIZATION
   ↓
FEATURE EXTRACTION
   ↓
SCORING
   ↓
RELEVANCE FILTER
   ↓
COMPRESSION
   ↓
AI CONTEXT
```

---

# 70. AI Context Priority

اولویت:

```text
1. Current portfolio
2. Current positions
3. Current market state
4. Risk state
5. Fees
6. Liquidity
7. Recent price action
8. Technical features
9. Market structure
10. Previous AI decisions
11. Historical statistics
12. Older context
```

---

# 71. Context Budget

سیستم MUST دارای:

```text
max_context_tokens
max_output_tokens
max_markets
max_candles_per_market
max_trades_per_market
```

باشد.

اگر context بیش از budget شد:

```text
low-priority data
```

حذف شود.

داده‌های critical حذف نشوند.

---

# 72. Prompt/Context Hash

هر AI request:

```json
{
  "prompt_version": "3.2",
  "context_hash": "sha256:...",
  "model": "configured_model",
  "created_at": "..."
}
```

داشته باشد.

این برای:

```text
reproducibility
audit
caching
debugging
performance analysis
```

است.

---

# 73. AI Output Validation

AI output MUST:

1. parse شود.
2. JSON syntax validate شود.
3. Schema validate شود.
4. enumها validate شوند.
5. numeric ranges validate شوند.
6. risk constraints validate شوند.
7. freshness validate شود.
8. سپس به Risk Engine ارسال شود.

LLM output مستقیماً به Execution Engine ارسال نشود.

---

# 74. AI Hallucination Protection

اگر AI:

```text
symbol نامعتبر
price غیرواقعی
market غیرموجود
negative amount
invalid stop loss
invalid TP
unknown asset
```

تولید کند:

```text
AI_OUTPUT_INVALID
```

و:

```text
NO_TRADE
```

---

# 75. Market Universe Contract

AI نباید Market خارج از:

```text
market_registry
```

را پیشنهاد کند.

اگر:

```text
symbol not found
```

باشد:

```text
REJECT
```

---

# 76. Portfolio-aware Decision Contract

AI Context باید بداند:

```text
current holdings
available balance
locked balance
existing exposure
open orders
risk
drawdown
```

تا خرید دارایی موجود یا فروش دارایی ناموجود را بدون دلیل پیشنهاد نکند.

اما Risk Engine مرجع نهایی موجودی است.

---

# 77. Sell Validation

قبل از SELL:

```text
available asset balance
locked balance
existing sell orders
minimum order size
current exposure
```

بررسی شود.

SELL بیش از available position ممنوع است.

---

# 78. Buy Validation

قبل از BUY:

```text
available quote balance
fees
minimum amount
minimum notional
open buy orders
portfolio exposure
risk limits
```

بررسی شود.

---

# 79. No-Trade Contract

سیستم باید بتواند صریحاً بگوید:

```json
{
  "action": "NO_TRADE",

  "reason_codes": [
    "LOW_LIQUIDITY",
    "HIGH_SPREAD",
    "INSUFFICIENT_EDGE",
    "STALE_DATA"
  ]
}
```

No-trade یک تصمیم معتبر است، نه failure.

---

# 80. Safe Mode Contract

```json
{
  "mode": "SAFE_MODE",

  "entered_at": "2026-09-10T12:30:00Z",

  "reasons": [
    "API_ERROR",
    "DATA_INCONSISTENCY"
  ],

  "allow_new_orders": false,

  "allow_cancel_orders": true,

  "allow_reconciliation": true
}
```

AI نمی‌تواند Safe Mode را خاموش کند.

---

# 81. Security Contract

هیچ‌یک از موارد زیر نباید در:

```text
snapshot
AI context
logs
events
errors
analytics
Git
database plaintext
```

ذخیره شوند:

```text
API key
private key
TOTP
password
authentication secret
withdrawal credential
```

Credentialها باید در Secret Manager یا encrypted secure storage قرار گیرند.

---

# 82. API Permission Contract

ربات معاملاتی باید حداقل permissionهای لازم را داشته باشد.

برای trading:

```text
READ
TRADE
```

مناسب است.

برای این ربات:

```text
WITHDRAW
```

نباید فعال شود.

---

# 83. Audit Contract

برای هر معامله باید chain زیر قابل بازسازی باشد:

```text
Snapshot
   ↓
Feature Set
   ↓
Candidate Ranking
   ↓
AI Context
   ↓
AI Decision
   ↓
Risk Decision
   ↓
Execution Decision
   ↓
Exchange Order
   ↓
Fills
   ↓
PnL
```

هیچ مرحله‌ای نباید بدون ID قابل trace باشد.

---

# 84. Required IDs

Entityهای مهم:

```text
snapshot_id
cycle_id
decision_id
risk_decision_id
execution_id
order_id
client_order_id
trade_id
event_id
error_id
context_hash
```

---

# 85. ID Rules

IDها باید:

```text
unique
stable
traceable
non-secret
```

باشند.

ID نباید شامل credential یا اطلاعات حساس باشد.

---

# 86. Storage Contract

ساختار پیشنهادی:

```text
data/
├── assets/
│   └── asset_registry.json
│
├── markets/
│   └── market_registry.json
│
├── snapshots/
│   ├── snapshot_000001.json
│   ├── snapshot_000002.json
│   └── ...
│
├── features/
│   └── ...
│
├── historical/
│   └── ...
│
├── ai/
│   ├── contexts/
│   ├── decisions/
│   └── performance/
│
├── risk/
│   └── decisions/
│
├── orders/
│   ├── active/
│   └── history/
│
├── trades/
│   └── ...
│
├── events/
│   └── ...
│
├── audit/
│   └── ...
│
└── archive/
```

---

# 87. JSON vs Database

JSON مناسب برای:

```text
registry
snapshot
AI decision
configuration
audit artifacts
```

Database مناسب برای:

```text
trades
orders
fills
features
historical candles
events
performance
```

برای نسخه اولیه:

```text
SQLite
```

و برای production:

```text
PostgreSQL
```

مناسب است.

---

# 88. Snapshot Retention

Snapshotهای raw قدیمی باید:

```text
compressed
archived
aggregated
```

شوند.

اما حذف اطلاعات مهم audit بدون retention policy ممنوع است.

---

# 89. Aggregation Contract

از Snapshotهای 30 دقیقه‌ای:

```text
30m
1h
4h
1d
1w
```

featureهای aggregate ساخته شوند.

این Aggregateها جایگزین raw data برای AI context می‌شوند.

---

# 90. Historical Candle Contract

```json
{
  "symbol": "BTCUSDT",
  "timeframe": "30m",

  "timestamp": "2026-09-10T12:00:00Z",

  "open": 112500.0,
  "high": 113400.0,
  "low": 112100.0,
  "close": 113245.0,

  "volume": 184.2,

  "source": "nobitex"
}
```

Unique key:

```text
(symbol, timeframe, timestamp)
```

---

# 91. Duplicate Prevention

برای Entityهای immutable:

```text
trade_id
event_id
candle(symbol,timeframe,timestamp)
```

unique constraint وجود داشته باشد.

برای Snapshot:

```text
snapshot_id
```

unique باشد.

---

# 92. Data Quality Score

Score در بازه:

```text
0.0 تا 1.0
```

باشد.

پیشنهاد:

```text
0.95 - 1.00 = Excellent
0.90 - 0.95 = Good
0.75 - 0.90 = Warning
0.50 - 0.75 = Poor
< 0.50      = Critical
```

Thresholdها configurable باشند.

---

# 93. Quality Gate

قبل از AI:

```text
if data_quality < minimum_ai_quality:
    NO_AI_ANALYSIS
```

قبل از Trading:

```text
if data_quality < minimum_trade_quality:
    NO_TRADE
```

---

# 94. Global Consistency Checks

سیستم باید بررسی کند:

```text
market exists
asset exists
price valid
timestamp valid
orderbook valid
balance valid
orders synchronized
trade history synchronized
fee available
risk available
```

هر failure مهم باید وارد Risk Gate شود.

---

# 95. Contract Testing

برای هر Schema باید تست وجود داشته باشد:

```text
valid payload
missing required field
wrong type
invalid enum
negative amount
invalid timestamp
duplicate entity
cross-field inconsistency
old schema
future schema
```

---

# 96. Version Migration

مثلاً:

```text
snapshot v1.0
      ↓
migration
      ↓
snapshot v2.0
```

Migration باید deterministic باشد.

اطلاعات حذف‌شده نباید بدون ثبت دلیل از بین بروند.

---

# 97. Schema Registry

سیستم باید یک Registry برای Schemaها داشته باشد:

```text
schemas/
├── asset.schema.json
├── market.schema.json
├── snapshot.schema.json
├── orderbook.schema.json
├── candle.schema.json
├── portfolio.schema.json
├── order.schema.json
├── trade.schema.json
├── ai_decision.schema.json
├── risk_decision.schema.json
├── execution.schema.json
├── event.schema.json
└── error.schema.json
```

---

# 98. Contract Ownership

مالک هر Contract:

```text
Asset/Market      → Market Data Layer
Snapshot          → Data Collector
Features          → Feature Engine
Portfolio         → Account Data Layer
AI Decision       → AI Layer
Risk Decision     → Risk Engine
Execution         → Execution Engine
Order/Trade       → Exchange Integration
Event             → Event/Audit Layer
```

هیچ AI Agentی مالک Contractهای Risk یا Execution نیست.

---

# 99. Contract Change Policy

هر تغییر Schema باید:

```text
version increment
migration assessment
validation update
tests update
documentation update
```

داشته باشد.

Breaking change بدون version bump ممنوع است.

---

# 100. Golden Rule

در تمام سیستم این ترتیب باید رعایت شود:

```text
RAW DATA
   ↓
VALIDATION
   ↓
NORMALIZATION
   ↓
SNAPSHOT
   ↓
FEATURE EXTRACTION
   ↓
QUALITY GATE
   ↓
QUANT SCANNER
   ↓
RELEVANCE FILTER
   ↓
AI CONTEXT
   ↓
AI DECISION
   ↓
AI OUTPUT VALIDATION
   ↓
RISK ENGINE
   ↓
REVALIDATION
   ↓
EXECUTION ENGINE
   ↓
EXCHANGE
   ↓
ORDER/FILL
   ↓
RECONCILIATION
   ↓
PNL
   ↓
PERFORMANCE
   ↓
HISTORICAL SUMMARY
```

هیچ مرحله‌ای نباید دور زده شود.

---

# 101. Absolute Data Safety Rules

اگر یکی از موارد زیر رخ دهد:

```text
schema validation failure
data corruption
stale critical data
unknown market
unknown order status
portfolio mismatch
exchange/local reconciliation failure
invalid AI output
risk calculation failure
fee uncertainty
liquidity collapse
extreme spread
authentication failure
API inconsistency
WebSocket desynchronization
```

سیستم MUST:

```text
NO NEW TRADE
```

را اجرا کند.

در شرایط شدید:

```text
SAFE_MODE
```

فعال شود.

---

# 102. Final Data Contract Principle

داده باید:

```text
Correct
Complete enough
Fresh
Traceable
Versioned
Validated
Deterministic
Auditable
Minimal for AI
Safe
```

باشد.

و اصل نهایی:

```text
AI may interpret data.
AI may recommend an action.
AI may NOT redefine the data contract.
AI may NOT bypass validation.
AI may NOT bypass risk controls.
AI may NOT directly execute trades.
AI may NOT override security controls.
```

مرجع نهایی:

```text
Data Contract
    >
Validation
    >
Risk Engine
    >
Execution Rules
    >
AI Recommendation
```

و در تمام شرایط:

```text
IF UNCERTAIN → NO TRADE
```

<!-- END INSERTION -->
