"""
audit_trades.py — Cross-checks trades.csv against fills.csv.
Run: python audit_trades.py
"""
import csv
from collections import defaultdict

TRADES_FILE = "data/trades.csv"
FILLS_FILE  = "data/fills.csv"

# ── Load data ──────────────────────────────────────────────────────────────
trades = []
with open(TRADES_FILE) as f:
    for row in csv.DictReader(f):
        trades.append(row)

fills = []
with open(FILLS_FILE) as f:
    for row in csv.DictReader(f):
        fills.append(row)

# ── Aggregate fills by (market_id, position_side) ──────────────────────────
fill_groups = defaultdict(list)
for frow in fills:
    key = (frow["market_id"], frow["position_side"])
    fill_groups[key].append(frow)

fill_summary = {}
for key, rows in fill_groups.items():
    total_ct = sum(float(r["contracts_filled"]) for r in rows)
    total_cost = sum(float(r["fill_price"]) * float(r["contracts_filled"]) for r in rows)
    total_rebate = sum(float(r["rebate_usd"]) for r in rows)
    avg_price = total_cost / total_ct if total_ct else 0.0
    fill_summary[key] = {
        "total_ct": total_ct,
        "avg_price": avg_price,
        "total_rebate": total_rebate,
        "count": len(rows),
    }

# ── Group trades by market ─────────────────────────────────────────────────
market_trades = defaultdict(list)
for t in trades:
    market_trades[t["market_id"]].append(t)

total_pnl = 0.0
issues = []

print("TRADE AUDIT REPORT — 2026-03-20")
print("=" * 110)

for market_id, mktrades in sorted(market_trades.items(), key=lambda x: x[1][0]["timestamp"]):
    title     = mktrades[0]["market_title"]
    mtype     = mktrades[0]["market_type"]
    underlying = mktrades[0]["underlying"]
    print(f"\n{'─' * 110}")
    print(f"Market : {title}")
    print(f"Type   : {mtype}  |  Underlying: {underlying}  |  ID: {market_id[:22]}...")

    market_pnl = 0.0
    sides_pnl = {}

    for t in sorted(mktrades, key=lambda x: x["side"]):
        side    = t["side"]
        size    = float(t["size"])
        entry   = float(t["price"])
        pnl     = float(t["pnl"])
        fees    = float(t["fees_paid"])
        rebates = float(t["rebates_earned"])
        score   = t.get("signal_score", "?")

        # Reverse-engineer the implied exit price from the P&L formula:
        #   YES: pnl = (exit - entry) * size - fees + rebates  →  exit = entry + (pnl+fees-rebates)/size
        #   NO:  pnl = -(exit - entry) * size - fees + rebates →  exit = entry - (pnl+fees-rebates)/size
        gross = pnl + fees - rebates
        if side == "YES":
            implied_exit = entry + gross / size if size else 0.0
        else:
            implied_exit = entry - gross / size if size else 0.0

        snap_dist = abs(implied_exit - round(implied_exit))
        if snap_dist < 0.015:
            settled     = int(round(implied_exit))
            exit_label  = f"RESOLVED → {settled}  (mid snap)"
        else:
            exit_label  = f"TAKER EXIT @ {implied_exit:.4f}"

        # Cross-check with fills
        fi = fill_summary.get((market_id, side))
        if fi:
            sdiff = abs(fi["total_ct"] - size)
            pdiff = abs(fi["avg_price"] - entry)
            rdiff = abs(fi["total_rebate"] - rebates)
            s_ok = "Sz:OK" if sdiff < 0.02    else f"Sz:MISMATCH(fills={fi['total_ct']:.3f} vs {size:.3f})"
            p_ok = "Px:OK" if pdiff < 0.002   else f"Px:MISMATCH(fills={fi['avg_price']:.4f} vs {entry:.4f})"
            r_ok = "Rb:OK" if rdiff < 0.0005  else f"Rb:MISMATCH(fills={fi['total_rebate']:.6f} vs {rebates:.6f})"
        else:
            s_ok, p_ok, r_ok = "Sz:NO-FILLS", "Px:NO-FILLS", "Rb:NO-FILLS"

        flag_parts = []
        if "MISMATCH" in s_ok:
            flag_parts.append("SZ-MIS")
            issues.append(f"Size mismatch  : {title[:45]} {side}")
        if "MISMATCH" in p_ok:
            flag_parts.append("PX-MIS")
            issues.append(f"Price mismatch : {title[:45]} {side}")
        if "NO-FILLS" in s_ok:
            flag_parts.append("NO-FILL")
            issues.append(f"No fill records: {title[:45]} {side}")

        flag_str = ("  !!! " + " ".join(flag_parts)) if flag_parts else ""
        win      = "+" if pnl >= 0 else "-"
        print(
            f"  [{win}] {side:3s}: {size:7.3f}ct @ {entry:.4f}  |  {exit_label:38s}  |  "
            f"P&L: ${pnl:+9.4f}  |  Score:{score:5s}  |  {s_ok}  {p_ok}  {r_ok}{flag_str}"
        )

        market_pnl += pnl
        total_pnl  += pnl
        sides_pnl[side] = pnl

    # Spread analysis (if both YES+NO exist)
    if "YES" in sides_pnl and "NO" in sides_pnl:
        combined  = sides_pnl["YES"] + sides_pnl["NO"]
        both_neg  = sides_pnl["YES"] < 0 and sides_pnl["NO"] < 0
        spread_flag = "  !!!! BOTH LEGS NEGATIVE — likely resolution snap bug" if both_neg else ""
        print(f"  {'':50s} Spread P&L: ${combined:+9.4f}{spread_flag}")
        if both_neg:
            issues.append(f"BOTH-LEGS-NEG  : {title}")

    print(f"  {'':50s} Market P&L: ${market_pnl:+9.4f}")

