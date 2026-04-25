"""
pm_reconcile.py — Reconcile trades.csv against Polymarket Data API.

PM's data-api.polymarket.com/activity is the on-chain source of truth:
  • BUY  events record the actual USDC spent and contracts received
  • SELL events record the actual USDC received from a taker exit
  • REDEEM events record the settlement proceeds (1.0 per contract for winners)

The bot's internal recording has two known failure modes:

  1. Entry price recorded as order price (e.g. 0.68), not actual CLOB fill
     price (e.g. 0.49).  Market FAK orders sweep the best ask, which may be
     well below the order's ceiling price.

  2. TP-sell exits not detected: the scanner places a limit_taker_fak SELL
     that fills while the bot is still tracking the position as open.  Later,
     when the market resolves, the monitor's RESOLVED path closes it at
     exit_price=1.0 and resolved_outcome=WIN — even if the TP sold for a loss.

reconcile_trades_csv() corrects both bugs by patching trades.csv in-place:
  • price         → actual entry price from PM BUY event
  • pnl           → actual net (sell_usdc + redeem_usdc − buy_usdc)
  • resolved_outcome → "WIN"/"LOSS" for REDEEM exits; "" for taker SELL exits
  • fees_paid / rebates_earned → zeroed (fees are embedded in PM USDC flows)

Only patches closed positions where both an entry (BUY) AND an exit
(SELL or REDEEM) are present in PM activity.  Open positions are skipped.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

import httpx

import config
from logger import get_bot_logger

log = get_bot_logger(__name__)

_DATA_DIR   = Path(__file__).parent / "data"
_TRADES_CSV = _DATA_DIR / "trades.csv"

# ── Side / outcome mapping ─────────────────────────────────────────────────────

_SIDE_TO_PM_OUTCOMES: dict[str, list[str]] = {
    "UP":   ["Up"],
    "DOWN": ["Down"],
    "YES":  ["Yes"],
    "NO":   ["No"],
}


def _side_matches_outcome(side: str, outcome: str) -> bool:
    return outcome in _SIDE_TO_PM_OUTCOMES.get(side.upper(), [])


# ── PM Activity fetch ──────────────────────────────────────────────────────────

async def fetch_pm_activity(funder: str, limit: int = 500) -> list[dict]:
    """Fetch recent TRADE/REDEEM activity from data-api.polymarket.com."""
    url = f"https://data-api.polymarket.com/activity?user={funder}&limit={limit}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("value", [])


def group_by_condition(rows: list[dict]) -> dict[str, list[dict]]:
    """Group PM activity rows by conditionId."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        cid = r.get("conditionId", "")
        if cid:
            groups.setdefault(cid, []).append(r)
    return groups


# ── Correction logic ───────────────────────────────────────────────────────────

