"""Single decision pipeline — one entry decision, ranked by NET edge.

Why this module exists
----------------------
Phase 1 (``docs/phase1-architecture.md``) found that the live engine can
produce a BUY from six independent places, ranks candidates by three
incompatible notions (24h change, scanner score, growth score), and — in RISK
mode — the "this move is already extended" guards are dropped for the day's
leaders: ``analysis.decide`` removes ``ENTRY_QUALITY_LOW`` /
``CORRECTION_RISK_ABOVE_MAX`` for ``top_gainer`` candidates and ``ai.py``
switches ``chase_ok`` off whenever risk mode is on. That is *why* the bot buys
after a big rise.

This module is the Phase-2 replacement for the ENTRY decision:

* **One scorer.** Every candidate is judged once, in one function, by net
  expected edge (probability × payoff − cost). Ranking follows that number and
  nothing else.
* **The entry-location veto is unconditional.** :func:`entry_location_veto`
  takes no ``top_gainer`` argument, so no ranking role can relax it, and an
  unknown entry location fails closed (no extension data = no buy).
* **Replayable.** Every decision carries the evidence snapshot it was made
  from, so it can be stored and re-checked later.

Units
-----
Every percentage in this module is a PERCENT (``0.25`` means 0.25%), never a
fraction. The engine mixes both conventions; keeping one here removes a whole
class of bugs.

Status
------
**Not wired into the engine.** This file places, sizes and cancels nothing and
``engine.py`` does not import it, so live and paper trading behave exactly as
before. Measuring it belongs to Phase 3 (backtest harness); no profitability
claim is made here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

BUY = "BUY"
AVOID = "AVOID"

__all__ = ["BUY", "AVOID", "Decision", "DecisionPipeline", "Evidence",
           "Policy", "entry_location_veto"]


@dataclass(frozen=True)
class Policy:
    """Thresholds. Defaults are deliberately conservative, not tuned.

    Nothing here is calibrated against data yet (Phase 3); they encode "don't
    pay a round trip for a move that has already happened", which is a *rule*,
    not an optimisation.
    """

    # --- entry location (the hard veto; never relaxable by a rank flag)
    max_extension_atr: float = 2.5      # price above EMA20, in ATR units
    min_room_pct: float = 0.7           # room to the next real resistance
    # --- data / tradability
    min_data_quality: float = 0.50
    min_liquidity: float = 0.30
    min_spread_cap_pct: float = 1.5     # spread cap floor
    max_spread_atr_frac: float = 0.5    # ... or half an ATR, whichever larger
    # --- edge
    min_p_up: float = 0.55
    min_net_edge_pct: float = 0.0       # a trade must be positive NET
    # --- payoff geometry
    atr_target_mult: float = 3.2
    atr_stop_mult: float = 1.2


@dataclass(frozen=True)
class Evidence:
    """Everything the entry decision is allowed to look at.

    ``top_gainer`` is recorded for the log only. It is intentionally NOT read
    anywhere in this module — that is the whole point of Phase 2.
    """

    symbol: str
    price: float = 0.0
    atr_pct: float = 0.0                    # 2.0 = 2% ATR
    extension_atr: float | None = None      # (price - EMA20) / ATR
    dist_resistance_pct: float | None = None
    p_up: float = 0.5                       # P(target before stop)
    expected_move_pct: float | None = None  # forward projection, percent
    change_24h: float = 0.0
    spread_pct: float = 0.0
    fee_pct: float = 0.25                   # ONE side
    slippage_pct: float = 0.10              # ONE side
    data_quality: float = 0.0
    liquidity: float = 0.0
    liquidity_status: str = "UNKNOWN"
    structure: str = "UNKNOWN"
    top_gainer: bool = False                # log only, never read


@dataclass
class Decision:
    """One evaluated candidate — the audit record of a single entry call."""

    symbol: str
    action: str = AVOID
    vetoes: list[str] = field(default_factory=list)
    net_edge_pct: float = 0.0
    cost_pct: float = 0.0
    target_pct: float = 0.0
    stop_pct: float = 0.0
    p_up: float = 0.0
    rank: int = 0
    policy: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)

    @property
    def buy(self) -> bool:
        return self.action == BUY

    def to_dict(self) -> dict:
        doc = asdict(self)
        doc["buy"] = self.buy          # the property is not a dataclass field
        return doc


def round_trip_cost_pct(ev: Evidence) -> float:
    """Fees both sides + slippage both sides + paying the spread on entry."""
    return (2.0 * max(0.0, ev.fee_pct)
            + 2.0 * max(0.0, ev.slippage_pct)
            + max(0.0, ev.spread_pct))


def entry_location_veto(ev: Evidence, policy: Policy) -> list[str]:
    """The "you are buying the top" guard — unconditional by construction.

    Deliberately takes no ``top_gainer`` parameter: a candidate cannot buy its
    way out of this check by being the day's biggest gainer. An unknown
    extension (no EMA/ATR) fails CLOSED, because the engine cannot prove the
    price is near a sane entry.
    """
    out: list[str] = []
    if ev.extension_atr is None:
        out.append("ENTRY_LOCATION_UNKNOWN")
    elif ev.extension_atr > policy.max_extension_atr:
        out.append("ENTRY_TOO_EXTENDED")
    if (ev.dist_resistance_pct is not None
            and ev.dist_resistance_pct < policy.min_room_pct):
        out.append("TOO_CLOSE_TO_RESISTANCE")
    return out


def _tradability_vetoes(ev: Evidence, policy: Policy) -> list[str]:
    out: list[str] = []
    if ev.price <= 0 or ev.atr_pct <= 0:
        out.append("NO_PRICE_DATA")
    if ev.data_quality < policy.min_data_quality:
        out.append("DATA_QUALITY_LOW")
    if str(ev.liquidity_status or "").upper() in ("CRITICAL", "UNKNOWN", ""):
        out.append("LIQUIDITY_UNTRADABLE")
    if ev.liquidity < policy.min_liquidity:
        out.append("LIQUIDITY_SCORE_LOW")
    spread_cap = max(policy.min_spread_cap_pct,
                     ev.atr_pct * policy.max_spread_atr_frac)
    if ev.spread_pct > spread_cap:
        out.append("SPREAD_TOO_WIDE")
    if str(ev.structure or "").upper() == "DOWNTREND":
        out.append("STRUCTURE_DOWNTREND")
    return out


def _geometry(ev: Evidence, policy: Policy) -> tuple[float, float, float]:
    """(gross_upside_pct, gross_downside_pct, cost_pct) — all in percent.

    Upside is the NEAREST of: the ATR travel target, the next real resistance
    and the forward projection. A ceiling 0.4% away must shrink the target,
    otherwise every trade looks profitable on paper.
    """
    cost = round_trip_cost_pct(ev)
    upside = ev.atr_pct * policy.atr_target_mult
    if ev.dist_resistance_pct is not None and ev.dist_resistance_pct > 0:
        upside = min(upside, ev.dist_resistance_pct)
    if ev.expected_move_pct is not None:
        upside = min(upside, max(0.0, ev.expected_move_pct))
    downside = max(ev.atr_pct * policy.atr_stop_mult, 2.0 * cost)
    return max(0.0, upside), max(0.0, downside), cost


def expected_edge(ev: Evidence, policy: Policy) -> tuple[float, float, float,
                                                         float]:
    """(net_edge_pct, target_pct, stop_pct, cost_pct).

    ``net = p_up × upside − (1 − p_up) × downside − cost``: the round trip is
    paid whatever happens, so it is charged once against the expectation.
    """
    upside, downside, cost = _geometry(ev, policy)
    p = min(1.0, max(0.0, float(ev.p_up)))
    net = p * upside - (1.0 - p) * downside - cost
    return net, upside, downside, cost


class DecisionPipeline:
    """The single place that turns evidence into BUY / AVOID."""

    def __init__(self, policy: Policy | None = None):
        self.policy = policy or Policy()

    def evaluate(self, ev: Evidence) -> Decision:
        policy = self.policy
        net, upside, downside, cost = expected_edge(ev, policy)
        vetoes = _tradability_vetoes(ev, policy)
        vetoes.extend(entry_location_veto(ev, policy))
        if float(ev.p_up) < policy.min_p_up:
            vetoes.append("P_UP_BELOW_MIN")
        if net < policy.min_net_edge_pct:
            vetoes.append("NET_EDGE_BELOW_MIN")
        # preserve order, drop duplicates (a veto is never averaged away)
        seen: set[str] = set()
        vetoes = [v for v in vetoes if not (v in seen or seen.add(v))]
        return Decision(
            symbol=ev.symbol,
            action=AVOID if vetoes else BUY,
            vetoes=vetoes,
            net_edge_pct=round(net, 4),
            cost_pct=round(cost, 4),
            target_pct=round(upside, 4),
            stop_pct=round(downside, 4),
            p_up=round(min(1.0, max(0.0, float(ev.p_up))), 4),
            policy=asdict(policy),
            evidence=asdict(ev),
        )

    def rank(self, evidences: list[Evidence]) -> tuple[list[Decision],
                                                       list[Decision]]:
        """(buyable ranked by net edge, rejected) — one pass, one order."""
        decisions = [self.evaluate(ev) for ev in evidences]
        buyable = sorted((d for d in decisions if d.buy),
                         key=lambda d: -d.net_edge_pct)
        for i, d in enumerate(buyable, 1):
            d.rank = i
        rejected = [d for d in decisions if not d.buy]
        return buyable, rejected

    def best(self, evidences: list[Evidence]) -> Decision | None:
        """The single best BUY, or None. Never invents a decision."""
        buyable, _rejected = self.rank(evidences)
        return buyable[0] if buyable else None
