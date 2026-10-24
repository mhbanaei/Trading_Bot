"""Central configuration — nothing hardcoded (contract SS27, SS58, SS71).

All knobs live here; a config.json at the project root overrides defaults.
Fees, freshness thresholds, quality gates, AI top-N, context budgets:
everything is configurable, per contract.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"


@dataclass
class EngineCfg:
    cycle_seconds: int = 60            # demo cadence (contract nominates 30m)
    analyze_every: int = 1             # run AI+risk every N cycles
    max_cycles: int = 0                # 0 = unlimited
    server_host: str = "127.0.0.1"
    server_port: int = 8787
    data_dir: str = "data"
    max_markets: int = 30              # SS71 context budget: top-N by volume
    max_snapshots: int = 200           # retention cap for data/markets/snapshots
    events_retention_mb: float = 20.0  # cap for events/events.jsonl
    # live market universe (universe.py): the WHOLE exchange catalog in one
    # cached public call, independent of the snapshot. Held coins, exits,
    # manual orders and the «همه بازارها» table read prices from here, so a
    # coin outside the cycle's top-N universe still has a working stop-loss.
    live_universe: bool = True
    universe_ttl_s: float = 20.0       # cache TTL for that catalog


@dataclass
class ExchangeCfg:
    mode: str = "demo"                 # demo | nobitex
    demo_seed: int = 42
    demo_base_prices: dict = field(default_factory=lambda: {
        "BTCUSDT": 113000.0, "ETHUSDT": 4200.0, "ADAUSDT": 0.62,
        "SOLUSDT": 210.0, "DOGEUSDT": 0.16, "SHIBIRT": 0.00085,
        "BTCIRT": 9950000000.0, "USDTIRT": 99500.0,
    })
    demo_quote_usdt: dict = field(default_factory=lambda: {
        "BTCUSDT": "USDT", "ETHUSDT": "USDT", "ADAUSDT": "USDT",
        "SOLUSDT": "USDT", "DOGEUSDT": "USDT", "SHIBIRT": "IRT",
        "BTCIRT": "IRT", "USDTIRT": "IRT",
    })
    demo_quote_base: dict = field(default_factory=lambda: {
        "SHIBIRT": "IRT", "BTCIRT": "IRT", "USDTIRT": "IRT",
    })
    nobitex_base_url: str = "https://apiv2.nobitex.ir"
    nobitex_api_key: str = ""          # legacy; token login is preferred
    # --- real-account login (docs: /auth/login/ with captcha="api" + X-TOTP)
    nobitex_username: str = ""
    nobitex_password: str = ""
    nobitex_totp_secret: str = ""      # base32 2FA secret (Google Authenticator)
    nobitex_remember: str = "yes"      # yes -> 30-day token, no -> 4h token
    # --- live-account behaviour
    # live_trading: real orders on the real account once logged in. Default
    # TRUE per product spec: after login the bot trades the real account
    # (set false for watch-only paper mode).
    live_trading: bool = True
    live_by_default: bool = True       # after login, trade real immediately
    wallet_sync: bool = True           # mirror real spot balances into the engine
    prefer_usdt_markets: bool = True   # invest across USDT-quoted markets
    include_irt_markets: bool = True   # also allow IRT-quoted (BTCIRT...)
    # انتخاب بازار معاملات (quote currency) — سه‌حالته و مستقل از دو فیلد
    # بولی قدیمی بالا: "both" = هم تتری هم تومانی، "usdt" = فقط بازارهای
    # USDT، "irt" = فقط بازارهای IRT (تومان). مقدار خالی/نامعتبر =
    # fallback به دو فیلد قدیمی (prefer_usdt_markets/include_irt_markets)
    # تا رفتار قبلی کاربران حفظ شود. UI این گزینه را به‌صورت یک دراپ‌داون
    # + جفت checkbox نمایش می‌دهد.
    trade_quote_markets: str = "both"
    # نحوهٔ تعامل دو بازار در حالت «هر دو» (Both) — این تنظیم **مستقل** از
    # انتخاب بازار است و همان چیزی است که پنجرهٔ Popup انتخاب Both ثبت
    # می‌کند. موتور هرگز نباید از «Both روشن است» نتیجه بگیرد که انتقال
    # دارایی مجاز است؛ تنها همین فیلد مجوز جابه‌جایی می‌دهد:
    #   "linked"      = تعامل بین بازارها: ربات می‌تواند در صورت نیاز
    #                   موجودی/دارایی را بین بخش‌های IRT و USDT تبدیل
    #                   یا جابه‌جا کند (و خرید تومانی از موجودی تتری
    #                   هم تغذیه شود).
    #   "independent" = بازارهای مستقل: هر بازار فقط از موجودی خودش خرید
    #                   می‌کند و هیچ انتقال/تبدیل/جابه‌جایی بین دو بخش
    #                   انجام نمی‌شود (حتی وقتی Both فعال است).
    # مقدار خالی/نامعتبر → "independent" (fail-safe: هیچ پولی جابه‌جا
    # نمی‌شود).
    trade_market_interaction: str = "independent"
    min_quote_volume_usdt: float = 50000.0  # 24h quote-volume floor (USDT-eq)
    # per-order notional floor in USDT-equivalent, converted at the live
    # USDT-IRT price for IRT markets (0.44 USDT ≈ 100,000 Toman at
    # ~229k Rial/USDT — Nobitex's historical global minimum order value;
    # tune if Nobitex changes it). The dashboard shows the live Toman
    # equivalent in the settings tab.
    # NOTE (verified live): Nobitex rejects any order below 0.5 USDT with
    # «SmallOrder: Order value is limited to above 0.5.» — a smaller floor
    # here means the bot SENDS orders the exchange always rejects.
    min_order_usdt: float = 0.5
    rls_to_irt_divisor: float = 10.0   # wallet rls balances are in Rial
    rest_timeout: float = 12.0
    orderbook_depth: int = 20
    book_levels: int = 12


@dataclass
class FeeCfg:
    maker_rate: float = 0.0025
    taker_rate: float = 0.0025
    source: str = "CONFIG"             # contract SS27: source must be declared


@dataclass
class QualityCfg:
    min_ai_quality: float = 0.60       # SS93 quality gates
    min_trade_quality: float = 0.75
    ticker_freshness_s: int = 90       # SS58 data freshness
    orderbook_freshness_s: int = 90
    max_stale_before_no_ai: int = 120


@dataclass
class RiskCfg:
    max_risk_per_trade: float = 0.01   # fraction of equity at stop loss
    max_position_fraction: float = 0.15
    max_total_exposure: float = 0.60
    max_drawdown: float = 0.15
    # honest fee-aware edge floor (SS28): a BUY must expect to clear
    # fees BOTH ways (2 x 0.0025 taker) + slippage, with real headroom.
    # The old 0.0025 floor let zero-edge trades through: expected net of
    # exactly +0.25% vs ~+0.55% round-trip cost = guaranteed slow loss.
    min_net_expected_return: float = 0.006   # SS28 threshold
    max_open_positions: int = 8
    max_slippage: float = 0.001        # SS38
    decision_ttl_s: int = 1800         # SS42 expires_at
    # SS41 per-market BUY cooldown. Old 900s let the bot re-fire on the
    # same winner up to 6x/hour; each re-entry pays the full 0.5%
    # round-trip fee, so churn ATE the edge. 1800s keeps persistence but
    # halves the fee bleed.
    buy_cooldown_s: int = 1800
    revalidate_price_tolerance: float = 0.004  # SS43
    # minimum stop distance as a FRACTION of entry (0.012 = 1.2%). The stop
    # is the LARGER of 1.5×ATR and this percent — so quiet markets get a
    # realistic stop instead of a dust-tight one (spread would hit it).
    min_stop_distance_pct: float = 0.012
    # allow the bot to ADD free cash to an already-held coin (top-up) when
    # its AI still ranks it the best option — not just first-time buys
    allow_topup: bool = True
    # SS100d auto stop-loss: after EVERY buy fill (manual or automatic) the
    # sell-stop is recomputed from the new entry at a realistic distance
    # between these two fractions (0.02=2% .. 0.06=6%); volatility decides
    # where in the range it lands (quiet markets -> tight, wild -> wide)
    auto_stop_min_pct: float = 0.02
    auto_stop_max_pct: float = 0.06
    # default exit levels armed on EVERY buy (manual or automatic): a
    # fixed stop-loss percent (negative = below the buy price) and a
    # fixed take-profit percent (positive = above it), both measured
    # from the buy price. Stop 0 = fall back to the ATR band above.
    # Profit math on 0.25%+0.25% round-trip fees: -2/+15 needs +17%
    # winners per losing -2% exit to break even; -3/+25 needs only 5.5.
    default_stop_loss_pct: float = -3.0
    default_take_profit_pct: float = 25.0
    # HADD ratchet — always on. The moment the live price reaches the
    # current «حد سود فروش» the strike counter advances, the hadd (the
    # TP reference) moves to that price and the next take-profit re-arms
    # step% above it. 0 disables the ratchet.
    profit_lock_enabled: bool = True
    # ratchet step: each strike re-arms the take-profit this much above
    # the new hadd (and is the height of one «پله» of the ladder).
    profit_lock_step_pct: float = 15.0
    profit_lock_stage_floor_pct: float = 1.0
    profit_lock_max_stage: int = 0
    # نردبانِ پله‌ها (ladder) — با هر strike هم حد ضرر بالا می‌رود و هم
    # سرمایهٔ افزوده روی همان سکه می‌نشیند:
    # • حد ضرر روی «پلهٔ قبلی» می‌نشیند و هرگز پایین نمی‌آید — پله ۱ ←
    #   قیمت خرید (سربه‌سر)، پله ۲ ← پله ۱، پله ۳ ← پله ۲ … یعنی هر پله
    #   که بالا می‌رود سود پلهٔ قبل قفل می‌شود و یک پلهٔ کامل فضای نفس
    #   می‌ماند. (حد چند درصد زیر پلهٔ جدید نه: روی تکان معمولی بازار
    #   می‌فروخت و سود را قبل از ادامهٔ روند می‌بست.) مبنای درصد و مبلغ
    #   حد ضرر هم همان لحظه با هر پله به‌روز می‌شود.
    # • سرمایهٔ هر پله: همان **سودِ آن پله** — (قیمتی که پله به آن رسید −
    #   میانگین خرید) × تعداد سکه، از پول نقد آزاد. پله فقط از سود خودش
    #   سرمایه می‌گیرد (نه درصدی از ارزش موقعیت): با هر strike سودی که
    #   تازه به دست آمده دوباره روی همان سکه می‌نشیند و اگر آن قیمت بالای
    #   میانگین خرید نباشد، پله سرمایه نمی‌گیرد. موتور ریسک این مبلغ را
    #   به‌عنوان سقف اندازه می‌گیرد (حداقل سفارش صرافی و سقف سهم/نقد
    #   همچنان اعمال می‌شود) و هر پله حداکثر یک خرید دارد: اگر پول نقد یا
    #   سقف اجازه نداد، همان پله می‌سوزد (رویداد LADDER_TOPUP_BLOCKED
    #   دلیلش را ثبت می‌کند).
    ladder_enabled: bool = True
    ladder_topup_enabled: bool = True
    # After a STOP-LOSS exit (حد ضرر خورد)، rotate the freed cash into
    # the current best-ranked opportunity in the SAME cycle: the bot
    # synthesizes a BUY on the top-ranked market (risk engine still
    # gates size/cash/edge) so capital never sits idle after a losing
    # exit — the goal is growing TOTAL account value.
    rotate_after_stop: bool = True
    # per-market cooldown for the rotation BUY specifically (seconds);
    # keeps a stop-exited market from being instantly re-bought by the
    # rotation while still letting OTHER markets take the cash fast.
    rotate_cooldown_s: int = 120
    # SS100e profit top-up: when a held coin's since-buy profit reaches
    # this percent, the bot buys MORE of it with free cash (the risk engine
    # still sizes and gates the order). 0 disables.
    topup_profit_pct: float = 20.0
    # cancel OUR live orders still resting (unfilled) after this many
    # minutes so the locked IRT/coins return to the wallet and fresh
    # orders can be sent. 0 disables the reaper.
    stale_order_cancel_min: int = 10
    # SS101 dust conversion (panel balance/convert equivalent): balances
    # whose IRT value is BELOW this threshold are converted to IRT so the
    # cash is usable again for buys. 0 disables auto conversion.
    dust_convert_threshold_irt: float = 50000.0
    # coins that must NEVER be auto-converted (kept as assets even if tiny)
    dust_convert_exclude: str = "USDT"
    # safety: at most this many conversions per cycle (rate-limit guard)
    dust_convert_max_per_cycle: int = 3
    # حد ضرر/حد سود موقعیت‌های کوچک‌تر از حداقل سفارش صرافی: سفارش زیر
    # ۰٫۵ تتر همیشه REJECTED می‌شود (SmallOrder) و حد ضرر هرگز اجرا نمی‌شد؛
    # با روشن بودن این گزینه، خروجِ تحریک‌شده از مسیر «تبدیل» صرافی (بدون
    # کمینه) به تومان بسته می‌شود. خروجی تبدیل به تومان می‌نشیند، پس اگر
    # انحصار بخشی (تتری/مستقل) را می‌خواهید خاموش کنید تا خرده‌ها
    # دست‌نخورده بمانند.
    exit_convert_below_min: bool = True
    # ---------------- حالت ریسک (risk mode) — چرخش سریع روی برترین‌ها ----
    # OFF = حالت امن سابق: انتخاب تحلیلی چندعاملی، حد ضرر ‎-3٪، حد سود
    # ‎+25٪، خرید تکراری هر ۳۰ دقیقه و قفل طولانی بعد از حد ضرر.
    risk_mode: bool = False
    # در حالت ریسک رتبه‌بندی خرید با تغییر ۲۴ساعته (پرسودترین‌ها) انجام
    # می‌شود و حد ضرر/سود و cooldownها به مقادیر تندتر زیر سوییچ می‌شوند
    # (set_risk_mode در engine این فیلدها را زنده جایگزین می‌کند).
    # مقادیر بر اساس پژوهش‌های مستند حد ضرر (README، Update Note ۳۲):
    # • حد ثابت خیلی تنگ (‎-1.5٪) روی برترین‌های پرنوسان ۲۴ساعته = نویز
    #   (chartwhisperer: حد باید مضربی از ATR باشد؛ trendrider 2026: حد
    #   ‎-3٪ در تایم‌فریم‌های کوتاه بهترین win-rate/drawdown را دارد)
    # • حد سود ~‎+12٪ + قفل سود: با trailing‌های ATR-محور هم‌خوان است
    # • خروج افت (trailing از سقف) عریض‌تر از حد ضرر اولیه — 2× حد:
    #   trailing زودبیدار در بازار خُرد سود را می‌کُشد (cryptoriskcalc/blofin)
    risk_mode_stop_loss_pct: float = -3.0
    risk_mode_take_profit_pct: float = 12.0
    # فروش هنگام افت: از سقفِ قیمت از لحظه خرید، افت بیشتر از این درصد =
    # خروج کامل همان چرخه (trailing از سقف). ۰ = خاموش.
    risk_mode_drop_exit_pct: float = -6.0
    risk_mode_buy_cooldown_s: int = 300
    # ورود تنگ‌تر: سفارش خرید در همان قیمت لحظه‌ای (بدون pullback منفی)
    risk_mode_chase_pct: float = 0.0
    # تجمیع سرمایه: پوزیشن‌های خردِ ربات (خرید خود ربات، زیر این مقدار
    # تومان) فروش‌نشدنی‌اند (زیر حداقل سفارش صرافی) — در حالت ریسک از
    # مسیر تبدیل بدون‌حداقل صرافی به تومان برمی‌گردند تا قابل چرخش شوند.
    # ۰ = خاموش. فقط دارایی‌های خریدِ خودِ ربات؛ سکه‌های قبلی کاربر
    # هرگز خودکار فروش نمی‌روند.
    risk_mode_sweep_max_value_irt: float = 200000.0
    # حداکثر تعداد تجمیع در هر چرخه (کندی API)
    risk_mode_sweep_per_cycle: int = 4
    # ضریب اندازه خرید در حالت ریسک: خرید حداقل این‌ضریب از حداقل سفارش
    # صرافی (پیش‌فرض ۱٫۲) تا بعد از افت کوچک هنوز قابل فروش بماند و
    # چرخهٔ «خرید خرد → سوییپ فوری» کارمزد هدر ندهد
    risk_mode_size_floor_mult: float = 1.2
    # فیلتر پامپ بی‌حجم: در حالت ریسک بازارهایی که تغییر ۲۴ساعتهٔ بزرگ
    # دارند اما حجم معاملات ۲۴ساعتهٔ آن‌ها زیر این کف است (USDT) امتیاز
    # صفر می‌گیرند (LOW_VOLUME_PUMP) — خرید برنده‌های روز روی حجم کم = تله
    # پامپ/دامپ. ۰ = خاموش.
    risk_mode_min_quote_volume_usdt: float = 100000.0
    # لایهٔ رژیم بازار (regime gate): در حالت ریسک فقط رژیم‌های روندی
    # (صعودی قوی/ضعیف) خریدنی‌اند — چرخش روی پرسودترین‌ها در رنج یا روند
    # نزولی یعنی خرید سقف‌ها. خاموش‌کردنش = رفتار قبلی.
    risk_mode_require_uptrend: bool = True
    # آستانهٔ ATR٪ برای رژیم HIGH_VOLATILITY (کرش/جهش شدید) — هیچ استراتژی
    # این‌جا قابل معامله نیست؛ رژیم‌ساز همین مقدار را استفاده می‌کند.
    risk_mode_regime_atr_crash_pct: float = 8.0
    # حد ضرر ATR-محور حالت ریسک: max(کف ثابت، 1.5×ATR٪) با سقف مطلق —
    # تنگ‌تر از نوسان واقعی بازار = نویز؛ بازتر از سقف = ریسک بی‌مهار.
    risk_mode_atr_stop_floor_pct: float = -3.0
    risk_mode_atr_stop_mult: float = 1.5
    risk_mode_atr_stop_cap_pct: float = -8.0


@dataclass
class AICfg:
    engine: str = "heuristic"          # heuristic | llm
    top_n_markets: int = 5             # SS30 configurable N
    llm_base_url: str = ""             # OpenAI-compatible endpoint (optional)
    llm_api_key: str = ""
    llm_model: str = ""
    # چند مدل AI برای کمیته (ai_committee.py): هر مدل یک رأی می‌دهد و یک
    # BUY فقط وقتی مجاز است که ارزش انتظاری مثبتِ بعد از کارمزد تأیید شود.
    # لیست خالی = فقط مدل تکنیکال؛ llm_model بالا هم اگر ست باشد رأی می‌دهد.
    llm_models: str = ""                 # چندمدلی: با کاما جدا کنید
    # مثال: "gpt-4o-mini, claude-3-5-haiku, llama-3.1-70b" — هر مدل یک رأی
    # پیش‌فرض خاموش تا مسیر تستی/دموی قبلی عوض نشود؛ config.json این پروژه
    # آن را روشن می‌کند (committee_enabled: true).
    committee_enabled: bool = False
    # gate   : کمیته فقط می‌تواند BUY موتور را وتو کند
    # decide : کمیته خودش تصمیم BUY را می‌گیرد (موتور نقشهٔ ورود را می‌دهد)
    committee_mode: str = "gate"
    # آستانه‌های «قیمت بعد از خرید بالاتر می‌رود؟»
    committee_min_p_up: float = 0.60        # احتمال صعود
    committee_min_upside_pct: float = 1.0   # درصد حرکت مثبت انتظاری
    committee_min_net_edge_pct: float = 0.2  # ارزش انتظاری خالص بعد از هزینه
    committee_min_votes: int = 3            # حداقل رأی BUY (quorum)
    committee_horizon_s: int = 14400        # افق پیش‌بینی (ثانیه) — ۴ ساعت
    # فقط این تعداد کندیدیت برتر به مدل‌های LLM فرستاده می‌شوند (هزینهٔ
    # هر درخواست) — ۰ = هیچ مدل زبانی رأی نمی‌دهد
    committee_llm_top_n: int = 2
    # وزن هر مدل در رأی‌گیری (خالی = وزن‌های پیش‌فرض ai_committee)
    committee_weights: dict = field(default_factory=dict)
    max_context_tokens: int = 6000     # SS71 budgets
    max_output_tokens: int = 700
    max_candles_per_market: int = 60
    min_confidence: float = 0.55
    buy_best: bool = True             # always consider buying the top-ranked market
    # BUY limit entries rest this fraction BELOW the last price (0.0025 =
    # 0.25%) instead of paying the touch — realistic maker-style entry.
    # Set 0 to enter at the last price. (percentage-based, easy to tune)
    entry_pullback_pct: float = 0.0025


@dataclass
class PaperCfg:
    """Paper-trading account starting balances (demo mode only)."""
    starting_usdt: float = 10000.0
    starting_irt: float = 500000000.0   # ~5000 USDT at 100k IRT/USDT


@dataclass
class WebCfg:
    """Dashboard access control — the password gate before the program.

    password: the dashboard password. Empty = no gate, and the server then
      refuses to bind a NON-loopback address (a public dashboard without a
      password could place real orders). Either plaintext or a PBKDF2-SHA256
      hash: ``pbkdf2_sha256$<rounds>$<salt>$<hash>`` (generate with
      ``python -m nobitex_bot.webauth --hash``). Editing this value (or the
      file directly) is the recovery path after a lockout: a changed value
      clears the lock and drops every open session — no restart needed.
    max_attempts: wrong passwords before the lock (3 by default).
    lockout_seconds: 0 = stay locked until the password above changes;
      > 0 = auto-unlock after that many seconds as well.
    session_hours: how long one successful login stays valid.
    failure_delay_s: delay on every wrong password (anti brute-force).
    allow_remote_without_password: keep False. Only flip it when the gate is
      enforced somewhere else (e.g. an authenticating reverse proxy).
    """
    password: str = ""
    max_attempts: int = 3
    lockout_seconds: int = 0
    session_hours: float = 12.0
    failure_delay_s: float = 0.8
    allow_remote_without_password: bool = False


@dataclass
class Config:
    engine: EngineCfg = field(default_factory=EngineCfg)
    exchange: ExchangeCfg = field(default_factory=ExchangeCfg)
    fee: FeeCfg = field(default_factory=FeeCfg)
    quality: QualityCfg = field(default_factory=QualityCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    ai: AICfg = field(default_factory=AICfg)
    paper: PaperCfg = field(default_factory=PaperCfg)
    web: WebCfg = field(default_factory=WebCfg)
    schema_version: str = "1.0"

    def to_public_dict(self) -> dict:
        d = asdict(self)
        ex = d["exchange"]
        # Settings tab must always show the quote-market selector even on
        # config.json files written before this field existed
        ex.setdefault("trade_quote_markets", self.exchange.trade_quote_markets)
        # ... and the Both-mode interaction switch (transfer between the two
        # sections is allowed only when this says "linked")
        ex.setdefault("trade_market_interaction",
                      self.exchange.trade_market_interaction)
        ex["nobitex_api_key"] = "***" if ex["nobitex_api_key"] else ""
        ex["nobitex_password"] = "***" if ex["nobitex_password"] else ""
        ex["nobitex_totp_secret"] = "***" if ex["nobitex_totp_secret"] else ""
        d["ai"]["llm_api_key"] = "***" if d["ai"]["llm_api_key"] else ""
        # the dashboard password is a secret too: the settings tab sees the
        # mask (an untouched mask keeps the stored value on save)
        wb = d.setdefault("web", {})
        wb.setdefault("max_attempts", self.web.max_attempts)
        wb.setdefault("lockout_seconds", self.web.lockout_seconds)
        wb.setdefault("session_hours", self.web.session_hours)
        wb.setdefault("failure_delay_s", self.web.failure_delay_s)
        wb.setdefault("allow_remote_without_password",
                      self.web.allow_remote_without_password)
        wb["password"] = "***" if wb.get("password") else ""
        # SS100d/e: make sure every new risk knob is visible in the settings
        # panel even before config.json is rewritten (asdict covers dataclass
        # fields, but older config.json files may miss them — the defaults
        # come from the dataclass itself)
        rk = d.setdefault("risk", {})
        rk.setdefault("auto_stop_min_pct", self.risk.auto_stop_min_pct)
        rk.setdefault("auto_stop_max_pct", self.risk.auto_stop_max_pct)
        rk.setdefault("default_stop_loss_pct", self.risk.default_stop_loss_pct)
        rk.setdefault("default_take_profit_pct", self.risk.default_take_profit_pct)
        rk.setdefault("profit_lock_enabled", self.risk.profit_lock_enabled)
        rk.setdefault("profit_lock_step_pct", self.risk.profit_lock_step_pct)
        rk.setdefault("profit_lock_stage_floor_pct", self.risk.profit_lock_stage_floor_pct)
        rk.setdefault("profit_lock_max_stage", self.risk.profit_lock_max_stage)
        rk.setdefault("ladder_enabled", self.risk.ladder_enabled)
        rk.setdefault("ladder_topup_enabled", self.risk.ladder_topup_enabled)
        rk.setdefault("rotate_after_stop", self.risk.rotate_after_stop)
        rk.setdefault("rotate_cooldown_s", self.risk.rotate_cooldown_s)
        rk.setdefault("topup_profit_pct", self.risk.topup_profit_pct)
        rk.setdefault("stale_order_cancel_min", self.risk.stale_order_cancel_min)
        rk.setdefault("dust_convert_threshold_irt", self.risk.dust_convert_threshold_irt)
        rk.setdefault("dust_convert_exclude", self.risk.dust_convert_exclude)
        rk.setdefault("dust_convert_max_per_cycle", self.risk.dust_convert_max_per_cycle)
        rk.setdefault("risk_mode", self.risk.risk_mode)
        rk.setdefault("risk_mode_stop_loss_pct", self.risk.risk_mode_stop_loss_pct)
        rk.setdefault("risk_mode_take_profit_pct", self.risk.risk_mode_take_profit_pct)
        rk.setdefault("risk_mode_drop_exit_pct", self.risk.risk_mode_drop_exit_pct)
        rk.setdefault("risk_mode_buy_cooldown_s", self.risk.risk_mode_buy_cooldown_s)
        rk.setdefault("risk_mode_chase_pct", self.risk.risk_mode_chase_pct)
        rk.setdefault("risk_mode_sweep_max_value_irt",
                      self.risk.risk_mode_sweep_max_value_irt)
        rk.setdefault("risk_mode_sweep_per_cycle",
                      self.risk.risk_mode_sweep_per_cycle)
        rk.setdefault("risk_mode_size_floor_mult",
                      self.risk.risk_mode_size_floor_mult)
        rk.setdefault("risk_mode_min_quote_volume_usdt",
                      self.risk.risk_mode_min_quote_volume_usdt)
        rk.setdefault("risk_mode_require_uptrend",
                      self.risk.risk_mode_require_uptrend)
        rk.setdefault("risk_mode_regime_atr_crash_pct",
                      self.risk.risk_mode_regime_atr_crash_pct)
        rk.setdefault("risk_mode_atr_stop_floor_pct",
                      self.risk.risk_mode_atr_stop_floor_pct)
        rk.setdefault("risk_mode_atr_stop_mult",
                      self.risk.risk_mode_atr_stop_mult)
        rk.setdefault("risk_mode_atr_stop_cap_pct",
                      self.risk.risk_mode_atr_stop_cap_pct)
        return d


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    p = path or DEFAULT_CONFIG_PATH
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for section in ("engine", "exchange", "fee", "quality", "risk",
                            "ai", "paper", "web"):
                if section in data:
                    getattr(cfg, section).__dict__.update(data[section])
        except Exception as exc:  # never crash on bad config; log via event
            print(f"[config] failed to load {p}: {exc}")
    return cfg
