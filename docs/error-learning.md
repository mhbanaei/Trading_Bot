# Error monitoring and failed-trade intelligence

Two SQLite databases turn operational mistakes into searchable history, and a
deterministic analysis layer turns repeated patterns into **proposals** that a
human must still validate before any strategy parameter changes.

| File | Contents | Written by |
| --- | --- | --- |
| `error.db` (project root by default) | every captured error, deduplicated, with occurrence counters, stack traces, context and resolution status | the bot |
| `failed_trade.db` (project root by default) | unsuccessful **executions** (rejected / invalid / under-funded / below minimum / illiquid / partial), plus lessons and AI reports | the bot |
| `RTC/error.db`, `RTC/failed_trade.db` (default mirror) | read-only snapshots of the same two databases | `Diagnostics.sync_to_rtc()` |

Both files are created automatically on first use, and their parent directory
is created if missing. Neither is ever committed to Git (see `.gitignore` and
`RTC/.gitignore`).

### Where they live

The location is an **explicit setting**, never inferred from `engine.data_dir`
or any other path:

| Setting | Meaning |
| --- | --- |
| `diagnostics.directory` | `""` (default) = project root, next to `run.py`. A relative path resolves against the project root; an absolute path is used as-is (an isolated install or a test sandbox). |
| `diagnostics.rtc_mirror` | `"RTC"` (default) = mirror into `<project root>/RTC` when that directory exists. A relative path resolves against the project root; `""` disables the mirror even with `diagnostics.rtc_sync` on. |

So a test or a second install sets `diagnostics.directory` to its own temp
directory (and usually `diagnostics.rtc_mirror` to `""`) instead of relying on
an absolute `engine.data_dir` to opt out:

```json
{"diagnostics": {"directory": "C:/bot-state/diagnostics", "rtc_mirror": ""}}
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

## RTC mirroring

`Diagnostics.sync_to_rtc()` copies both databases with the SQLite **backup
API**, never by copying a live file:

1. the source connection is copied into `<name>.db.sync-tmp` in `RTC/`,
2. the copy is switched to `journal_mode=DELETE` (no `-wal` sidecar),
3. `os.replace()` swaps it in atomically.

On Windows a reader (an RTC dashboard, an inspection script) can hold the
destination file open; `os.replace` then fails and the copy falls back to the
backup API directly into the destination, which SQLite serialises safely. The
mirror is a **read-only snapshot** — only the main application writes.

`sync_rtc.py` never copies `*.db` files and its prune step skips every runtime
database, so refreshing the source mirror cannot delete the error history. The
generated `RTC/.gitignore` excludes `error.db` / `failed_trade.db` and their
sidecars.

## Dashboard

The «خطاها و یادگیری» tab shows total/critical/unresolved/repeated errors,
failed trade counts, lessons and their verification status, with filters for
date, symbol, IRT/USDT section, error category, severity, resolution status,
failure class and strategy. Stack traces are served only by
`/api/diagnostics/error?id=…`, behind the dashboard password gate. Actions:
re-run the analysis, sync the RTC mirror, mark an error resolved.

API:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/diagnostics` | summary + filtered errors, failed trades, lessons |
| GET | `/api/diagnostics/error?id=` | one error incl. stack trace |
| GET | `/api/learning` | lessons + stored AI reports |
| POST | `/api/learning/analyze` | deterministic analysis (`use_ai` opt-in) |
| POST | `/api/diagnostics/sync_rtc` | force the RTC mirror |
| POST | `/api/diagnostics/resolve` | change an error's resolution |

## Tests

`tests/test_diagnostics.py` covers automatic creation, restart persistence,
field capture, deduplication, stack traces, redaction, failed-trade records,
error↔trade links, repeated-pattern detection, lesson creation/status,
AI-unavailable fallback, concurrent writes, RTC sync (including a held-open
reader), backup usability after the source closes, runtime-database
preservation during a source refresh, bounded retention, logging capture and
safe no-ops after close. It also pins the explicit location settings
(`diagnostics.directory` / `rtc_mirror`: absolute, relative and disabled mirror)
and drives the real HTTP handler for `/api/diagnostics` and
`/api/diagnostics/error`, including the signed-out 401, server-side filters,
the stack-trace payload and the 404 for an unknown id. No real exchange order
is created or simulated.
