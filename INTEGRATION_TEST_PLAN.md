# Integration Test Plan

## Why Integration Tests?

Unit tests already cover individual modules (risk.py, maker.py, fill_simulator.py, etc.).
Integration tests cover the **component boundaries** — the paths where bugs actually happen:
- Units mismatch (capital vs face value, YES-prob vs token price)
- Race conditions (HL mid not yet populated when hedge fires)
- State threading across fill → risk → hedge → CSV
- Schema migrations (trades.csv header changes)

---

## Test Matrix

| ID | Scenario | Components | Priority |
|----|----------|------------|----------|
| IT-01 | YES fill → entry_cost_usd = price × size (not size) | maker fill, risk.open_position | P0 |
| IT-02 | NO fill → entry_cost_usd = (1−price) × size | maker fill, risk.open_position | P0 |
| IT-03 | _position_delta_usd uses capital, not face value | maker, risk | P0 |
| IT-04 | YES fill → net > threshold → HL SHORT placed | maker, risk, hl_client | P0 |
| IT-05 | NO fill → net < 0 → HL LONG placed | maker, risk, hl_client | P0 |
| IT-06 | Opposite YES+NO fills cancel → net=0 → no hedge | maker, risk, hl_client | P0 |
| IT-07 | Net below threshold after close → hedge removed | maker, risk, hl_client | P1 |
| IT-08 | HL mid=None → _rebalance_hedge returns early, no crash | maker, hl_client | P0 |
| IT-09 | close_position CSV row has `underlying` column | risk, CSV | P0 |
| IT-10 | record_hl_hedge_trade CSV row has `underlying` column | risk, CSV | P0 |
| IT-11 | CSV schema migration: old header → backup + new header | risk, CSV | P1 |
| IT-12 | /maker/capital endpoint returns entry_cost_usd sum, not face value | api_server, risk | P0 |
| IT-13 | HL BBO move ≥ trigger → reprice + hedge both called | maker, hl_client, pm_client | P1 |
| IT-14 | Correlated YES fills → coin loss limit → monitor fires | maker, risk, monitor | P1 |
| IT-15 | Hedge size = |net_capital_usd| / hl_mid (not BS-delta) | maker, risk | P0 |
| IT-16 | Hedge direction: net>0 → SHORT, net<0 → LONG | maker | P0 |
| IT-17 | Hedge capped at MAX_HL_NOTIONAL | maker, risk, hl_client | P1 |
| IT-18 | total_pm_capital_deployed = sum(entry_cost_usd) | risk.get_state | P0 |

---

## Detailed Scenarios

### IT-01 — YES fill → entry_cost_usd = price × size
- Open a YES position with size=600, entry_price=0.10
- Assert `pos.entry_cost_usd == 60.0` (not 600.0)
- Assert `risk.get_state()["total_pm_capital_deployed"] == 60.0`

### IT-02 — NO fill → entry_cost_usd = (1 − price) × size
- Open a NO position with size=600, entry_price=0.90
  (buying NO at 90¢ YES = 10¢ per token)
- Assert `pos.entry_cost_usd == 60.0`

### IT-03 — _position_delta_usd uses capital
- Open YES at price=0.10, size=600 → cost=60
- Open YES at price=0.90, size=600 → cost=540 (different market)
- Assert `_position_delta_usd("BTC") == 600.0` (sum of costs, not 1200)

### IT-04 — YES fill above threshold → hedge SHORT
- Open YES positions totalling > HEDGE_THRESHOLD_USD capital
- Call _rebalance_hedge("BTC")
- Assert hl_client.place_hedge was called with direction="SHORT"
- Assert hedge size ≈ net_capital / hl_mid

### IT-05 — NO fill → hedge LONG
- Open NO positions totalling > HEDGE_THRESHOLD_USD capital
- Call _rebalance_hedge  
- Assert direction="LONG"

### IT-06 — YES + NO cancel → no hedge
- Open YES $250 and NO $250 on same underlying
- Assert `_position_delta_usd("BTC") ≈ 0`
- Assert _rebalance_hedge does NOT call place_hedge

### IT-07 — Close position drops below threshold → hedge removed
- Open YES position at $300 (above threshold $200)
- Hedge is placed
- Close the position
- Assert `_position_delta_usd("BTC") == 0`
- Call _rebalance_hedge → assert close_hedge called (hedge removed)

### IT-08 — HL mid=None → no crash
- No HL mid registered for coin
- Call _rebalance_hedge with net > threshold
- Assert returns cleanly (no exception), no hedge placed

### IT-09 — close_position CSV `underlying` column
- Open + close position for BTC market
- Read back trades.csv
- Assert row["underlying"] == "BTC"

### IT-10 — record_hl_hedge_trade CSV `underlying` column
- Call risk.record_hl_hedge_trade(coin="ETH", ...)
- Read back trades.csv
- Assert row["underlying"] == "ETH"

### IT-11 — CSV migration
- Write old trades.csv with header missing `underlying`
- Instantiate RiskEngine (calls _ensure_csv)
- Assert backup file created (*.bak)
- Assert new trades.csv has TRADES_HEADER including `underlying`

### IT-12 — /maker/capital uses total_pm_capital_deployed
- Open YES positions with known entry_cost_usd (not face value)
- Call GET /maker/capital
- Assert `in_positions` = sum(entry_cost_usd), not sum(size)

### IT-13 — HL BBO move triggers reprice + hedge
- Register _on_hl_bbo_update handler
- Fire BBO update with move ≥ REPRICE_TRIGGER_PCT
- Assert _reprice_underlying called
- Assert _rebalance_hedge called

### IT-14 — Correlated YES fills → coin loss limit
- Open 3 YES fills on same underlying
- Inject losses > MAKER_COIN_MAX_LOSS_USD aggregate
- Assert monitor fires and all positions closed

### IT-15 — Hedge size = |net_capital| / hl_mid
- net_capital = $300, hl_mid = $2000
- Assert coins_to_hedge ≈ 0.15

### IT-16 — Hedge direction
- net > 0 → direction == "SHORT"
- net < 0 → direction == "LONG"

### IT-17 — Hedge capped at MAX_HL_NOTIONAL
- net_capital = MAX_HL_NOTIONAL * 10 (huge)
- Assert placed size = MAX_HL_NOTIONAL / hl_mid

### IT-18 — get_state total_pm_capital_deployed
- Open multiple positions; assert state sum equals sum(entry_cost_usd)

---

## Implementation File

All tests live in `tests/test_integration.py`.  
Run with:
```powershell
python -m pytest tests/test_integration.py -v
```

Run only P0 critical tests:
```powershell
python -m pytest tests/test_integration.py -v -m "p0"
```

---

## Test Infrastructure Needed

- `MockHLClient`: tracks `place_hedge` / `close_hedge` calls and returns truthy responses
- `MockPMClient`: provides `get_mid`, `get_markets`, `place_limit`, `cancel_order`
- All tests use the existing `_isolate_trades_csv` autouse fixture from `conftest.py`
