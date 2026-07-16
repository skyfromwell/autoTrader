#!/usr/bin/env python3
"""
Read-only query of QMT account funds and stock positions — no orders placed,
no connection kept open. Prints JSON to stdout.

Run on the Windows QMT box (QMT must already be running and logged in):
    python query_account.py
"""
from __future__ import annotations
import json
import os
import sys
import time

import glob

QMT_ACCOUNT = os.getenv("QMT_ACCOUNT", "66801935")


def _find_qmt_path() -> str:
    """Auto-detect the userdata path instead of hardcoding the Chinese
    install-folder name — typing/passing it through SSH mangles the encoding.
    Must be the full-QMT "userdata" folder, NOT "userdata_mini" (miniQMT has
    had real-money trading disabled since 2026-07-01)."""
    env_path = os.getenv("QMT_PATH")
    if env_path and os.path.isdir(env_path) and not env_path.endswith("userdata_mini"):
        return env_path
    matches = [m for m in glob.glob(r"P:\xuntou2\*\userdata") if not m.endswith("userdata_mini")]
    if matches:
        return matches[0]
    raise RuntimeError("Could not find QMT userdata folder under P:\\xuntou2\\*")


QMT_PATH = _find_qmt_path()

from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount


class _CB(XtQuantTraderCallback):
    def on_disconnected(self):
        pass


def main() -> None:
    trader = XtQuantTrader(QMT_PATH, 2, callback=_CB())
    acc    = StockAccount(QMT_ACCOUNT)
    trader.start()
    rc = trader.connect()
    if rc != 0:
        print(json.dumps({"error": f"connect() returned {rc}", "qmt_path_used": QMT_PATH,
                          "account": QMT_ACCOUNT}))
        sys.exit(1)
    trader.subscribe(acc)

    asset = None
    for _ in range(15):
        asset = trader.query_stock_asset(acc)
        if asset:
            break
        time.sleep(1)

    positions = trader.query_stock_positions(acc) or []

    out = {
        "account": {
            "cash": getattr(asset, "cash", None),
            "total_asset": getattr(asset, "total_asset", None),
            "market_value": getattr(asset, "market_value", None),
            "frozen_cash": getattr(asset, "frozen_cash", None),
        } if asset else None,
        "positions": [
            {
                "stock_code": getattr(p, "stock_code", None),
                "volume": getattr(p, "volume", None),
                "can_use_volume": getattr(p, "can_use_volume", None),
                "open_price": getattr(p, "open_price", None),
                "market_value": getattr(p, "market_value", None),
            }
            for p in positions
        ],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    trader.stop()


if __name__ == "__main__":
    main()
