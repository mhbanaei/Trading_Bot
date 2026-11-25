# Error monitoring and failed-trade intelligence

Two SQLite databases turn operational mistakes into searchable history, and a
deterministic analysis layer turns repeated patterns into **proposals** that a
human must still validate before any strategy parameter changes.

| File | Contents | Written by |
| --- | --- | --- |
| `error.db` (under `data/` by default) | every captured error, deduplicated, with occurrence counters, stack traces, context and resolution status | the bot |
| `failed_trade.db` (under `data/` by default) | unsuccessful **executions** (rejected / invalid / under-funded / below minimum / illiquid / partial), plus lessons and AI reports | the bot |
| `RTC/*.json`, `RTC/diagnostics_report.md` | **sanitized** aggregates, proposals and synthetic reproduction cases exported from both databases | `python sync_rtc.py` (separate process) |

Both databases are created automatically on first use, and their parent
directory is created if missing. They are private runtime data: never
committed to Git, and never copied into `RTC/` — not with the SQLite backup
API, not any other way. The commit-ready snapshot gets reports instead.

### Where they live

The location is an **explicit setting**, never inferred from `engine.data_dir`
or any other path:

| Setting | Meaning |
| --- | --- |
| `diagnostics.directory` | `"data"` (default) = the runtime data directory under the project root. A relative path resolves against the project root; an absolute path is used as-is (an isolated install or a test sandbox); `""` = the project root itself. |

The key ships in the generated `config.example.json` (with this default), even
when the live `config.json` predates it. There is deliberately no setting that
points a database at `RTC/`: the snapshot is produced by reports only.

So a test or a second install sets `diagnostics.directory` to its own temp
directory instead of relying on an absolute `engine.data_dir` to opt out:

```json
{"diagnostics": {"directory": "C:/bot-state/diagnostics"}}
```

Changing `directory` starts a fresh pair of databases there; the previous files
are left untouched (nothing is moved or deleted).

## What belongs in each database

`error.db` captures, with category and severity:

* Nobitex API failures (`EXCHANGE_API`, path and error code only),
* execution rejections (`EXECUTION`),
* market-data problems (`MARKET_DATA`),
* AI / committee failures (`AI`),
* analysis and risk-engine faults (`ANALYSIS`, `RISK`),
* database faults (`DATABASE`),
* background workers and cycles (`WORKER`, `SCHEDULER`),
* dashboard requests (`DASHBOARD`), config loading (`CONFIG`),
* wallet/universe sync (`SYNC`) and RTC mirroring (`RTC`),
* uncaught exceptions in the main thread and in any thread
  (`sys.excepthook` / `threading.excepthook`),
* records emitted through Python `logging` at ERROR or above.

`failed_trade.db` captures unsuccessful executions only. **A losing trade is
not a failed trade**: a position closed at its designed stop-loss is a normal
outcome recorded in the `performance` table, never here. Fields that the bot
does not actually know (fees, actual price, order-book snapshot, indicators)
stay `NULL` — nothing is inferred or invented.

Failure classes: `API_NETWORK`, `INVALID_ORDER`, `INSUFFICIENT_BALANCE`,
`MIN_ORDER`, `LIQUIDITY`, `SLIPPAGE`, `PARTIAL_FILL`, `STRATEGY`,
`MARKET_SELECTION`, `AI_DECISION`, `DATA_QUALITY`, `EXCHANGE_BEHAVIOR`,
`UNKNOWN`. The classifier reads the rejection text (English and Persian
keywords, exchange error codes) — it does not guess from profit/loss.

## Deduplication and bounded storage

A repeating error must not grow the file without limit. The fingerprint is
`category + module + exception type + normalized message`, where digits and
long hex ids collapse to `<n>`/`<id>`. A repeat updates `occurrences`,
`last_seen`, the newest stack trace and escalates severity (never downgrades);
an error that was marked `resolved` and comes back becomes `regressed`.

Retention (`diagnostics.retention_days`, `max_errors`, `max_failed_trades`)
removes only the oldest diagnostic rows. Orders, fills, positions and
performance rows are untouched, and lessons are never pruned.

## Secrets

Before anything is written, messages, stack traces and context go through
`Redactor`:

* the concrete values from `config.json` / credentials known to the process
  are replaced with `***` (they are only used for matching, never stored),
* shape-based patterns catch other tokens (`sk-…`, `freellmapi-…`, GitHub
  tokens, AWS keys, Slack/Telegram tokens, `Bearer …`, private key blocks),
* `"password": "…"`, `token=…`, `api_key=…` keep the key and drop the value.

No API key, password, TOTP secret, session token or Authorization header is
ever persisted.

## Learning layer (`nobitex_bot/learning.py`)

`analyze()` groups failed trades by failure class (and by market when one
market dominates) and errors by fingerprint. A group becomes a lesson only
after `diagnostics.learning_min_evidence` occurrences (default 3). Each lesson
stores the pattern, root cause, evidence (counts, symbols, sample ids, time
range), confidence, a recommended correction and `verification_status`.

Rules enforced in code:

* every lesson starts as `proposed`;
* re-analysis updates the existing lesson (same `pattern_key`) instead of
  duplicating it, and never downgrades a `verified` lesson;
* only `set_lesson_status()` can move a lesson to `verified`, and nothing in
  the bot calls it — validation stays a human, tested decision;
