#!/usr/bin/env python3
"""
QMT China A-share executor — Syncthing file-based approach.

Mac/Mini write one JSON file per pending order into a shared folder
(output/china_pending/). Syncthing replicates it to Windows instantly.
This script watches that folder, executes each order via xtquant,
then deletes the file. The deletion propagates back to Mac/Mini via Syncthing.

Setup:
    1. Install Syncthing on Windows, pair with Mac/Mini, share the
       autoTrader output/ folder (or china_pending/ subfolder).
    2. Set PENDING_DIR in environment or start_executor.bat.
    3. Open QMT → run this script.

Config (env vars or set below):
    PENDING_DIR  — path to synced china_pending folder
    QMT_ACCOUNT  — real account ID
    POLL_SECS    — directory scan interval (default 15s)
"""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

PENDING_DIR = Path(os.getenv("PENDING_DIR", r"C:\autoTrader\output\china_pending"))
ACCOUNT     = os.getenv("QMT_ACCOUNT", "66801935")
POLL_SECS   = int(os.getenv("POLL_SECS", "15"))
QMT_PATH    = os.getenv("QMT_PATH", r"P:\xuntou2\金融街证券QMT实盘 - 交易终端\userdata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(Path(os.getenv("LOG_FILE", r"C:\autoTrader\china_server\executor.log"))),
                            encoding="utf-8"),
    ]
)
log = logging.getLogger(__name__)

# ── xtquant setup ─────────────────────────────────────────────────────────────

from xtquant import xtconstant, xtdata
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

from qmt_trade_wrapper import TradeGuard

_trader: XtQuantTrader | None = None
_acc:    StockAccount   | None = None


class _CB(XtQuantTraderCallback):
    def on_disconnected(self):
        log.warning("QMT disconnected")


def _connect() -> None:
    global _trader, _acc
    _trader = XtQuantTrader(QMT_PATH, 1, callback=_CB())
    _acc    = StockAccount(ACCOUNT)
    _trader.start()
    rc = _trader.connect()
    if rc != 0:
        raise RuntimeError(f"XtQuantTrader.connect() returned {rc}")
    _trader.subscribe(_acc)
    log.info(f"Connected to QMT — account {ACCOUNT}")
    for _ in range(30):
        asset = _trader.query_stock_asset(_acc)
        if asset:
            log.info(f"Account ready: cash=¥{asset.cash:,.0f}  total=¥{asset.total_asset:,.0f}")
            return
        time.sleep(1)
    log.warning("Account data not ready after 30s — proceeding anyway")


# ── Order helpers ─────────────────────────────────────────────────────────────

def _tv_to_xt(tv_pair: str) -> str:
    """SZSE:000725 → 000725.SZ,  SSE:600030 → 600030.SH"""
    prefix, code = tv_pair.split(":", 1)
    return code + (".SZ" if prefix.upper() == "SZSE" else ".SH")


def _get_price(xt_symbol: str, fallback: float) -> float:
    try:
        ticks = xtdata.get_full_tick([xt_symbol])
        t = ticks.get(xt_symbol)
        if t:
            px = t.get("lastPrice") or t.get("last") or t.get("close")
            if px and float(px) > 0:
                return float(px)
    except Exception as e:
        log.warning(f"get_full_tick {xt_symbol} failed: {e}")
    return fallback


def _execute_order(order: dict, task_id: str) -> dict:
    """Guarded buy: skips if already held, verifies the fill, and writes a
    result JSON via TradeGuard instead of just trusting order_id != -1."""
    tv_pair      = order["pair"]
    signal_price = float(order.get("signal_price", 0))
    notional     = int(order.get("notional", 50000))

    xt_sym  = _tv_to_xt(tv_pair)
    px      = _get_price(xt_sym, signal_price)
    volume  = int((notional / px) // 100) * 100
    if volume <= 0:
        raise ValueError(f"0 shares at ¥{px:.2f} notional=¥{notional}")

    # Preserve the A-share exchange-specific market order types (five-level
    # convert-then-cancel-remainder) rather than TradeGuard's generic default.
    price_type = (xtconstant.MARKET_SH_CONVERT_5_CANCEL
                  if xt_sym.endswith(".SH")
                  else xtconstant.MARKET_SZ_INSTBUSI_RESTCANCEL)

    guard  = TradeGuard(_trader, _acc, task_id)
    result = guard.run(
        code=xt_sym,
        direction="buy",
        volume=volume,
        price=None,
        min_existing_volume=1,
        price_type=price_type,
    )
    result["symbol"]         = xt_sym
    result["exec_price_est"] = px
    result["tv_pair"]        = tv_pair
    result["notional"]       = notional
    return result


# ── Trading hours ─────────────────────────────────────────────────────────────

def _is_trading_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    if (9, 30) <= (h, m) <= (11, 30):
        return True
    if (13, 0) <= (h, m) <= (15, 0):
        return True
    return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    log.info(f"China executor starting  account={ACCOUNT}  pending_dir={PENDING_DIR}  poll={POLL_SECS}s")
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    _connect()

    while True:
        if not _is_trading_hours():
            log.debug("Outside trading hours — sleeping 60s")
            time.sleep(60)
            continue

        order_files = sorted(PENDING_DIR.glob("*.json"))
        if order_files:
            log.info(f"{len(order_files)} pending order file(s)")

        for fpath in order_files:
            try:
                order = json.loads(fpath.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning(f"Could not read {fpath.name}: {e}")
                continue

            pair    = order.get("pair", fpath.stem.replace("_", ":", 1))
            task_id = f"{fpath.stem}_{int(time.time())}"
            log.info(f"Processing {pair}  queued_at={order.get('queued_at','?')}  task_id={task_id}")

            try:
                result = _execute_order(order, task_id)
            except Exception as e:
                log.error(f"{pair} order failed: {e} — leaving file for retry")
                continue

            status = result.get("status")

            # SKIPPED_* means the guard correctly decided nothing should
            # happen (already held, etc.) — that's a resolved outcome, not a
            # failure to retry. Anything else non-terminal gets retried.
            if status in ("FILLED", "PARTIAL_FILL", "FILLED_INFERRED_FROM_POSITION",
                         "SKIPPED_ALREADY_HELD", "SKIPPED_NOTHING_TO_SELL"):
                try:
                    fpath.unlink()
                    log.info(f"✅ {pair}  status={status}  order={result.get('order_id')}  "
                             f"→ file deleted, sync will propagate  (result: {task_id}.json)")
                except Exception as e:
                    log.warning(f"Could not delete {fpath.name}: {e}")
            else:
                log.error(f"{pair} status={status}  detail={result.get('detail')}  "
                          f"— leaving file for retry  (result: {task_id}.json)")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    run()
