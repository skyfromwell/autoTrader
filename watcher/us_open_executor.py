#!/usr/bin/env python3
from __future__ import annotations
"""
US Stocks Market-Open Executor — cron at 06:30 PT (09:30 ET), Mon-Fri

Reads output/us_pending_orders.json and places Alpaca market orders.
Updates position_state.json with entry prices fetched from Alpaca after fill.
"""

import os, sys, json, time, logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from trader.trader import execute_trade as alpaca_execute
from watcher.position_manager import PositionManager

PENDING_FILE = Path("output/us_pending_orders.json")

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s",
                    handlers=[logging.StreamHandler()])


def _alpaca_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(
        api_key=os.getenv("ALPACA_API_KEY"),
        secret_key=os.getenv("ALPACA_SECRET_KEY"),
        paper=True,
    )


def main():
    log.info("=" * 55)
    log.info(f"  US Open Executor  |  {datetime.now():%Y-%m-%d %H:%M ET}")
    log.info("=" * 55)

    if not PENDING_FILE.exists():
        log.info("No pending queue — nothing to do"); return

    orders = json.loads(PENDING_FILE.read_text())
    if not orders:
        log.info("Queue empty"); return

    log.info(f"Processing {len(orders)} pending orders")
    manager = PositionManager()
    client  = _alpaca_client()
    executed, failed = [], []

    for order in orders:
        tv_sym   = order["tv_symbol"]
        action   = order["action"]
        reason   = order.get("reason", "sma_signal")
        notional = order.get("notional", 10000)
        symbol   = tv_sym.split(":")[-1]

        try:
            if action == "open_long":
                # Check not already in position
                try:
                    existing = client.get_open_position(symbol)
                    if existing:
                        log.info(f"[{tv_sym}] already has position — skipping open")
                        executed.append(order)
                        continue
                except Exception:
                    pass  # no existing position, proceed

                ok = alpaca_execute(tv_sym, "long", notional=notional)
                if ok:
                    time.sleep(4)  # wait for fill
                    entry = 0.0
                    try:
                        pos   = client.get_open_position(symbol)
                        entry = float(pos.avg_entry_price)
                    except Exception:
                        pass
                    manager.open_trade(
                        pair=tv_sym, direction="long", entry=entry,
                        tp=None, sl=None, atr=0.0, size=1.0, features={},
                        bar_time=datetime.now().isoformat(timespec="seconds"),
                    )
                    log.info(f"[{tv_sym}] ✅ OPENED LONG  entry={entry:.2f}  notional=${notional:,}")
                    executed.append(order)
                else:
                    log.error(f"[{tv_sym}] order placement failed")
                    failed.append(order)

            elif action == "close":
                try:
                    client.close_position(symbol)
                    time.sleep(3)
                    # Get close price from last trade
                    try:
                        trades = client.get_portfolio_history(period="1D")
                        close_price = 0.0
                    except Exception:
                        close_price = 0.0
                    manager.close_trade(tv_sym, reason, close_price)
                    log.info(f"[{tv_sym}] ✅ CLOSED  reason={reason}")
                    executed.append(order)
                except Exception as e:
                    log.warning(f"[{tv_sym}] close failed: {e}")
                    failed.append(order)

        except Exception as e:
            log.error(f"[{tv_sym}] unexpected error: {e}")
            failed.append(order)

    remaining = failed  # keep only failed orders for retry
    PENDING_FILE.write_text(json.dumps(remaining, indent=2))
    log.info(f"Done: {len(executed)} executed, {len(failed)} failed, {len(remaining)} remaining in queue")


if __name__ == "__main__":
    main()
