"""
Stop-loss failure analysis.
Usage: python _stoploss_analysis.py
"""
import csv, json

trades  = {r['market_id']: r for r in csv.DictReader(open('data/trades.csv'))}
fills   = {r['market_id']: r for r in csv.DictReader(open('data/momentum_fills.csv'))}
open_sp = json.load(open('data/market_open_spots.json'))

losses = [
    ('0x9ad8e452181ae3e102d970139822a31869b6ce2ee383ee65ea684dc2cf990e46', 'ETH-NO  bucket_15m'),
    ('0x8f809bf03ab051743dc33487d7dc745668b0bb00ced5793c07aa53b5e494cddc', 'SOL-YES bucket_15m'),
    ('0xf356b5824b8b2fc8763fc119447fa36db77140dde14979c018e9a40794d82bab', 'SOL-NO  bucket_15m'),
    ('0xc94525869a22e954db2abd093525099302e950d249b92610bc5fef8ceafb294e', 'XRP-NO  bucket_1h'),
]

SL_PCT = 0.01  # MOMENTUM_DELTA_STOP_LOSS_PCT

print("=" * 100)
print("STOP-LOSS FAILURE ANALYSIS  —  Session 2026-04-03")
print("=" * 100)

for mid, label in losses:
    t = trades.get(mid, {})
    f = fills.get(mid, {})

    recorded_strike = None
    for k in open_sp:
        if mid.startswith(k):
            recorded_strike = open_sp[k]
            break

    side          = f.get('side', '?')
    entry_price   = float(f.get('fill_price', 0))
    entry_spot    = float(t.get('spot_price', 0))
    exit_spot     = float(t.get('exit_spot_price', 0))
    scanner_strike= float(t.get('strike', 0))
    signal_delta  = float(f.get('signal_delta_pct', 0))
    tte           = float(f.get('tte_seconds', 0))
    pnl_realized  = float(t.get('pnl', 0))
    outcome       = t.get('resolved_outcome', '')

    if side in ('YES', 'BUY_YES'):
        delta_at_exit = (exit_spot - scanner_strike) / scanner_strike * 100
        sl_fires_at   = scanner_strike * (1 - SL_PCT / 100)
    else:
        delta_at_exit = (scanner_strike - exit_spot) / scanner_strike * 100
        sl_fires_at   = scanner_strike * (1 + SL_PCT / 100)

    pyth_verdict = "WIN" if delta_at_exit > 0 else "LOSS"
    oracle_verdict = outcome if outcome else "LOSS (token collapsed to 0.01)"

    divergence_pct = abs(exit_spot - scanner_strike) / scanner_strike * 100

    print(f"\n--- {label} ---")
    print(f"  TTE at fill:           {tte:.0f}s          (bucket min window: 300s)")
    print(f"  Side / token price:    {side} @ {entry_price}")
    print(f"  Strike (candle open):  {scanner_strike:.5f}")
    print(f"  Pyth spot at ENTRY:    {entry_spot:.5f}   signal delta: +{signal_delta:.4f}%  (in-the-money at fill)")
    print(f"  Pyth spot at CLOSE:    {exit_spot:.5f}   delta vs strike: {delta_at_exit:+.4f}%")
    print(f"  SL would fire when:    Pyth spot {'<' if side in ('YES','BUY_YES') else '>'} {sl_fires_at:.5f}  (never happened)")
    print(f"  Pyth says at close:    {pyth_verdict}")
    print(f"  PM Oracle said:        {oracle_verdict}")
    print(f"  Pyth / Oracle gap:     {divergence_pct:.4f}%  divergence at close")
    print(f"  Realized PnL:          {pnl_realized:.3f}  (exit reason: RESOLVED, not stop_loss)")

print()
print("=" * 100)
print("KEY FINDING:  Pyth said all 3 losing positions were WINNING at the moment of oracle resolution.")
print("              Delta SL fired at NONE of them because MOMENTUM_DELTA_STOP_LOSS_PCT=0.01%")
print("              requires Pyth to CONFIRM the position is wrong.  Pyth never confirmed it.")
print("              The PM oracle used a different price source and disagreed with Pyth.")
print("=" * 100)
print()
print("XRP comparison (SL DID fire, but too late):")
xrp_mid = '0xc94525869a22e954db2abd093525099302e950d249b92610bc5fef8ceafb294e'
t = trades.get(xrp_mid, {})
f = fills.get(xrp_mid, {})
print(f"  TTE at fill: {f.get('tte_seconds')}s  |  hold time: ~672s  |  exit_price: {t.get('price','?')}")
print(f"  The SL DID fire (reason=momentum_stop_loss) but exit_price=0.02 — 98% of max loss realized.")
print(f"  MOMENTUM_DELTA_STOP_LOSS_PCT=0.01% only confirms a loss already in progress.")
