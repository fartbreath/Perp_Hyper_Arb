
"""
main.py — Bot entry point

Usage:
    python main.py                    # Paper trading (default)
    PAPER_TRADING=false python main.py  # Live trading (careful!)

Architecture (all asyncio tasks):
  ┌─────────────────────────────────────────────────────┐
  │  asyncio event loop                                  │
  │                                                      │
  │  pm_client.run()     ← WS + heartbeat               │
  │  hl_client.run()     ← WS + dead man's switch        │
  │  maker_strategy.start()  ← quoting + hedge          │
  │  mispricing_scanner.start()  ← scan every 300s      │
  │  agent_loop()        ← consumes signal queue         │
  │  api_server          ← FastAPI REST for webapp       │
  │  state_sync_loop()   ← pushes bot state to api       │
  └─────────────────────────────────────────────────────┘

Signal flow (Strategy 2):
  MispricingScanner --(signal)--> asyncio.Queue
    --> agent_loop()
        [shadow mode] --> log + await human_approve() via stdin
        [auto mode]   --> AgentDecisionLayer.evaluate()
            --> EXECUTE: pm_client.place_limit() + risk.open_position()
            --> SKIP: log
            --> HALT: graceful_shutdown()
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

# Load .env before importing anything that reads env vars
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv optional; env vars can be set externally

import config
from logger import get_bot_logger
import api_server
from api_server import state as api_state
from market_data.pm_client import PMClient
from market_data.hl_client import HLClient
from market_data.rtds_client import RTDSClient
from market_data.chainlink_ws_client import ChainlinkWSClient
from market_data.chainlink_streams_client import ChainlinkStreamsClient
from market_data.spot_oracle import SpotOracle
from risk import RiskEngine, Position
from strategies.maker.strategy import MakerStrategy
from strategies.mispricing.strategy import MispricingScanner
from strategies.mispricing.signals import MispricingSignal
from strategies.Momentum.scanner import MomentumScanner
from strategies.Momentum.vol_fetcher import VolFetcher
from agent import AgentDecisionLayer, AgentDecision
from monitor import PositionMonitor, compute_unrealised_pnl
from fill_simulator import FillSimulator
from live_fill_handler import LiveFillHandler

log = get_bot_logger(__name__)

# ── Global signal queue ────────────────────────────────────────────────────────
# MispricingScanner puts signals here; agent_loop() reads them
_signal_queue: asyncio.Queue[MispricingSignal] = asyncio.Queue(maxsize=50)

# Shutdown event — set any time we want a clean exit
_shutdown_event = asyncio.Event()

# ── State-change event ─────────────────────────────────────────────────────────
# Set by position-open/close callbacks and signal arrivals so that state_sync_loop
# wakes immediately instead of waiting the full 1 s backstop interval.

_state_changed: asyncio.Event = asyncio.Event()


def notify_state_changed() -> None:
    """Signal that bot state has changed; wakes the state_sync_loop early."""
    _state_changed.set()


# ── Signal callback from scanner ──────────────────────────────────────────────

async def _on_mispricing_signal(signal_obj: MispricingSignal) -> None:
    """Enqueue a mispricing signal for the agent loop. Drop if full."""
    try:
        _signal_queue.put_nowait(signal_obj)
        # Also push to API state for webapp
        api_state.signals.append({
            "market_id": signal_obj.market_id,
            "market_title": signal_obj.market_title,
            "pm_price": signal_obj.pm_price,
            "implied_prob": signal_obj.implied_prob,
            "deviation": round(signal_obj.deviation, 4),
            "direction": signal_obj.direction,
            "fee_hurdle": round(signal_obj.fee_hurdle, 4),
            "deribit_iv": round(signal_obj.deribit_iv, 4),
            "deribit_instrument": signal_obj.deribit_instrument,
            "is_actionable": signal_obj.is_actionable,
            "score": round(signal_obj.score, 1),
            "signal_source": signal_obj.signal_source,
            "timestamp": time.time(),
            "agent_decision": None,   # filled by agent loop
        })
        # Cap in-memory list to last 200 signals (matches momentum_signals pattern)
        if len(api_state.signals) > 200:
            api_state.signals = api_state.signals[-200:]
    except asyncio.QueueFull:
        log.warning("Signal queue full — dropping signal", market=signal_obj.market_title)
    else:
        notify_state_changed()


# ── Agent loop ────────────────────────────────────────────────────────────────

async def agent_loop(
    pm: PMClient,
    agent: AgentDecisionLayer,
    monitor: PositionMonitor,
    risk: RiskEngine,
    max_size_usd: float = config.MAX_PM_EXPOSURE_PER_MARKET * 0.5,
) -> None:
    """
    Consume signals from _signal_queue and make decisions.

    In SHADOW mode: logs agent recommendation; asks human to approve/deny.
    In AUTO mode:   acts directly on agent recommendation.
    """
    log.info("Agent loop started", auto=config.AGENT_AUTO)

    while not _shutdown_event.is_set():
        try:
            sig = await asyncio.wait_for(_signal_queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            continue

        if not config.BOT_ACTIVE:
            log.info("Agent loop idling — BOT_ACTIVE=False", market=sig.market_title)
            continue

        if config.BYPASS_AGENT:
            # Skip Ollama entirely — treat every actionable signal as EXECUTE
            decision = AgentDecision(
                decision="EXECUTE",
                confidence=1.0,
                reason="BYPASS_AGENT mode — auto-executing all actionable signals",
                suggested_size_pct=1.0,
            )
            log.info("Agent bypassed", market=sig.market_title,
                     deviation=round(sig.deviation, 4), direction=sig.direction)
        else:
            try:
                decision = await agent.evaluate(sig)
            except Exception as exc:
                log.error("Agent eval failed", exc=str(exc))
                continue

        # Update the webapp signal with agent decision
        for entry in reversed(api_state.signals):
            if entry["market_id"] == sig.market_id:
                entry["agent_decision"] = decision.decision
                entry["agent_confidence"] = round(decision.confidence, 3)
                entry["agent_reason"] = decision.reason
                break

        if decision.is_halt:
            log.critical("HALT from agent — initiating shutdown", reason=decision.reason)
            _shutdown_event.set()
            return

        if not decision.is_execute:
            log.info("Signal skipped", market=sig.market_title, reason=decision.reason)
            continue

        # In shadow mode: require human approval before acting
        if not config.AGENT_AUTO:
            approved = await _human_approve(sig, decision)
            if not approved:
                log.info("Human denied signal", market=sig.market_title)
                continue

        # Execute the trade
        size_usd = max_size_usd * decision.suggested_size_pct
        log.info(
            "Executing signal",
            market=sig.market_title,
            direction=sig.direction,
            size=round(size_usd, 2),
            auto=config.AGENT_AUTO,
        )

        market = pm._markets.get(sig.market_id)
        if market is None:
            log.warning("Market not found in pm_client cache", market_id=sig.market_id)
            continue

        token_id = (
            market.token_id_yes if sig.direction == "BUY_YES"
            else market.token_id_no
        )
        # sig.pm_price is always the YES token price. For BUY_NO orders the
        # order targets the NO token, which trades at (1 − YES price).
        order_price = sig.pm_price if sig.direction == "BUY_YES" else (1.0 - sig.pm_price)
        order_id = await pm.place_limit(
            token_id=token_id,
            side="BUY",
            price=order_price,
            size=size_usd,
            market=market,
        )
        if order_id:
            pos = Position(
                market_id=sig.market_id,
                market_type=market.market_type,
                underlying=sig.underlying,
                side="YES" if sig.direction == "BUY_YES" else "NO",
                size=size_usd,
                entry_price=sig.pm_price,
                strategy="mispricing",
                # Signal context for retrospective analysis
                entry_deviation=sig.deviation,
                implied_prob=sig.implied_prob,
                deribit_iv=sig.deribit_iv,
                tte_years=sig.tte_years,
                spot_price=sig.spot_price,
                strike=sig.strike if sig.strike is not None else 0.0,
                kalshi_price=sig.kalshi_price if sig.kalshi_price is not None else 0.0,
                signal_source=sig.signal_source,
                signal_score=sig.score,
            )
            risk.open_position(pos)
            monitor.record_entry_deviation(sig.market_id, sig.deviation)
            log.info(
                "Position opened",
                order_id=order_id,
                market=sig.market_title,
                entry_price=sig.pm_price,
                size_usd=size_usd,
                deviation=round(sig.deviation, 4),
                score=round(sig.score, 1),
            )


# ── Human approval prompt (shadow mode) ──────────────────────────────────────

async def _human_approve(sig: MispricingSignal, decision: AgentDecision) -> bool:
    """
    Print the signal summary + agent decision and wait for y/n on stdin.
    Non-blocking via asyncio executor.
    If config.AUTO_APPROVE is True, skips the prompt and returns True automatically.
    """
    print("\n" + "=" * 60)
    print(sig.summary())
    print(f"\nAgent: {decision.decision}  confidence={decision.confidence:.0%}")
    print(f"Reason: {decision.reason}")
    print("=" * 60)

    if config.AUTO_APPROVE:
        print("[AUTO_APPROVE=True] Automatically approving.")
        return True

    def _read() -> str:
        try:
            return input("Execute? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "n"

    answer = await asyncio.get_event_loop().run_in_executor(None, _read)
    return answer == "y"


# ── State sync loop ────────────────────────────────────────────────────────────

async def state_sync_loop(
    pm: PMClient,
    hl: HLClient,
    maker: MakerStrategy,
    agent: AgentDecisionLayer,
    risk: RiskEngine,
    spot_client: "SpotOracle",
) -> None:
    """
    Periodically push live bot state into api_state so the webapp
    reflects current reality without needing direct references.
    """
    # Set component references once — used by API action endpoints
    api_state.maker_ref = maker
    while not _shutdown_event.is_set():
        try:
            # Consistent snapshots — avoid calling getattr on private attrs inside the loop
            positions_snap = risk.get_positions()
            markets_snap = pm.get_markets()
            active_quotes_snap = maker.get_active_quotes()
            coin_hedges_snap = maker.get_coin_hedges()
            funding_snap = hl.get_fundings_snapshot()

            # Health — WS status comes from pm/hl clients
            api_state.pm_ws_connected = getattr(pm, "_ws_connected", False)
            api_state.hl_ws_connected = getattr(hl, "_ws_connected", False)
            api_state.last_heartbeat_ts = getattr(pm, "_last_heartbeat_ts", 0.0)
            api_state.bot_active = config.BOT_ACTIVE

            # Positions from risk engine
            # Recently-closed positions remain visible for CLOSED_GRACE_SECONDS so
            # the webapp doesn't immediately lose track of them on close.
            _CLOSED_GRACE_SECONDS = 300
            _now_time = time.time()
            positions_raw = {}
            for cid, pos in positions_snap.items():
                if pos.is_closed:
                    if pos.closed_at is None:
                        continue  # closed before closed_at tracking; hide immediately
                    _age = _now_time - pos.closed_at.timestamp()
                    if _age > _CLOSED_GRACE_SECONDS:
                        continue
                _mkt = markets_snap.get(pos.market_id)  # cid is now a composite key; use pos.market_id
                # Get current order book mid for the HELD token from live WebSocket feed.
                # YES positions use the YES book; NO positions use the NO book.
                _book = pm.get_book(_mkt.token_id_yes) if _mkt else None
                _book_no = pm.get_book(_mkt.token_id_no) if _mkt else None
                _held_book = _book if pos.side in ("YES", "BUY_YES") else _book_no
                _cur_mid: float | None = None
                if _held_book:
                    _b, _a = _held_book.best_bid, _held_book.best_ask
                    if _b is not None and _a is not None:
                        _cur_mid = (_b + _a) / 2
                    elif _b is not None:
                        _cur_mid = _b
                    elif _a is not None:
                        _cur_mid = _a
                # Active bid-quote fill state (YES positions) and ask-quote fill state (NO positions)
                _token_id = _mkt.token_id_yes if _mkt else None
                _bid_q = (
                    active_quotes_snap.get(_token_id)
                    if (_token_id and pos.strategy == "maker")
                    else None
                )
                _bid_orig = _bid_q.original_size if _bid_q else None
                _bid_rem = _bid_q.size if _bid_q else None
                _bid_fill = (
                    round(_bid_orig - _bid_rem, 4)
                    if _bid_orig is not None and _bid_rem is not None
                    else None
                )
                # ASK quote — fills produced by our resting ask become NO positions
                _ask_q = (
                    active_quotes_snap.get(f"{_token_id}_ask")
                    if (_token_id and pos.strategy == "maker")
                    else None
                )
                _ask_orig = _ask_q.original_size if _ask_q else None
                _ask_rem = _ask_q.size if _ask_q else None
                _ask_fill = (
                    round(_ask_orig - _ask_rem, 4)
                    if _ask_orig is not None and _ask_rem is not None
                    else None
                )
                # Compute unrealised P&L server-side using the same function
                # as the monitor — single source of truth, consistent with logs.
                _token_entry_price: float | None = None
                _token_current_price: float | None = None
                _unrealised_pnl: float | None = None
                if _cur_mid is not None:
                    # entry_price and current price are both actual token prices.
                    _token_entry_price = pos.entry_price
                    _token_current_price = _cur_mid
                    _unrealised_pnl = round(
                        compute_unrealised_pnl(pos, _cur_mid), 4
                    )
                # Profit-target progress (mispricing strategy only)
                _monitor = api_state.monitor_ref
                _profit_target_usd: float | None = None
                _pct_of_target: float | None = None
                if pos.strategy != "maker" and _monitor is not None:
                    _init_dev = _monitor.get_entry_deviation(
                        pos.market_id, config.MISPRICING_THRESHOLD
                    )
                    _profit_target_usd = round(
                        _init_dev * config.PROFIT_TARGET_PCT * pos.size, 4
                    )
                    if _profit_target_usd > 0 and _unrealised_pnl is not None:
                        _pct_of_target = round(
                            _unrealised_pnl / _profit_target_usd * 100, 1
                        )
                # Estimated close P&L at book (taker exit) including already-earned
                # entry rebates + projected exit-leg rebate, minus taker exit fee.
                _est_close_pnl: float | None = None
                if _held_book:
                    _exit_p_book = _held_book.best_bid
                    if _exit_p_book is not None:
                        _pnl_price = compute_unrealised_pnl(pos, _exit_p_book)
                        # actual token price for fee formula
                        _tok_ep = _exit_p_book
                        _fees_on = (_mkt.fees_enabled if _mkt else True)
                        _rebate_pct = (_mkt.rebate_pct if _mkt else 0.0)
                        _exit_fee = (
                            pos.size * _tok_ep * (1.0 - _tok_ep) * config.PM_FEE_COEFF
                            if _fees_on else 0.0
                        )
                        _exit_rebate = (
                            pos.size * config.PM_FEE_COEFF * _tok_ep * (1.0 - _tok_ep) * _rebate_pct
                            if _fees_on else 0.0
                        )
                        _est_close_pnl = round(
                            _pnl_price - _exit_fee + pos.pm_rebates_earned + _exit_rebate, 4
                        )
                positions_raw[cid] = {
                    "condition_id": pos.market_id,
                    "market_title": _mkt.title if _mkt else pos.market_id,
                    "market_slug": _mkt.market_slug if _mkt else "",
                    "end_date": _mkt.end_date.isoformat() if _mkt and _mkt.end_date else None,
                    "underlying": pos.underlying,
                    "side": pos.side,
                    "size_usd": pos.size,       # contracts (legacy field name kept for compat)
                    "contracts": pos.size,      # explicit contract count
                    "entry_cost_usd": round(pos.entry_cost_usd, 2),
                    "entry_price": pos.entry_price,         # actual token fill price
                    "token_entry_price": round(_token_entry_price, 4) if _token_entry_price is not None else pos.entry_price,
                    "token_current_price": round(_token_current_price, 4) if _token_current_price is not None else None,
                    "current_mid": round(_cur_mid, 4) if _cur_mid is not None else None,
                    "unrealised_pnl_usd": _unrealised_pnl,
                    "profit_target_usd": _profit_target_usd,
                    "pct_of_target": _pct_of_target,
                    "book_age_s": round(time.time() - _book.timestamp, 1) if _book else None,
                    "yes_book_bid": round(_book.best_bid, 4) if _book and _book.best_bid is not None else None,
                    "yes_book_ask": round(_book.best_ask, 4) if _book and _book.best_ask is not None else None,
                    "pm_rebates_earned": round(pos.pm_rebates_earned, 4),
                    "est_close_pnl": _est_close_pnl,
                    "strategy": pos.strategy,
                    "venue": "PM",
                    "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
                    # Current active quote fill state (null when no resting order)
                    # order_id: the PM order ID of the last order that filled into this position.
                    "order_id": pos.order_id or None,
                    # Live resting-order fill state split by side:
                    #   YES positions fill from the BID quote; NO positions from the ASK quote.
                    "active_bid_original_ct": _bid_orig,
                    "active_bid_remaining_ct": _bid_rem,
                    "active_bid_filled_ct": _bid_fill,
                    "active_ask_original_ct": _ask_orig,
                    "active_ask_remaining_ct": _ask_rem,
                    "active_ask_filled_ct": _ask_fill,
                    "signal_score": pos.signal_score,
                    # Legacy field — null for all new positions
                    "spread_id": pos.spread_id,
                    "strike": pos.strike if pos.strike else None,
                    # GTD hedge (momentum strategy only)
                    "hedge_order_id": pos.hedge_order_id or None,
                    "hedge_token_id": pos.hedge_token_id or None,
                    "hedge_price": pos.hedge_price if pos.hedge_price else None,
                    "hedge_size_usd": pos.hedge_size_usd if pos.hedge_size_usd else None,
                    # Closed-position visibility (present only when recently closed)
                    "is_closed": pos.is_closed,
                    "closed_at": pos.closed_at.isoformat() if pos.closed_at else None,
                    "realized_pnl": round(pos.realized_pnl, 4) if pos.is_closed else None,
                }
            # NOTE: api_state.positions is assigned once after HL hedges are
            # also added below — a single atomic dict reference swap avoids
            # readers ever seeing a half-built state (A3 fix).

            # Pin only tokens for currently open positions so the WS subscription
            # filters (TTE horizon, bucket-started, volume) continue to apply to
            # all other markets (A2 fix — was: pin all tracked tokens).
            position_tokens = {
                mkt.token_id_yes
                for pos in positions_snap.values()
                if not pos.is_closed
                for mkt in [markets_snap.get(pos.market_id)]
                if mkt is not None
            }
            if position_tokens:
                pm.pin_tokens(position_tokens)

            # HL delta hedges from maker (coin-level, keyed as "hl_hedge_{coin}")
            for coin, hedge in coin_hedges_snap.items():
                hl_key = f"hl_hedge_{coin}"
                notional = round(hedge["size"] * hedge["price"], 2)
                positions_raw[hl_key] = {
                    "condition_id": hl_key,
                    "market_title": f"{coin} Perp",
                    "underlying": coin,
                    "side": hedge["direction"],   # "SHORT" or "LONG"
                    "size_usd": notional,
                    "entry_price": hedge["price"],
                    "strategy": "maker_hedge",
                    "venue": "HL",
                    "opened_at": None,
                    "hl_size_coins": round(hedge["size"], 6),
                }
            api_state.positions = positions_raw
            markets_raw = {}
            for cid, mkt in markets_snap.items():
                book_yes = pm.get_book(mkt.token_id_yes)
                markets_raw[cid] = {
                    "condition_id": mkt.condition_id,
                    "title": mkt.title,
                    "market_type": mkt.market_type,
                    "underlying": mkt.underlying,
                    "fees_enabled": mkt.fees_enabled,
                    "token_id_yes": mkt.token_id_yes,
                    "market_slug": mkt.market_slug,
                    "end_date": mkt.end_date.isoformat() if mkt.end_date else None,
                    # PM orderbook fallback — populated from live WS snapshots
                    "yes_book_bid": book_yes.best_bid if book_yes else None,
                    "yes_book_ask": book_yes.best_ask if book_yes else None,
                    "yes_book_ts":  book_yes.timestamp  if book_yes else None,
                    "spot_mid": spot_client.get_mid(mkt.underlying, mkt.market_type),
                }
            api_state.markets = markets_raw

            # Data quality summary — counts of stale/missing book snapshots
            _now_ts = time.time()
            _fresh = sum(
                1 for m in markets_raw.values()
                if m.get("yes_book_ts") and _now_ts - m["yes_book_ts"] <= 30
            )
            _stale = sum(
                1 for m in markets_raw.values()
                if m.get("yes_book_ts") and _now_ts - m["yes_book_ts"] > 30
            )
            _no_book = sum(1 for m in markets_raw.values() if not m.get("yes_book_ts"))
            api_state.data_quality = {
                "sub_token_count":    getattr(pm, "sub_token_count", 0),
                "sub_rejected_count": getattr(pm, "sub_rejected_count", 0),
                "market_count":       len(markets_raw),
                "fresh_book_count":   _fresh,
                "stale_book_count":   _stale,
                "no_book_count":      _no_book,
                "spot_mids":   spot_client.all_mids(),
                "spot_ages_s": {
                    coin: round(spot_client.get_spot_age_rtds(coin), 1)
                    for coin in sorted(spot_client.tracked_coins)
                },
            }
            quotes_raw = {}
            for tid, q in active_quotes_snap.items():
                mkt_info = markets_snap.get(q.market_id)
                quotes_raw[tid] = {
                    "market_id": q.market_id,
                    "token_id": q.token_id,
                    "side": q.side,
                    "price": q.price,
                    "size": q.size,
                    "order_id": q.order_id,
                    "posted_at": q.posted_at,
                    "market_title": mkt_info.title if mkt_info else q.market_id,
                    "underlying": mkt_info.underlying if mkt_info else "UNKNOWN",
                    "score": q.score,
                }
            api_state.active_quotes = quotes_raw

            # Signals from maker strategy
            api_state.maker_signals = maker.get_signals()

            # Funding from hl_client
            funding_raw = {}
            for coin, snap in funding_snap.items():
                funding_raw[coin] = {
                    "predicted_rate": snap.hl_predicted,
                    "binance_predicted": snap.binance_predicted,
                    "bybit_predicted": snap.bybit_predicted,
                    "fetched_at": snap.timestamp,
                }
            api_state.funding = funding_raw

            # Agent shadow log
            api_state.agent_shadow_log = agent.get_shadow_log()[-100:]

            # ── Broadcast SSE + event-driven sleep ────────────────────────────
            # Push a state bundle to all connected SSE clients so the frontend
            # receives live updates without polling individual REST endpoints.
            try:
                await api_server.broadcast_sse(api_server.build_live_state())
            except Exception as _sse_exc:
                log.debug("SSE broadcast error", exc=str(_sse_exc))

        except Exception as exc:
            log.error("State sync error", exc=str(exc))

        # Wait for a state-change event (position open/close, signal), or the
        # 1 s backstop — whichever comes first.  This ensures the webapp stays
        # fresh within ≤1 s even without explicit events while removing the
        # hard 5 s delay between state updates.
        try:
            await asyncio.wait_for(_state_changed.wait(), timeout=1.0)
            _state_changed.clear()
        except asyncio.TimeoutError:
            pass


# ── Graceful shutdown ─────────────────────────────────────────────────────────

def _install_signal_handlers() -> None:
    """Install OS signal handlers for clean shutdown."""
    loop = asyncio.get_event_loop()

    def _handle_signal(signum, frame):
        log.info("Shutdown signal received", signum=signum)
        _shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (OSError, ValueError):
            pass  # Windows may not support all signals


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    """
    Bootstrap all components and run until shutdown.
    """
    log.info(
        "Bot starting",
        paper_trading=config.PAPER_TRADING,
        agent_auto=config.AGENT_AUTO,
        model=config.AGENT_MODEL,
    )

    # Log the fully-resolved runtime config so the exact values in use are
    # always visible in bot.log without needing to read two separate files.
    from config import get_effective_config
    _eff = get_effective_config()
    log.info("Effective config (defaults + overrides)", **_eff)

    if config.PAPER_TRADING:
        log.warning("PAPER TRADING mode — no real orders will be placed")
    else:
        # Live mode: refuse to start with missing credentials
        missing = []
        if not config.POLY_PRIVATE_KEY:
            missing.append("POLY_PRIVATE_KEY")
        if config.MAKER_HEDGE_ENABLED:
            if not config.HL_ADDRESS:
                missing.append("HL_ADDRESS")
            if not config.HL_SECRET_KEY:
                missing.append("HL_SECRET_KEY")
        if missing:
            log.critical(
                "Live trading requires credentials — set these env vars or keep PAPER_TRADING=True",
                missing=missing,
            )
            sys.exit(1)

    _install_signal_handlers()

    # ── Instantiate all components ───────────────────────────────────────────
    risk_engine = RiskEngine()
    pm = PMClient()
    hl = HLClient()
    # Spot price source: RTDS (crypto_prices) for 1h/daily/weekly markets;
    # on-chain Chainlink HTTP polling for 5m/15m/4h markets.
    # SpotOracle facade routes get_mid/get_spot/get_spot_age to the right client.
    spot_client = RTDSClient()
    # Chainlink oracles — event-driven, zero polling:
    #   ChainlinkWSClient: AnswerUpdated events on Polygon for BTC/ETH/SOL/XRP/BNB/DOGE
    #   ChainlinkStreamsClient: direct Data Streams WebSocket for HYPE/USD (requires
    #     free sponsored key from https://pm-ds-request.streams.chain.link/)
    # SpotOracle picks the freshest snapshot from both HYPE feeds automatically.
    chainlink_ws = ChainlinkWSClient()
    chainlink_streams = ChainlinkStreamsClient()
    spot_oracle = SpotOracle(spot_client, chainlink_ws, chainlink_streams)
    # Enable raw oracle tick CSV logging (data/oracle_ticks.csv).
    # Records every oracle event from all sources regardless of open positions.
    # Used for post-trade analysis, feed-liveness checks, and inter-feed latency.
    spot_oracle.enable_oracle_tick_log()
    maker = MakerStrategy(pm, hl, risk_engine, spot_client=spot_oracle)
    scanner = MispricingScanner(pm, hl, _on_mispricing_signal, scan_interval=config.MISPRICING_SCAN_INTERVAL, spot_client=spot_oracle)
    agent = AgentDecisionLayer(risk_engine)
    # Strategy 3 — Momentum scanner (direct execution, no agent loop)
    vol_fetcher = VolFetcher()
    vol_fetcher.register(spot_client)   # registers RTDS spot price callback for rolling realized-vol buffer
    def _on_momentum_signal(sig_dict: dict) -> None:
        api_state.momentum_signals.append(sig_dict)
        # Cap in-memory list to last 200 signals
        if len(api_state.momentum_signals) > 200:
            api_state.momentum_signals = api_state.momentum_signals[-200:]
        notify_state_changed()

    momentum_scanner = MomentumScanner(pm, hl, risk_engine, vol_fetcher, spot_client=spot_oracle, on_signal=_on_momentum_signal)

    def _on_position_close(market_id: str) -> None:
        """Notify both strategy scanners when any position closes.

        Mispricing scanner: resets its cooldown clock so the same market can
        be re-evaluated immediately after a round-trip.
        Momentum scanner: resets the per-market cooldown so the full
        MOMENTUM_MARKET_COOLDOWN_SECONDS window restarts from close, not from
        the original signal fire (which expires in 5 s and was the root cause
        of systematic re-entry churn).
        """
        scanner.record_trade_close(market_id)
        momentum_scanner.record_trade_close(market_id)
        notify_state_changed()

    def _on_momentum_stop_loss(market_id: str, tte_remaining: float) -> None:
        """Block re-entry into a market for the rest of its window after a stop-loss.

        Prevents the scanner from immediately re-entering the same expiry bucket
        after being stopped out, which compounds losses when the market continues
        moving against the original signal.
        """
        momentum_scanner.record_stop_loss_close(market_id, tte_remaining)

    monitor = PositionMonitor(
        pm, risk_engine,
        spot_client=spot_oracle,
        on_close_callback=_on_position_close,
        on_stop_loss_callback=_on_momentum_stop_loss,
    )
    fill_sim = FillSimulator(pm, maker, risk_engine, monitor)
    live_fill_handler = LiveFillHandler(pm, maker, risk_engine, monitor)

    # Expose references to API action endpoints (manual close, etc.)
    api_state.monitor_ref = monitor
    api_state.pm_ref = pm
    api_state.risk_ref = risk_engine
    api_state.momentum_ref = momentum_scanner

    # ── Connect clients ──────────────────────────────────────────────────────
    log.info("Connecting PM client…")
    await pm.start()

    log.info("Connecting HL client…")
    await hl.start()

    log.info("Connecting RTDS spot client…")
    await spot_client.start()

    log.info("Starting Chainlink WS client (AggregatorV3 events)…")
    await chainlink_ws.start()

    log.info("Starting Chainlink Streams client (HYPE/USD direct feed)…")
    await chainlink_streams.start()

    # In live mode: cancel any stale open orders and restore existing positions
    # before the maker strategy begins quoting.  No-op in paper mode.
    if not config.PAPER_TRADING:
        await live_fill_handler.startup_restore()
    await live_fill_handler.start()  # registers fill callback (no-op in paper mode)

    # ── Start all tasks ──────────────────────────────────────────────────────
    tasks = [
        asyncio.create_task(maker.start(), name="maker"),
        asyncio.create_task(scanner.start(), name="scanner"),
        asyncio.create_task(momentum_scanner.start(), name="momentum"),
        asyncio.create_task(
            agent_loop(pm, agent, monitor, risk_engine),
            name="agent_loop",
        ),
        asyncio.create_task(monitor.start(), name="monitor"),
        asyncio.create_task(
            state_sync_loop(pm, hl, maker, agent, risk_engine, spot_oracle),
            name="state_sync",
        ),
        asyncio.create_task(
            api_server.run_api_server(port=config.API_PORT),
            name="api_server",
        ),
    ]
    if config.PAPER_TRADING:
        tasks.append(asyncio.create_task(fill_sim.start(), name="fill_simulator"))

    log.info(
        "All tasks running",
        tasks=[t.get_name() for t in tasks],
        api_port=config.API_PORT,
    )

    # ── Wait for shutdown ────────────────────────────────────────────────────
    await _shutdown_event.wait()
    log.info("Shutdown initiated — cancelling tasks…")

    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    # Final cleanup
    await pm.stop()
    await hl.stop()

    log.info("Bot shut down cleanly")


if __name__ == "__main__":
    asyncio.run(main())