* the optional FreeLLMAPI call explains and groups failures. It is opt-in
  (`diagnostics.learning_ai_enabled`), runs read-only, writes a report, and
  cannot edit source code, run migrations, change config or bypass risk
  controls. When the endpoint is unavailable the local analysis still runs.

Recommended corrections are hypotheses, not applied changes. A strategy
parameter is only changed after out-of-sample evidence and regression tests.

## Failure capture paths

* `nobitex_api._raw_request` / `_public_get` — one record per failed request.
* `execution.execute` — price-unavailable, no-size and cash/minimum skips.
* `execution._fail_real` — rejected real orders, with the exchange text.
* `execution._live_fill_from_order` — fills that could not be applied.
* partial fills on a live order.
* `engine.manual_buy` / `sell_position_now` — manual attempts that did not end
  in a fill, including risk rejections and unfilled sells.
* `engine._run_loop` — a failed cycle (`CRITICAL`, next cycle continues).
* `collector` — discovery, per-market collection and candle-store failures.
* `web.do_GET` / `web.do_POST` — handler exceptions.

Writes are queued on a bounded background queue (5000 items) with a dedicated
writer thread: a slow disk can never stall a trading cycle. If the queue is
full, writes are dropped and `summary()["dropped_writes"]` shows the loss —
never a silent exception. Every write also appends a human-readable line to
`logs/errors.log`; SQLite remains the structured source of truth.

## Public snapshot: sanitized exports

The two sides have separate responsibilities.

**Private (the bot):** `error.db` and `failed_trade.db` hold the full history —
real messages, real order ids, real account context. They stay on the machine
that runs the bot. `Diagnostics.backup_local(dir)` may take a consistent copy
with the SQLite backup API, but only into a private directory of the operator's
choosing.

**Public (`RTC/`):** `python sync_rtc.py` runs a separate export process
(`nobitex_bot/rtc_export.py`) that reads the private databases read-only and
writes nine files:

| File | Contents |
| --- | --- |
| `error_summary.json` | totals per category/severity/module, most frequent sanitized signatures, file/line/function instead of stack text |
| `failed_trade_summary.json` | attempts per failure class/market/action/strategy, shares, relative slippage and fill ratios |
| `learning_lessons.json` | lesson text, confidence, verification status (all `proposed`), sanitized evidence |
| `execution_metrics.json` | slippage in basis points, fill ratios, rejection shares |
| `strategy_metrics.json` | unsuccessful attempts per strategy and symbol (reliability, not profitability) |
| `data_quality_report.json` | market-data/sync signals plus database health |
| `database_schema.json` | tables, columns and indexes only — no rows |
| `reproduction_cases.json` | **authored synthetic fixtures**, explicitly not recorded incidents |
| `diagnostics_report.md` | human-readable summary and the export policy |

Three gates run before a byte is written:

1. **allowlist** — fields are selected from `ERROR_FIELDS` / `TRADE_FIELDS` /
   `LESSON_FIELDS`; a new column is ignored until it is named, and keys such as
   `portfolio_before`, `order_id`, `token` or `apiKey` are refused at any depth.
2. **sanitization** — credentials are redacted, absolute paths become
   `<path>/file.py`, e-mail and wallet addresses are masked, identifiers are
   pseudonymized with a per-export random salt (correlate inside one snapshot,
   non-reversible afterwards), and amounts stay relative (ratios, bps, shares).
3. **validation** — the finished document is re-scanned. A violation raises
   `PrivacyError`, so the export aborts with a field location and never writes a
   leaky report. `sync_rtc.py` deletes any raw `.db`, `-wal`, `-shm` or
   `-journal` it finds in `RTC/` and fails the run if one is still there.

`python sync_rtc.py --check` re-validates the reports already in `RTC/` without
changing anything.

## Dashboard

The «خطاها و یادگیری» tab shows total/critical/unresolved/repeated errors,
failed trade counts, lessons and their verification status, with filters for
date, symbol, IRT/USDT section, error category, severity, resolution status,
failure class and strategy. Stack traces are served only by
`/api/diagnostics/error?id=…`, behind the dashboard password gate. Actions:
re-run the analysis and mark an error resolved. Publishing the sanitized
snapshot is not a dashboard action: it is `python sync_rtc.py`, a separate
process an operator runs deliberately.

API:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/diagnostics` | summary + filtered errors, failed trades, lessons |
| GET | `/api/diagnostics/error?id=` | one error incl. stack trace |
| GET | `/api/learning` | lessons + stored AI reports |
| POST | `/api/learning/analyze` | deterministic analysis (`use_ai` opt-in) |
| POST | `/api/diagnostics/resolve` | change an error's resolution |

## Tests

`tests/test_diagnostics.py` covers automatic creation, restart persistence,
field capture, deduplication, stack traces, redaction, failed-trade records,
error↔trade links, repeated-pattern detection, lesson creation/status,
AI-unavailable fallback, concurrent writes, local backup usability after the
source closes, deletion of raw databases from `RTC/`, bounded retention,
logging capture and safe no-ops after close. It also pins the explicit
`diagnostics.directory` setting (absolute and relative) and drives the real
HTTP handler for `/api/diagnostics` and
`/api/diagnostics/error`, including the signed-out 401, server-side filters,
the stack-trace payload and the 404 for an unknown id. `tests/test_rtc_export.py`
is the privacy gate: it seeds identifiers, paths, balances and a wallet-shaped
value, exports them, and asserts none survive, that the allowlist drops unknown
columns, that a leaky document aborts the export with nothing written, and that
the private databases are only read. No real exchange order is created or
simulated.