# ── Summary ────────────────────────────────────────────────────────────────
print(f"\n{'=' * 110}")
print(f"SESSION TOTALS")
print(f"  Trades : {len(trades)} records across {len(market_trades)} markets")
print(f"  Fills  : {len(fills)} fill events")
print(f"  Total P&L: ${total_pnl:+.4f}")
print()
if issues:
    print(f"ISSUES FOUND ({len(issues)}):")
    for i in issues:
        print(f"  !!  {i}")
else:
    print("No issues found — all trades cross-check cleanly.")

# ── Open positions (fills without a closed trade) ─────────────────────────
print()
print("=" * 110)
print("OPEN POSITIONS  (fills logged but no closed trade record yet)")
print("=" * 110)

closed_mids = {t["market_id"] for t in trades}
open_groups = defaultdict(list)
for frow in fills:
    if frow["market_id"] not in closed_mids:
        key = (frow["market_id"], frow["market_title"][:52], frow["position_side"])
        open_groups[key].append(frow)

if not open_groups:
    print("  (none)")
else:
    total_open_exposure = 0.0
    for (mid, title, side), rows in sorted(open_groups.items(), key=lambda x: x[0][1]):
        total_ct  = sum(float(r["contracts_filled"]) for r in rows)
        total_cost = sum(float(r["fill_price"]) * float(r["contracts_filled"]) for r in rows)
        total_rebate = sum(float(r["rebate_usd"]) for r in rows)
        avg_px    = total_cost / total_ct if total_ct else 0.0
        fills_n   = len(rows)
        # Cost basis: YES→ avg_px*ct; NO (SELL YES)→ (1-avg_px)*ct as YES-equivalent exposure
        exposure  = avg_px * total_ct if side == "YES" else (1.0 - avg_px) * total_ct
        total_open_exposure += exposure
        print(
            f"  {side:3s} | {total_ct:7.3f}ct @ {avg_px:.4f} avg | "
            f"exposure: ${exposure:7.2f} | fills:{fills_n:3d} | "
            f"rebate: ${total_rebate:.4f} | {title}"
        )
    print()
    print(f"  Total open exposure (cost basis): ${total_open_exposure:.2f}")
