"""Learning layer for failed trades and recurring errors.

Reads ``failed_trade.db`` and ``error.db``, finds patterns that repeat often
enough to matter, and writes structured lessons. Deterministic and local by
default; the optional FreeLLMAPI call only *explains* a lesson and never
changes code, config, risk limits or strategy parameters.

Rules this module enforces (requirement C/D):

* one bad trade is never a lesson — ``min_evidence`` identical outcomes are
  required before a record is written;
* every lesson starts as ``proposed``; only an explicit, tested change may
  mark it ``verified`` (``Diagnostics.set_lesson_status``);
* AI output is stored as text, never applied.
"""
from __future__ import annotations

import json
import urllib.request
from collections import Counter, defaultdict
from typing import Any

from .diagnostics import Diagnostics, record_error
from .tls import context as tls_context

#: deterministic suggested correction per failure class. Kept short, and
#: deliberately phrased as a hypothesis to test — never an auto-applied fix.
CORRECTIONS = {
    "API_NETWORK": "Add/verify retry-with-backoff around this call, then "
                   "re-test the path with a mocked timeout before touching "
                   "strategy parameters.",
    "INVALID_ORDER": "Validate the request (clientOrderId shape, tick/step "
                     "rounding, side) before submission and add a regression "
                     "test with the exchange's exact rejection text.",
    "INSUFFICIENT_BALANCE": "Re-sync the wallet immediately before sizing and "
                            "clamp to the free balance; add a test with a "
                            "stale ledger balance.",
    "MIN_ORDER": "Compute the minimum notional from the live USDT/IRT rate "
                 "and the market's own minimum, and refuse locally instead "
                 "of sending an order the exchange will reject.",
    "LIQUIDITY": "Check book depth against the intended size before pricing; "
                 "skip or split the order instead of leaving it unmatched.",
    "SLIPPAGE": "Re-price from a fresh book snapshot and compare the expected "
                "fill with the max-slippage budget before submitting.",
    "PARTIAL_FILL": "Track the unfilled remainder explicitly and decide "
                    "(chase or cancel) instead of assuming a full fill.",
    "STRATEGY": "Treat as a strategy hypothesis: require out-of-sample "
                "backtest evidence before changing any parameter.",
    "MARKET_SELECTION": "Resolve the market through the canonical registry "
                        "(base+quote) and refuse unknown/paused markets "
                        "before creating a decision.",
    "AI_DECISION": "Compare the AI recommendation against the realised "
                   "outcome over a larger sample before weighting it lower.",
    "DATA_QUALITY": "Reject stale/missing prices at the gate and record the "
                    "data age so the cause is visible in the next failure.",
    "EXCHANGE_BEHAVIOR": "Reconcile the order status from the exchange before "
                         "reporting success; never infer a fill from a "
                         "submission reply.",
    "UNKNOWN": "Classify the next occurrence with more context (request, "
               "status, balances) before proposing a code change.",
}


def _evidence_trade(row: dict) -> dict:
    return {
        "attempt_id": row.get("attempt_id"),
        "timestamp": row.get("timestamp"),
        "symbol": row.get("symbol"),
        "action": row.get("intended_action"),
        "outcome": row.get("outcome"),
        "status": row.get("order_status"),
        "reason": (row.get("failure_reason") or "")[:200],
    }


