"""
Cross-check ON loser SELL orders against PM data activity API (source of truth).
Usage: python _query_on_fills.py
"""
import sys, os, asyncio
sys.path.insert(0, os.path.dirname(__file__))

import httpx
from py_clob_client_v2.client import ClobClient
import config as cfg

# ON loser SELL market_fak orders from orders.csv (price=0.01)
# market_id taken from trades.csv for the matching opening_neutral row by timestamp
ORDERS = [
    {"order_id": "0x159720b761a98d28a4934dd61413d99772e3aeb5bb2961cc3aa167eda3a12b7e", "market_id": "0xff613c0cccf0cf37765b7ac1f066289a42d9b76418d65a5046cfaeef1f113c17", "side": "NO",  "time": "12:15"},
    {"order_id": "0x16b02ab818bd6d057ef914e2656e70f0c1b48c6bf87a1ffb707098c15fe85c96", "market_id": "0x280de07044773ed5e7931f454e9015ad068c08468c3d7420c39e9048edcbd96d", "side": "NO",  "time": "12:16"},
    {"order_id": "0x7728379c8ee923302192d6c2caac8e95dc52fb99850fe87ed74beb9887dd666e", "market_id": "0xb494ff80f40042cdbd51b96498acb8dee8d1f95e21fdabe5aa24a0f38dfb0c2f", "side": "NO",  "time": "12:20"},
    {"order_id": "0xffa2b1257214f0f5f067d2a320270722e7f59e5d11e0b128f27b89d064ebcb53", "market_id": "0x9a3570369ff84d4a3f3ac14c2de23eb6d7cb9ecb5fbb0f03555d44fb1264059d", "side": "YES", "time": "12:40"},
    {"order_id": "0xeb213d10a967f771d30ab830c5e3c5811bb7be96ff22f050dce3556973d08066", "market_id": "0x05762327505a44e2c10e1174869f76c6c3bf18b7a82a9f1e3da8c37476b7a979", "side": "NO",  "time": "12:45"},
    {"order_id": "0xb632f6b4524c96ed6b89cb888570bedffc051f5e4780dd58cf4ed173852aa8af", "market_id": "0x36d7f28efa7747741bc5b06dbfa9e3f6fb7baa5c55b56e9fd1bd05e614fc12d8", "side": "NO",  "time": "12:46"},
]

OUTCOME_MAP = {"YES": ["Yes"], "NO": ["No"]}

clob = ClobClient(
    host=cfg.POLY_HOST,
    key=cfg.POLY_PRIVATE_KEY,
    chain_id=137,
    signature_type=2,
    funder=cfg.POLY_FUNDER,
)
clob.set_api_creds(clob.derive_api_key())

