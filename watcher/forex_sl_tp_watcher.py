#!/usr/bin/env python3
from __future__ import annotations
"""
Software-side SL/TP watcher for Oanda forex trades that couldn't get a
broker-side stop attached.

Oanda's FIFO safeguard blocks a TAKE_PROFIT/STOP_LOSS order on a trade if
another open trade of the same instrument and same unit size already exists
(see trader/oanda_trader.py). For those trades, position_state.json records
sl_tp_source="software" (or "mixed", for a position where only part of the
size is unprotected) and software_watch_units = the size this watcher must
cover. This script polls live Oanda pricing and closes that portion the
same way a broker-side stop would.

Run:
    python -m watcher.forex_sl_tp_watcher            # one-shot check
    python -m watcher.forex_sl_tp_watcher --loop      # poll every 60s
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from trader.oanda_trader import close_position, _oanda_instrument, _request as oanda_request
from watcher.position_manager import PositionManager, STATE_FILE

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

manager      = PositionManager()
_OANDA_ACCT  = os.environ["OANDA_ACCOUNT_ID"]
_POLL_SECS   = 60


def _live_price(instrument: str) -> float | None:
    try:
        r = oanda_request("GET", f"/accounts/{_OANDA_ACCT}/pricing?instruments={instrument}")
        p = r["prices"][0]
        bid, ask = float(p["bids"][0]["price"]), float(p["asks"][0]["price"])
        return (bid + ask) / 2
    except Exception as e:
        log.error(f"pricing fetch failed for {instrument}: {e}")
        return None


def check_once() -> None:
    data   = json.loads(STATE_FILE.read_text())
    trades = data.get("open_trades", {})

    for pair, t in trades.items():
        if t.get("closed") or not pair.startswith("OANDA:"):
            continue
        if t.get("sl_tp_source") not in ("software", "mixed"):
            continue
        units = t.get("software_watch_units") or 0
        if units <= 0:
            continue

        direction = t["direction"]
        tp, sl    = t.get("tp"), t.get("sl")
        instrument = _oanda_instrument(pair)
        price = _live_price(instrument)
        if price is None:
            continue

        hit = None
        if direction == "long":
            if sl is not None and price <= sl:
                hit = ("SL", sl)
            elif tp is not None and price >= tp:
                hit = ("TP", tp)
        else:
            if sl is not None and price >= sl:
                hit = ("SL", sl)
            elif tp is not None and price <= tp:
                hit = ("TP", tp)

        if hit:
            kind, level = hit
            log.warning(f"[{pair}] {kind} hit @ {level}  live={price}  "
                        f"closing {units} software-managed units")
            res = close_position(pair, units=int(units), direction=direction)
            if res.get("success"):
                manager.close_software_watch(pair, closed_units=units,
                                             reason=f"software_{kind.lower()}_hit@{level}",
                                             close_price=price)
                log.info(f"[{pair}] ✅ closed {units} units")
            else:
                log.error(f"[{pair}] close failed: {res.get('error')}")
        else:
            log.info(f"[{pair}] watching  live={price}  sl={sl}  tp={tp}  units={units}")


def main() -> None:
    if "--loop" not in sys.argv:
        check_once()
        return
    log.info(f"Forex SL/TP software watcher started — polling every {_POLL_SECS}s")
    while True:
        try:
            check_once()
        except Exception as e:
            log.error(f"check_once failed: {e}")
        time.sleep(_POLL_SECS)


if __name__ == "__main__":
    main()