def analyze_failed_trades(diag: Diagnostics, min_evidence: int = 3,
                          limit: int = 2000) -> list[dict]:
    rows = diag.failed_trades(limit=limit)
    if not rows:
        return []
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("failure_category") or "UNKNOWN")].append(row)
    lessons = []
    for category, items in groups.items():
        if len(items) < max(1, int(min_evidence)):
            continue
        symbols = Counter(r.get("symbol") or "?" for r in items).most_common(5)
        actions = Counter((r.get("intended_action") or "?").upper()
                          for r in items)
        sections = Counter(r.get("market_section") or "?" for r in items)
        ids = [r["attempt_id"] for r in items][:50]
        confidence = min(0.9, 0.3 + 0.1 * len(items))
        lesson_id = diag.save_lesson(
            pattern_key="FT:%s" % category,
            pattern="%d unsuccessful %s attempts share the failure class %s"
                    % (len(items), "/".join(sorted(actions)), category),
            root_cause=(
                "Repeated %s failures (%d occurrences). Most affected: %s. "
                "This is a recurring execution problem, not a single bad "
                "trade." % (category, len(items),
                            ", ".join("%s×%d" % (s, n) for s, n in symbols))),
            evidence={
                "count": len(items),
                "symbols": symbols,
                "actions": dict(actions),
                "market_sections": dict(sections),
                "outcomes": dict(Counter(r.get("outcome") or "?"
                                         for r in items)),
                "first": min((r.get("timestamp") or "" for r in items),
                             default=""),
                "last": max((r.get("timestamp") or "" for r in items),
                            default=""),
                "samples": [_evidence_trade(r) for r in items[:5]],
            },
            occurrences=len(items), confidence=round(confidence, 3),
            recommended_correction=CORRECTIONS.get(
                category, CORRECTIONS["UNKNOWN"]),
            related_failed_trade_ids=ids,
            related_error_ids=sorted({r["error_id"] for r in items
                                      if r.get("error_id")}))
        lessons.append({"lesson_id": lesson_id, "pattern_key": "FT:%s"
                        % category, "occurrences": len(items)})

    # symbol/strategy specific patterns are only reported when they clearly
    # dominate (>= 2x the average) so ordinary noise never becomes a lesson
    per_symbol = Counter(r.get("symbol") for r in rows if r.get("symbol"))
    if len(rows) >= max(1, int(min_evidence)) * 2:
        avg = len(rows) / max(1, len(per_symbol))
        for symbol, count in per_symbol.items():
            if count >= max(int(min_evidence), avg * 2):
                lesson_id = diag.save_lesson(
                    pattern_key="SYM:%s" % symbol,
                    pattern="%s concentrates %d failed attempts (average per "
                            "market is %.1f)" % (symbol, count, avg),
                    root_cause="One market produces a disproportionate share "
                               "of unsuccessful executions — check its "
                               "liquidity, tick size and market state.",
                    evidence={"symbol": symbol, "count": count,
                              "average_per_market": round(avg, 2),
                              "total": len(rows)},
                    occurrences=count,
                    confidence=round(min(0.85, 0.25 + 0.05 * count), 3),
                    recommended_correction="Inspect that market's book depth "
                                           "and minimum order before trading "
                                           "it again; exclude it if the "
                                           "pattern persists out-of-sample.",
                    related_failed_trade_ids=[r["attempt_id"] for r in rows
                                              if r.get("symbol") == symbol][:50])
                lessons.append({"lesson_id": lesson_id,
                                "pattern_key": "SYM:%s" % symbol,
                                "occurrences": count})
    return lessons


def analyze_errors(diag: Diagnostics, min_evidence: int = 3,
                   limit: int = 2000) -> list[dict]:
    rows = diag.errors(limit=limit)
    lessons = []
    for row in rows:
        if int(row.get("occurrences") or 0) < max(1, int(min_evidence)):
            continue
        category = row.get("category") or "UNKNOWN"
        lesson_id = diag.save_lesson(
            pattern_key="ER:%s" % row["error_id"],
            pattern="%s error repeats: %s" % (row.get("module") or category,
                                               (row.get("message") or "")[:180]),
            root_cause="A single error signature repeats %d times (%s in %s)."
                       % (row.get("occurrences"), row.get("exception_type")
                          or "non-exception", row.get("module") or "?"),
            evidence={"error_id": row["error_id"],
                      "occurrences": row.get("occurrences"),
                      "first_seen": row.get("first_seen"),
                      "last_seen": row.get("last_seen"),
                      "severity": row.get("severity"),
                      "resolution": row.get("resolution")},
            occurrences=int(row.get("occurrences") or 0),
            confidence=round(min(0.9, 0.3 + 0.05 * int(
                row.get("occurrences") or 0)), 3),
            recommended_correction=CORRECTIONS.get(category,
                                                   CORRECTIONS["UNKNOWN"]),
            related_error_ids=[row["error_id"]])
        lessons.append({"lesson_id": lesson_id,
                        "pattern_key": "ER:%s" % row["error_id"],
                        "occurrences": row.get("occurrences")})
    return lessons