def _compute_correction(trade_row: dict, pm_market_rows: list[dict]) -> Optional[dict]:
    """
    Given a trades.csv row and all PM activity for the same conditionId,
    return a dict of field corrections to apply (may be empty → None).

    Accounting:
        actual_entry = sum(BUY usdcSize) / sum(BUY size)
        actual_pnl   = sum(SELL usdcSize) + sum(REDEEM usdcSize) - sum(BUY usdcSize)

    PM's usdcSize already embeds taker fees — no separate adjustment needed.
    """
    strategy = trade_row.get("strategy", "")
    if strategy == "momentum_hedge":
        return None  # hedge rows have separate P&L semantics; skip

    side = (trade_row.get("side") or "").upper()
    if side in ("HEDGE", ""):
        return None

    # Filter PM rows by event type and matching side/outcome
    buy_rows = [
        r for r in pm_market_rows
        if r.get("type") == "TRADE"
        and r.get("side") == "BUY"
        and _side_matches_outcome(side, r.get("outcome", ""))
    ]
    sell_rows = [
        r for r in pm_market_rows
        if r.get("type") == "TRADE"
        and r.get("side") == "SELL"
        and _side_matches_outcome(side, r.get("outcome", ""))
    ]
    # REDEEMs carry no outcome field — they could belong to either the main
    # position's token or the hedge token (same conditionId, opposite tokenId).
    # When the bot placed a GTD hedge (BUY on opposite side), the hedge token
    # may have won and been redeemed while the main token expired worthless.
    # Attributing that REDEEM to the main position inflates PnL by the full
    # redemption amount (Bug #1 Apr-23 post-mortem).
    #
    # Detection: if opposite-side BUY rows exist AND total REDEEM size ≈ opposite
    # BUY size (within 5%), the REDEEM belongs to the hedge token — exclude it
    # from main position P&L so actual_pnl = sell_usdc - buy_usdc (a loss).
    _all_redeem_rows = [r for r in pm_market_rows if r.get("type") == "REDEEM"]
    _opp_outcomes: dict[str, list[str]] = {
        "UP": ["Down"], "DOWN": ["Up"], "YES": ["No"], "NO": ["Yes"],
    }
    _opp_buy_rows = [
        r for r in pm_market_rows
        if r.get("type") == "TRADE"
        and r.get("side") == "BUY"
        and r.get("outcome", "") in _opp_outcomes.get(side, [])
    ]
    if _opp_buy_rows and _all_redeem_rows:
        _opp_buy_size = sum(float(r.get("size") or 0) for r in _opp_buy_rows)
        _tot_redeem_size = sum(float(r.get("size") or 0) for r in _all_redeem_rows)
        if _opp_buy_size > 0 and abs(_tot_redeem_size - _opp_buy_size) / _opp_buy_size < 0.05:
            # REDEEM size matches hedge BUY size — REDEEMs belong to hedge token.
            # Exclude from main position PnL; main expired worthless (LOSS).
            redeem_rows: list[dict] = []
        else:
            redeem_rows = _all_redeem_rows
    else:
        redeem_rows = _all_redeem_rows

    if not buy_rows:
        return None  # no PM entry data — cannot reconcile

    # Require an exit before patching; open positions are intentionally skipped.
    # Use _all_redeem_rows here so that a hedge REDEEM (same conditionId) is
    # still treated as an exit signal even when redeem_rows was cleared above.
    if not sell_rows and not _all_redeem_rows:
        return None

    # ── Actual entry price from BUY events ────────────────────────────────────
    total_buy_usdc = sum(float(r.get("usdcSize") or 0) for r in buy_rows)
    total_buy_size = sum(float(r.get("size")    or 0) for r in buy_rows)
    if total_buy_size <= 0:
        return None
    actual_entry_price = total_buy_usdc / total_buy_size

    # ── Actual PnL from USDC flows ────────────────────────────────────────────
    total_sell_usdc   = sum(float(r.get("usdcSize") or 0) for r in sell_rows)
    total_redeem_usdc = sum(float(r.get("usdcSize") or 0) for r in redeem_rows)
    actual_pnl = (total_sell_usdc + total_redeem_usdc) - total_buy_usdc

    # ── Determine resolved_outcome ────────────────────────────────────────────
    if redeem_rows:
        total_redeem_size = sum(float(r.get("size") or 0) for r in redeem_rows)
        if total_redeem_size > 0:
            rate = total_redeem_usdc / total_redeem_size
            correct_outcome = "WIN" if rate > 0.5 else "LOSS"
        else:
            # size=0 REDEEM = loser tokens redeemed for $0
            correct_outcome = "LOSS"
    elif _all_redeem_rows:
        # REDEEMs exist but were attributed to the hedge token (excluded above).
        # Main position's token expired worthless — unambiguous LOSS.
        correct_outcome = "LOSS"
    else:
        # Pure taker exit (TP or SL) — no market resolution, no WIN/LOSS label
        correct_outcome = ""

    # ── Compare against current recorded values ───────────────────────────────
    try:
        current_price   = float(trade_row.get("price") or 0)
        current_pnl     = float(trade_row.get("pnl")   or 0)
    except (ValueError, TypeError):
        return None

    current_outcome = (trade_row.get("resolved_outcome") or "").strip()
    _blank = {"", "nan", "None", "none"}

    corrections: dict = {}

    # Entry price: correct if off by more than 0.5¢
    if abs(actual_entry_price - current_price) > 0.005:
        corrections["price"] = round(actual_entry_price, 8)

    # PnL: correct if off by more than 1¢.
    # When PnL changes, also zero fees/rebates — PM USDC flows already include
    # taker fees, so separate fee fields would double-count them.
    if abs(actual_pnl - current_pnl) > 0.01:
        corrections["pnl"]              = round(actual_pnl, 10)
        corrections["fees_paid"]         = "0.0"
        corrections["rebates_earned"]    = "0.0"

    # resolved_outcome: fix incorrect WIN/LOSS for taker exits, or wrong resolution label
    if correct_outcome and current_outcome != correct_outcome:
        corrections["resolved_outcome"] = correct_outcome
    elif correct_outcome == "" and current_outcome not in _blank:
        # Taker exit incorrectly labelled WIN or LOSS — clear it
        corrections["resolved_outcome"] = ""

    return corrections if corrections else None


