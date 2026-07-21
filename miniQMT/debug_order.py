#!/usr/bin/env python3
"""
Run this directly on the remote Windows machine to diagnose order failures:
    py -3.9 debug_order.py
"""
import time
from xtquant import xtconstant
from xtquant.xttrader import XtQuantTrader
from xtquant.xttype import StockAccount

QMT_PATH   = r"P:\XUNTOU\金融街证券QMT模拟 - 交易终端\userdata_mini"

# Try multiple account ID formats
ACCOUNT_IDS = [
    "66801935",
    "测试66801935",
]

print(f"[1] Creating trader  path={QMT_PATH}")
trader = XtQuantTrader(QMT_PATH, 2)   # session=2 to avoid conflict with running server

print("[2] Starting trader...")
trader.start()

print("[3] Connecting...")
result = trader.connect()
print(f"    connect() → {result}  (0=ok)")

# Try each account ID format
working_acc = None
for acct_id in ACCOUNT_IDS:
    acc = StockAccount(acct_id)
    trader.subscribe(acc)
    print(f"\n[4] Trying account ID: '{acct_id}'")
    asset = None
    for i in range(8):
        asset = trader.query_stock_asset(acc)
        if asset is not None:
            print(f"    ✓ asset ready after {i+1}s — cash={asset.cash}  total={asset.total_asset}")
            working_acc = acc
            break
        time.sleep(1)
    if asset and working_acc is None:
        print("    All asset fields:")
        for k, v in vars(asset).items():
            print(f"      {k} = {v}")
    if working_acc:
        break
    print(f"    ✗ no data for '{acct_id}'")

if not working_acc:
    print("\n[!] No account ID worked — check QMT GUI for the correct account ID")
    trader.stop()
    exit(1)

print(f"\n[5] Working account: {working_acc.account_id}")
positions = trader.query_stock_positions(working_acc)
print(f"    positions → {positions}")

print("\n[6] Placing limit order  600036.SH  100 shares @ 45.00...")
order_id = trader.order_stock(
    account       = working_acc,
    stock_code    = "600036.SH",
    order_type    = xtconstant.STOCK_BUY,
    order_volume  = 100,
    price_type    = xtconstant.FIX_PRICE,
    price         = 45.00,
    strategy_name = "debug",
    order_remark  = "",
)
print(f"    order_id → {order_id}  (-1=rejected)")

print("\n[7] Done.")
trader.stop()