async def main():
    url = f"https://data-api.polymarket.com/activity?user={cfg.POLY_FUNDER}&limit=500"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        activity = data if isinstance(data, list) else data.get("value", [])

    print(f"PM activity rows returned: {len(activity)}")

    by_cond = {}
    for r in activity:
        cid = r.get("conditionId", "")
        if cid:
            by_cond.setdefault(cid, []).append(r)

    print(f"\n{'TIME':5} {'SIDE':4} {'CLOB status':20} {'PM SELL usdc':12} {'PM SELL size':12} {'actual price':12} {'actual pnl':10}")
    print("-" * 85)
    for o in ORDERS:
        oid = o["order_id"]
        mid = o["market_id"]
        side = o["side"]
        t = o["time"]

        try:
            order = clob.get_order(oid)
            sm     = float(order.get("size_matched") or 0) if order else 0
            lp     = float(order.get("price") or 0) if order else 0
            status = f"{order.get('status','?')}@{lp:.3f}x{sm:.3f}" if order else "NOT FOUND"
        except Exception as e:
            status = f"ERR:{e}"

        mkt_rows  = by_cond.get(mid, [])
        sell_rows = [r for r in mkt_rows if r.get("type") == "TRADE" and r.get("side") == "SELL"
                     and r.get("outcome", "") in OUTCOME_MAP.get(side, [])]
        buy_rows  = [r for r in mkt_rows if r.get("type") == "TRADE" and r.get("side") == "BUY"
                     and r.get("outcome", "") in OUTCOME_MAP.get(side, [])]

        sell_usdc  = sum(float(r.get("usdcSize") or 0) for r in sell_rows)
        sell_size  = sum(float(r.get("size") or 0) for r in sell_rows)
        buy_usdc   = sum(float(r.get("usdcSize") or 0) for r in buy_rows)
        sell_price = sell_usdc / sell_size if sell_size > 0 else 0
        actual_pnl = sell_usdc - buy_usdc if buy_usdc > 0 else None

        pnl_str = f"{actual_pnl:+.4f}" if actual_pnl is not None else "N/A (no buys)"
        print(f"{t:5} {side:4} {status:20} {sell_usdc:12.4f} {sell_size:12.4f} {sell_price:12.4f} {pnl_str:10}")

        if not mkt_rows:
            print(f"      *** market_id not in PM activity (beyond limit or wrong ID)")
        elif not sell_rows:
            all_types = list({r.get('type') for r in mkt_rows})
            all_sides = list({r.get('side') for r in mkt_rows})
            all_outcomes = list({r.get('outcome') for r in mkt_rows})
            print(f"      *** no SELL rows for {side} — mkt has {len(mkt_rows)} rows: types={all_types} sides={all_sides} outcomes={all_outcomes}")

asyncio.run(main())
"""
Query the PM CLOB API for actual fill prices on ON loser sell orders.
Usage: python _query_on_fills.py
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import TradeParams
import config as cfg

# ON loser SELL market_fak orders from orders.csv (price=0.01, no market_id)
ORDER_IDS = [
    "0x159720b761a98d28a4934dd61413d99772e3aeb5bb2961cc3aa167eda3a12b7e",  # 12:15
    "0x16b02ab818bd6d057ef914e2656e70f0c1b48c6bf87a1ffb707098c15fe85c96",  # 12:16
    "0x7728379c8ee923302192d6c2caac8e95dc52fb99850fe87ed74beb9887dd666e",  # 12:20
    "0xffa2b1257214f0f5f067d2a320270722e7f59e5d11e0b128f27b89d064ebcb53",  # 12:40
    "0xeb213d10a967f771d30ab830c5e3c5811bb7be96ff22f050dce3556973d08066",  # 12:45
    "0xb632f6b4524c96ed6b89cb888570bedffc051f5e4780dd58cf4ed173852aa8af",  # 12:46
]

clob = ClobClient(
    host=cfg.POLY_HOST,
    key=cfg.POLY_PRIVATE_KEY,
    chain_id=137,
    signature_type=2,
    funder=cfg.POLY_FUNDER,
)
clob.set_api_creds(clob.derive_api_key())

print(f"{'ORDER_ID':66} {'TRADES':6} {'FILL_PRICE':10} {'SIZE':8}")
print("-" * 100)
for oid in ORDER_IDS:
    try:
        trades = clob.get_trades(TradeParams(id=oid))
        if trades:
            total_size  = sum(float(t.get("size", 0)) for t in trades)
            total_value = sum(float(t.get("price", 0)) * float(t.get("size", 0)) for t in trades)
            vwap = total_value / total_size if total_size else 0
            print(f"{oid}  {len(trades):6}  {vwap:10.4f}  {total_size:8.4f}")
        else:
            # Fall back to order record
            order = clob.get_order(oid)
            sm = float(order.get("size_matched") or 0) if order else 0
            lp = float(order.get("price") or 0) if order else 0
            status = order.get("status", "?") if order else "NOT FOUND"
            print(f"{oid}  NO_TRADES  price={lp:.4f}  matched={sm:.4f}  status={status}")
    except Exception as e:
        print(f"{oid}  ERROR: {e}")
