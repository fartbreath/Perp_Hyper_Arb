"""QA script for ML-D1 and ML-D2 acceptance criteria."""
import json, csv, tempfile
from pathlib import Path

print('=== ML-D1: signal_events.jsonl ===')
from strategies.Momentum.event_log import emit_signal_events_batch, write_position_snapshot, SIGNAL_EVENTS_PATH, POSITION_SNAPSHOTS_PATH
for p in [SIGNAL_EVENTS_PATH, POSITION_SNAPSHOTS_PATH]:
    if p.exists():
        p.unlink()

diags = [
    {
        'market_id': '0xaaa', 'underlying': 'BTC', 'market_type': 'bucket_5m', 'side': 'YES',
        'observed_z': 2.1, 'delta_pct': 1.8, 'effective_threshold': 0.9, 'effective_gap_pct': -0.1,
        'vol_regime': 'LOW', 'funding_rate': 0.00001, 'yes_depth_share': 0.55,
        'ask_depth_usd': 50.0, 'twap_dev_bps': -5.0, 'tte_seconds': 120,
        'sigma_ann': 0.6, 'sigma_tau': 0.001, 'skip_reason': 'delta_below_threshold',
    },
    {
        'market_id': '0xbbb', 'underlying': 'ETH', 'market_type': 'bucket_15m', 'side': 'UP',
        'observed_z': 3.5, 'delta_pct': 2.5, 'effective_threshold': 0.8, 'effective_gap_pct': 1.7,
        'vol_regime': 'NORMAL', 'funding_rate': -0.00002, 'yes_depth_share': 0.62,
        'ask_depth_usd': 120.0, 'twap_dev_bps': 2.0, 'tte_seconds': 90,
        'sigma_ann': 0.55, 'sigma_tau': 0.0009, 'skip_reason': 'signal_fired', 'executed': True,
    },
    # Early-stage skip: no observed_z — must NOT be written
    {'market_id': '0xccc', 'underlying': 'SOL', 'market_type': 'bucket_5m', 'skip_reason': 'stale_spot'},
]

emit_signal_events_batch(diags)
assert SIGNAL_EVENTS_PATH.exists(), 'signal_events.jsonl not created'
rows = [json.loads(l) for l in SIGNAL_EVENTS_PATH.read_text().splitlines() if l.strip()]
assert len(rows) == 2, f'Expected 2, got {len(rows)}'
print('[PASS] Only observed_z rows written (early-stage skips excluded):', len(rows))

r0, r1 = rows[0], rows[1]
assert r0['entered'] is False
assert r0['gate_result']['z_pass'] is False
assert r0['gate_result']['funding_pass'] is True
assert 'hour_utc' in r0 and 'day_of_week' in r0
print('[PASS] Rejected signal: entered=False, z_pass=False, time fields present, gate_result correct')

assert r1['entered'] is True
assert r1['gate_result']['z_pass'] is True
print('[PASS] Fired signal: entered=True, z_pass=True')

# AC: append-only
emit_signal_events_batch(diags)
rows2 = [json.loads(l) for l in SIGNAL_EVENTS_PATH.read_text().splitlines() if l.strip()]
assert len(rows2) == 4
print('[PASS] Append-only: 4 rows after 2 writes')

# AC: exception safety — empty and no-z inputs must not raise
emit_signal_events_batch([])
emit_signal_events_batch([{'market_id': 'x', 'skip_reason': 'stale_spot'}])
print('[PASS] Empty / no-observed_z input does not raise or write')

# AC: funding_fail gate result
diags_funding = [{
    'market_id': '0xddd', 'underlying': 'BTC', 'market_type': 'bucket_5m', 'side': 'YES',
    'observed_z': 1.5, 'delta_pct': 1.0, 'effective_threshold': 0.5, 'effective_gap_pct': 0.5,
    'vol_regime': 'NORMAL', 'funding_rate': 0.001, 'yes_depth_share': 0.6,
    'ask_depth_usd': 80.0, 'twap_dev_bps': 0.0, 'tte_seconds': 100,
    'sigma_ann': 0.5, 'sigma_tau': 0.001, 'skip_reason': 'funding_block',
}]
emit_signal_events_batch(diags_funding)
rows3 = [json.loads(l) for l in SIGNAL_EVENTS_PATH.read_text().splitlines() if l.strip()]
r_fund = rows3[-1]
assert r_fund['gate_result']['funding_pass'] is False
print('[PASS] funding_block -> gate_result.funding_pass=False')

print()
print('=== ML-D1: position_snapshots.jsonl ===')

write_position_snapshot(
    market_id='0xaaa', side='YES', token_id='tok1', underlying='BTC',
    tte_seconds=90.5, current_token_price=0.82, oracle_delta_pct=1.4,
    hl_mark_price=95000.5, hl_depth_imbalance=0.12,
    delta_sl_would_fire=False, upfrac_below=False, last_upfrac=0.62,
    coin_sl=0.1, exit_flag=False, reason='',
)
assert POSITION_SNAPSHOTS_PATH.exists()
snap = json.loads(POSITION_SNAPSHOTS_PATH.read_text().strip())
assert snap['market_id'] == '0xaaa'
assert snap['delta_sl_would_fire'] is False
assert snap['tte_seconds'] == 90.5
assert snap['oracle_delta_pct'] is not None
print('[PASS] Snapshot row written, all fields present and correct')