def analyze(diag: Diagnostics, min_evidence: int = 3) -> dict:
    """Run both analyses; returns a summary the dashboard can render."""
    try:
        trade_lessons = analyze_failed_trades(diag, min_evidence)
        error_lessons = analyze_errors(diag, min_evidence)
    except Exception as exc:
        record_error("ANALYSIS", "WARNING",
                     "learning analysis failed: %s" % exc,
                     module="learning.analyze", exc=exc, recovered=True,
                     recovery_action="analysis skipped; trading unaffected")
        return {"ok": False, "error": str(exc), "lessons": []}
    return {"ok": True,
            "lesson_count": len(trade_lessons) + len(error_lessons),
            "trade_lessons": len(trade_lessons),
            "error_lessons": len(error_lessons),
            "lessons": trade_lessons + error_lessons,
            "note": "Lessons are proposals. None is applied to the strategy "
                    "until it has out-of-sample evidence and regression tests."}


# ------------------------------------------------------------------- AI
def _ai_payload(lessons: list[dict], failed: list[dict]) -> dict:
    return {
        "task": "Analyse recurring operational failures of a trading bot. "
                "Explain probable root causes, group duplicates and suggest "
                "concrete, testable corrections. Do not claim profitability. "
                "Do not propose bypassing risk controls.",
        "lessons": [
            {"pattern": l.get("pattern"), "root_cause": l.get("root_cause"),
             "occurrences": l.get("occurrences"),
             "correction": l.get("recommended_correction"),
             "confidence": l.get("confidence")}
            for l in lessons[:12]],
        "recent_failed_trades": [
            {"symbol": f.get("symbol"), "action": f.get("intended_action"),
             "category": f.get("failure_category"),
             "status": f.get("order_status"),
             "reason": (f.get("failure_reason") or "")[:160]}
            for f in failed[:15]],
    }


def ai_report(diag: Diagnostics, ai_cfg: Any, min_evidence: int = 3) -> dict:
    """Ask the configured FreeLLMAPI-compatible endpoint to explain failures.

    Fully optional: with no endpoint/key/model configured, or on any error,
    the local analysis keeps working and the caller gets ``ok=False``.
    The response is stored for a human to read; nothing is executed.
    """
    base = str(getattr(ai_cfg, "llm_base_url", "") or "").strip()
    key = str(getattr(ai_cfg, "llm_api_key", "") or "").strip()
    model = str(getattr(ai_cfg, "llm_model", "") or "").strip()
    if not (base and key and model):
        return {"ok": False, "reason": "FreeLLMAPI is not configured"}
    analyze(diag, min_evidence)
    lessons = diag.lessons(limit=50)
    failed = diag.failed_trades(limit=50)
    if not lessons and not failed:
        return {"ok": False, "reason": "no failure history yet"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "You are a careful reliability engineer for a crypto "
                        "trading bot. Be specific and evidence-based. Never "
                        "promise profit; never suggest skipping risk checks."},
            {"role": "user",
             "content": json.dumps(_ai_payload(lessons, failed),
                                   ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "max_tokens": int(getattr(ai_cfg, "max_output_tokens", 700) or 700),
    }
    url = base.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=45,
                                    context=tls_context()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = (body.get("choices") or [{}])[0].get("message", {}).get(
            "content") or ""
        text = str(text).strip()
        if not text:
            raise ValueError("empty AI response")
    except Exception as exc:
        # the key/base URL are never included in the recorded message
        record_error("AI", "WARNING",
                     "FreeLLMAPI analysis unavailable: %s" % type(exc).__name__,
                     module="learning.ai_report", exc=exc, recovered=True,
                     recovery_action="local statistical analysis continued")
        return {"ok": False, "reason": "FreeLLMAPI unavailable: %s"
                % type(exc).__name__}
    report_id = diag.save_learning_report(text, engine="freellmapi",
                                          model=model)
    return {"ok": True, "report_id": report_id, "report": text}


def describe(lessons: list[dict]) -> str:
    """Plain-text summary for logs/CLI without inventing conclusions."""
    if not lessons:
        return "no recurring failure pattern above the evidence threshold"
    lines = []
    for lesson in lessons:
        lines.append("- [%s] %s (x%s, confidence %.2f, %s)"
                     % (lesson.get("pattern_key"),
                        lesson.get("pattern"),
                        lesson.get("occurrences"),
                        float(lesson.get("confidence") or 0),
                        lesson.get("verification_status")))
    return "\n".join(lines)
