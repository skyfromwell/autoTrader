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
ACCOUNT_ID = "测试66801935"

print(f"[1] Creating trader  path={QMT_PATH}")
trader = XtQuantTrader(QMT_PATH, 2)   # session=2 to avoid conflict with running server
acc    = StockAccount(ACCOUNT_ID)

print("[2] Starting trader...")
trader.start()

print("[3] Connecting...")
result = trader.connect()
print(f"    connect() → {result}  (0=ok)")

print("[4] Subscribing account...")
trader.subscribe(acc)
time.sleep(2)   # give it a moment to settle

print("[5] Querying account asset...")
asset = trader.query_stock_asset(acc)
print(f"    asset → {asset}")
if asset:
    print(f"    cash={asset.cash}  total={asset.total_asset}")

print("[6] Querying positions...")
positions = trader.query_stock_positions(acc)
print(f"    positions → {positions}")

print("[7] Placing limit order  600036.SH  100 shares @ 45.00...")
order_id = trader.order_stock(
    account       = acc,
    stock_code    = "600036.SH",
    order_type    = xtconstant.STOCK_BUY,
    order_volume  = 100,
    price_type    = xtconstant.FIX_PRICE,
    price         = 45.00,
    strategy_name = "debug",
    order_remark  = "",
)
print(f"    order_id → {order_id}  (-1=rejected)")

print("[8] Placing market order  600036.SH  100 shares (MARKET_SH_CONVERT_5_CANCEL)...")
order_id2 = trader.order_stock(
    account       = acc,
    stock_code    = "600036.SH",
    order_type    = xtconstant.STOCK_BUY,
    order_volume  = 100,
    price_type    = xtconstant.MARKET_SH_CONVERT_5_CANCEL,
    price         = 0,
    strategy_name = "debug",
    order_remark  = "",
)
print(f"    order_id → {order_id2}  (-1=rejected)")

print("[9] Done.")
trader.stop()