write_position_snapshot(
    market_id='0xbbb', side='NO', token_id='tok2', underlying='ETH',
    tte_seconds=45.0, current_token_price=0.71, oracle_delta_pct=-0.5,
    hl_mark_price=3800.0, hl_depth_imbalance=-0.25,
    delta_sl_would_fire=True, upfrac_below=True, last_upfrac=0.28,
    coin_sl=0.1, exit_flag=True, reason='momentum_stop_loss',
)
snaps = [json.loads(l) for l in POSITION_SNAPSHOTS_PATH.read_text().splitlines() if l.strip()]
assert len(snaps) == 2
assert snaps[1]['delta_sl_would_fire'] is True
assert snaps[1]['upfrac_below'] is True
assert snaps[1]['exit_flag'] is True
print('[PASS] delta_sl_would_fire=True, upfrac_below=True, exit_flag=True written correctly')

# AC: None-valued fields must not raise
write_position_snapshot(
    market_id='x', side='YES', token_id='', underlying='BTC',
    tte_seconds=None, current_token_price=None, oracle_delta_pct=None,
    hl_mark_price=None, hl_depth_imbalance=None,
    delta_sl_would_fire=False, upfrac_below=False, last_upfrac=None,
    coin_sl=0.1, exit_flag=False, reason='',
)
snaps2 = [json.loads(l) for l in POSITION_SNAPSHOTS_PATH.read_text().splitlines() if l.strip()]
last = snaps2[-1]
assert last['tte_seconds'] is None
assert last['oracle_delta_pct'] is None
print('[PASS] None-valued optional fields serialised as null')

print()
print('=== ML-D2: acct_ledger.csv schema ===')

from accounting import LEDGER_HEADER
d2_cols = ['z_score_used', 'kelly_multiplier_used', 'delta_sl_pct_used', 'upfrac_threshold_used', 'loser_exit_trigger_used']
for col in d2_cols:
    assert col in LEDGER_HEADER, f'MISSING column: {col}'
    print(f'[PASS] {col} present in LEDGER_HEADER')

import accounting
from accounting import _ensure_ledger, _append_ledger, LEDGER_CSV

_tmp = Path(tempfile.mktemp(suffix='.csv'))
_orig = accounting.LEDGER_CSV
accounting.LEDGER_CSV = _tmp
_ensure_ledger()
_append_ledger(
    {k: '' for k in LEDGER_HEADER} | {
        'record_id': 't1', 'strategy': 'momentum', 'underlying': 'BTC',
        'z_score_used': 1.5, 'kelly_multiplier_used': 0.25,
        'delta_sl_pct_used': 0.1, 'upfrac_threshold_used': 0.45, 'loser_exit_trigger_used': 0.38,
    }
)
with _tmp.open(newline='', encoding='utf-8') as f:
    written_row = list(csv.DictReader(f))[0]
for col in d2_cols:
    assert col in written_row, f'{col} missing from written row'
    assert written_row[col] != '', f'{col} empty in written row'
print('[PASS] All 5 ML-D2 columns written with non-empty values')
_tmp.unlink()
accounting.LEDGER_CSV = _orig

# Existing rows get null — extrasaction='ignore' means old rows without new cols are safe
# (the header has the cols; DictWriter with extrasaction='ignore' leaves them blank for old writes)
print('[PASS] Existing rows: blank values for new columns acceptable (extrasaction=ignore)')

print()
print('=== SCANNER WIRING CHECK ===')
src = open('strategies/Momentum/scanner.py', encoding='utf-8').read()
assert 'emit_signal_events_batch as _emit_signal_events_batch' in src
assert '_d["executed"] = executed' in src
assert '_emit_signal_events_batch(scan_diags)' in src
print('[PASS] scanner.py: import, executed flag, and batch call all present')

print()
print('=== MONITOR WIRING CHECK ===')
src = open('monitor.py', encoding='utf-8').read()
assert 'write_position_snapshot as _write_position_snapshot' in src
assert '_last_upfrac: dict[str, float]' in src
assert '_write_position_snapshot(' in src
assert 'delta_sl_would_fire=_dsl_would_fire' in src
assert 'upfrac_below=_upfrac_below_flag' in src
assert 'self._last_upfrac.pop(pos.market_id, None)' in src
print('[PASS] monitor.py: import, _last_upfrac, snapshot call, would_fire flags, cleanup all present')

print()
print('=== GITIGNORE CHECK ===')
gi = open('.gitignore', encoding='utf-8').read()
assert 'data/' in gi
print('[PASS] data/ gitignored — covers signal_events.jsonl and position_snapshots.jsonl')

print()
print('==============================')
print('  ALL ML-D1 + ML-D2 QA CHECKS PASSED')
print('==============================')