# ── Main entry point ───────────────────────────────────────────────────────────

async def reconcile_trades_csv(
    funder: str,
    *,
    csv_path: Path = _TRADES_CSV,
    pm_limit: int = 500,
) -> dict:
    """Fetch PM activity and patch trades.csv with actual fill prices and PnL.

    Returns:
        { "patched": int, "markets": list[dict], "errors": list[str] }
    """
    if not funder:
        return {"patched": 0, "markets": [], "errors": ["POLY_FUNDER not configured"]}

    try:
        pm_rows = await fetch_pm_activity(funder, limit=pm_limit)
    except Exception as exc:
        return {"patched": 0, "markets": [], "errors": [f"PM fetch failed: {exc}"]}

    if not pm_rows:
        return {"patched": 0, "markets": [], "errors": ["PM returned no activity"]}

    pm_by_cond = group_by_condition(pm_rows)

    if not csv_path.exists():
        return {"patched": 0, "markets": [], "errors": ["trades.csv not found"]}

    try:
        from risk import TRADES_HEADER
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if rows and "timestamp" not in rows[0]:
            with csv_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f, fieldnames=TRADES_HEADER))
    except Exception as exc:
        return {"patched": 0, "markets": [], "errors": [f"CSV read failed: {exc}"]}

    from risk import TRADES_HEADER  # noqa: F811 (re-import after try/except)

    patched_count   = 0
    patched_markets: list[dict] = []
    errors: list[str] = []

    for row in rows:
        market_id = row.get("market_id", "")
        if not market_id:
            continue
        pm_market_rows = pm_by_cond.get(market_id)
        if not pm_market_rows:
            continue

        corrections = _compute_correction(row, pm_market_rows)
        if not corrections:
            continue

        log.info(
            "pm_reconcile: patching row",
            market_id=market_id[:24],
            market_title=(row.get("market_title") or "")[:50],
            side=row.get("side"),
            corrections={k: f"{row.get(k)!r} → {v!r}" for k, v in corrections.items()},
        )
        row.update(corrections)
        patched_count += 1
        patched_markets.append({
            "market_id":    market_id,
            "market_title": row.get("market_title", ""),
            "side":         row.get("side", ""),
            "corrections":  {k: str(v) for k, v in corrections.items()},
        })

    if patched_count == 0:
        return {"patched": 0, "markets": [], "errors": errors}

    # Atomically rewrite trades.csv via a .tmp sibling (mirrors patch_trade_outcome)
    try:
        tmp = csv_path.with_suffix(".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADES_HEADER)
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(csv_path)
        log.info("pm_reconcile: trades.csv rewritten", patched=patched_count)
    except Exception as exc:
        errors.append(f"CSV write failed: {exc}")
        log.error("pm_reconcile: rewrite failed", exc=str(exc))
        return {"patched": 0, "markets": [], "errors": errors}

    return {"patched": patched_count, "markets": patched_markets, "errors": errors}
