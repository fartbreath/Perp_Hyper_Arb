"""
core/types.py — Cross-cutting typed contracts used by all layers.

These are the interface boundaries between the Market Data layer (L1),
the Strategy & Execution layer (L2), and the Presentation layer (L3).

Rules:
  - This file may only import from the Python standard library.
  - No imports from config, risk, strategies, monitor, or api_server.
  - All types are immutable or clearly documented as mutable state holders.

See INSTITUTIONAL_GRADE_PLAN.md §Reference Architecture for the full contract spec.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional


# ── Gate audit contract ───────────────────────────────────────────────────────

@dataclass
class GateResult:
    """
    Result of a single gate evaluation.

    Every gate in every strategy and monitor MUST produce one of these
    instead of using an early-return with no log.  The 'passed=False'
    instances are the audit trail that explains why an exit or entry
    was suppressed.

    Usage example (in ON scanner):
        gates: list[GateResult] = []
        gates.append(GateResult(
            gate="ON-07_winner_confirm",
            passed=winner_bid_ok,
            reason=f"winner_bid={winner_bid} floor={floor}",
            value=winner_bid,
            threshold=floor,
        ))
        failed = [g for g in gates if not g.passed]
        if failed:
            _log_gate_suppression(pair_id, failed, self._suppress_counts)
            return False
    """
    gate: str
    """Unique gate name, e.g. 'ON-07_winner_confirm', 'delta_sl_grace', 'funding_gate'."""

    passed: bool
    """True = gate allows the action; False = gate suppresses the action."""

    reason: str
    """Human-readable explanation, e.g. 'winner_bid=0.52 < floor=0.60'."""

    value: Optional[float] = None
    """The actual value that was checked against the threshold."""

    threshold: Optional[float] = None
    """The threshold the value was compared against."""


def _should_log_suppression(count: int, threshold: int) -> bool:
    """Return True only on counts that warrant a log entry.

    Emits on every count below threshold (low-volume info path), then only on
    the first breach of threshold and subsequent powers-of-2 multiples of
    threshold.  This prevents log flooding when a gate fires on every WS tick
    while still surfacing that the suppression is ongoing.

    Examples (threshold=3):
      count 1,2 → True (below threshold, INFO)
      count 3   → True (first WARNING — escalation)
      count 6   → True (2× threshold)
      count 12  → True (4× threshold)
      count 24  → True (8× threshold)
      count 4,5,7–11,13–23 → False (silent)
    """
    if count < threshold:
        return True
    # At or above threshold: only log at EXACT multiples of threshold that are
    # also a power-of-2 multiple (threshold, 2×, 4×, 8×, …).
    # Guard: count must divide evenly — without this, e.g. count=7 with
    # threshold=3 gives multiple=2 (a power of 2) and incorrectly returns True.
    if count % threshold != 0:
        return False
    multiple = count // threshold
    return multiple > 0 and (multiple & (multiple - 1)) == 0


def log_gate_suppression(
    log,
    entity_id: str,
    failed_gates: list[GateResult],
    suppress_counts: dict[str, int],
    threshold: int = 3,
    extra_fields: Optional[dict] = None,
) -> None:
    """
    Log suppressed gates using standard Python logger with structured kwargs.

    Emits DEBUG on every count below `threshold`.  At `threshold` and above,
    emits a WARNING only at power-of-2 multiples of `threshold` (10, 20, 40,
    …) so long-running suppressions do not flood the log.

    Args:
        log:             Logger instance (from get_bot_logger).
        entity_id:       Identifier for the entity being monitored (pair_id,
                         token_id, market_id — whatever is meaningful).
        failed_gates:    List of GateResult objects where passed=False.
        suppress_counts: Mutable dict[gate_name, consecutive_count] owned by
                         the caller. Updated in-place by this function.
        threshold:       Consecutive-suppression count before escalating to WARNING.
        extra_fields:    Optional additional log fields (strategy name, etc.).
    """
    extra = extra_fields or {}
    for g in failed_gates:
        suppress_counts[g.gate] = suppress_counts.get(g.gate, 0) + 1
        count = suppress_counts[g.gate]
        if not _should_log_suppression(count, threshold):
            continue
        fields = dict(
            entity_id=entity_id,
            gate=g.gate,
            reason=g.reason,
            consecutive=count,
            **extra,
        )
        if g.value is not None:
            fields["value"] = g.value
        if g.threshold is not None:
            fields["threshold"] = g.threshold

        if count >= threshold:
            log.warning("gate_suppressed", **fields)
        else:
            log.debug("gate_suppressed", **fields)


def reset_gate_suppress_counts(
    suppress_counts: dict[str, int],
    gate: Optional[str] = None,
) -> None:
    """
    Reset consecutive-suppression counters after a gate passes.

    Call this when the action fires successfully so the next suppression
    starts from 1 rather than continuing a stale high count.

    Args:
        suppress_counts: The mutable dict owned by the caller.
        gate:            If provided, reset only this gate. If None, reset all.
    """
    if gate is not None:
        suppress_counts.pop(gate, None)
    else:
        suppress_counts.clear()


# ── Feed health contract ──────────────────────────────────────────────────────

@dataclass
class FeedHealth:
    """
    Health state for a single oracle data feed.

    Produced by SpotOracle.get_feed_health(coin, market_type) and consumed by:
      - PositionMonitor._check_position_data_freshness()  (S2 + S3)
      - GET /health/feeds  (S4)
      - S3 state-transition logs

    See INSTITUTIONAL_GRADE_PLAN.md §S3.1 for the full contract spec.
    """

    coin: str
    """Underlying coin this feed provides data for, e.g. 'BTC', 'ETH'."""

    primary_source: str
    """Active oracle source: 'chainlink_streams' | 'rtds_chainlink' | 'chainlink_ws' | 'rtds' | 'none'."""

    price: Optional[float]
    """Most recent price from the primary source. None = no data ever received."""

    age_secs: Optional[float]
    """Seconds since last tick. None = never received data."""

    status: Literal["HEALTHY", "STALE", "DOWN"]
    """
    HEALTHY = data is fresh (age < staleness threshold).
    STALE   = data exists but is older than the staleness threshold.
    DOWN    = no data ever received, or primary source is disconnected.
    """

    last_reconnect_at: float = 0.0
    """Epoch timestamp of last Chainlink Streams full reconnect. 0.0 = never."""

    reconnect_count_1h: int = 0
    """Number of Chainlink Streams full reconnects in the rolling 60-minute window."""

    checked_at: float = field(default_factory=time.time)
    """Epoch timestamp when this snapshot was taken."""


@dataclass
class ShardHealth:
    """
    Health state for one PM WebSocket shard.

    Produced by PMClient.get_shard_health() and consumed by:
      - GET /health/feeds  (S4)
      - S3.2 state-transition logs

    See INSTITUTIONAL_GRADE_PLAN.md §S3.2 for the full contract spec.
    """

    shard_id: int
    """Unique shard identifier assigned at creation."""

    health: Literal["CONNECTING", "CONNECTED", "DEGRADED", "DISCONNECTED"]
    """
    CONNECTING    = running but not yet established a connection.
    CONNECTED     = WS open and messages flowing within the staleness window.
    DEGRADED      = WS open but no message received for > HL_WS_STALE_SECS seconds.
    DISCONNECTED  = WS closed (either stopped or between reconnect attempts).
    """

    token_count: int
    """Number of tokens assigned to this shard."""

    last_message_age_secs: Optional[float]
    """Seconds since the last WS message. None = never received a message."""

    message_count: int
    """Total messages received on this shard since start."""

    checked_at: float = field(default_factory=time.time)
    """Epoch timestamp when this snapshot was taken."""


@dataclass
class PositionDataHealth:
    """
    Data freshness status for a single open position.

    Produced by PositionMonitor._check_position_data_freshness() once per
    monitor sweep. Used to decide whether to trigger REST fallbacks (S2.2,
    S2.3) or a hard exit on stale oracle near expiry (S2.4).
    """

    token_id: str
    coin: str

    oracle_age_secs: Optional[float]
    """Age of last oracle tick for this coin. None = oracle has never ticked."""

    book_age_secs: Optional[float]
    """Age of PM book snapshot for this token. None = book never received."""

    oracle_status: Literal["HEALTHY", "STALE", "DOWN"]
    """HEALTHY = within MOMENTUM_SPOT_MAX_AGE_SECS, STALE = beyond, DOWN = None."""

    book_status: Literal["HEALTHY", "STALE", "DOWN"]
    """HEALTHY = within MOMENTUM_BOOK_MAX_AGE_SECS, STALE = beyond, DOWN = None."""

    oracle_source: Optional[str] = None
    """The oracle source currently serving this coin, e.g. 'chainlink_streams'."""


# ── Trade intent contract ─────────────────────────────────────────────────────

@dataclass
class TradeIntent:
    """
    A strategy's request to open a position.

    Strategies emit TradeIntent objects. The execution layer (RiskGate +
    PMClient.place_limit / place_market) handles the actual order placement.

    This is the target model — today's code still calls pm.place_limit()
    directly inside strategy scanners. New strategy code must use TradeIntent.
    Existing direct-placement code is legacy and will be migrated incrementally.

    The gates_passed field is the audit trail: it records every GateResult
    from every gate that evaluated this signal. Failed gates are included
    (they would have suppressed the intent if the overall evaluation returned
    True for all). This allows post-trade analysis of which gates were close
    calls.
    """

    strategy: str
    """Strategy identifier: 'momentum', 'opening_neutral', 'maker', 'mispricing'."""

    token_id: str
    """PM CLOB token ID for the token to buy."""

    side: Literal["YES", "NO", "UP", "DOWN"]
    """The market side being purchased."""

    size_usd: float
    """USDC notional to deploy."""

    order_type: Literal["limit", "market"]
    """Order placement type."""

    limit_price: Optional[float]
    """Required when order_type='limit'. None for market orders."""

    market_id: str
    """PM condition_id for the parent market."""

    coin: str
    """Underlying asset, e.g. 'BTC'."""

    market_type: str
    """Bucket type, e.g. 'bucket_5m'."""

    strike: float
    """Market strike price."""

    spot_at_signal: Optional[float]
    """Oracle spot price at signal time."""

    spot_age_at_signal: Optional[float]
    """Oracle age in seconds at signal time."""

    gates_passed: list[GateResult] = field(default_factory=list)
    """All gates evaluated for this signal. Both passed and failed gates included."""

    created_at: float = field(default_factory=time.time)
    """Epoch timestamp when the intent was created."""
