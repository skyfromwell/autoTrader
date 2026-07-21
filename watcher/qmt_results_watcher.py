#!/usr/bin/env python3
from __future__ import annotations
"""
Consumes result JSONs written by china_server/qmt_trade_wrapper.py's
TradeGuard, synced from the QMT Windows box into output/qmt_results/ via
Syncthing (folder "qmt-results" — see china_server/syncthing_setup.bat for
the sibling china-pending folder this mirrors).

Until this existed, nothing read those files — china_executor.py's TradeGuard
gave a real FILLED/PARTIAL_FILL/SKIPPED/ERROR status, but the Mac/Mini side
had no way to see it beyond eyeballing the synced JSON by hand.

For each result:
  - FILLED / PARTIAL_FILL / FILLED_INFERRED_FROM_POSITION: record the trade
    in position_state.json via PositionManager, using the real post-trade
    fill price when available (position_after.avg_price) rather than the
    pre-trade estimate. Skipped if the pair is already tracked as open.
  - SKIPPED_ALREADY_HELD / SKIPPED_NOTHING_TO_SELL: log only — the guard
    correctly decided nothing needed to happen.
  - SUBMIT_FAILED / NO_FILL_DETECTED / ERROR / PARTIAL_FILL: Telegram alert,
    since these need a human to look.

Each processed file is deleted after handling (mirrors china_pending's
"file gone = done" convention) so this can run as a simple poll loop.

Run:
    python -m watcher.qmt_results_watcher          # one-shot check
    python -m watcher.qmt_results_watcher --loop    # poll every 30s
"""

import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from watcher.position_manager import PositionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

manager      = PositionManager()
RESULTS_DIR  = Path(__file__).parent.parent / "output" / "qmt_results"
_POLL_SECS       = 30
_CLOSED_POLL_SECS = 1800  # 30 min — no point polling often when nothing can arrive
_CHINA_TZ = ZoneInfo("Asia/Shanghai")


def _is_china_market_hours() -> bool:
    now = datetime.now(_CHINA_TZ)
    if now.weekday() >= 5:
        return False
    hm = (now.hour, now.minute)
    return (9, 30) <= hm <= (11, 30) or (13, 0) <= hm <= (15, 0)

_FILLED_STATUSES  = {"FILLED", "PARTIAL_FILL", "FILLED_INFERRED_FROM_POSITION"}
_SKIPPED_STATUSES = {"SKIPPED_ALREADY_HELD", "SKIPPED_NOTHING_TO_SELL"}
_ALERT_STATUSES   = {"SUBMIT_FAILED", "NO_FILL_DETECTED", "ERROR", "PARTIAL_FILL"}


def _telegram_alert(text: str) -> None:
    tok = os.getenv("TELEGRAM_TOKEN", "")
    cid = os.getenv("TELEGRAM_CHAT_ID", "")
    if not tok or not cid:
        log.warning("TELEGRAM_TOKEN/CHAT_ID not set — skipping alert, log-only")
        return
    try:
        payload = json.dumps({"chat_id": cid, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        log.warning(f"Telegram alert failed: {e}")


def _handle_result(fpath: Path) -> None:
    try:
        result = json.loads(fpath.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Could not read {fpath.name}: {e}")
        return

    status  = result.get("status")
    pair    = result.get("tv_pair") or result.get("code")
    task_id = result.get("task_id", fpath.stem)

    log.info(f"[{task_id}] {pair}  status={status}  "
             f"requested={result.get('requested_volume')}  "
             f"filled={result.get('filled_volume')}")

    if status in _FILLED_STATUSES:
        existing = manager.get_trade(pair) if pair else None
        if existing and not existing.closed:
            log.info(f"[{task_id}] {pair} already tracked as open — not re-recording")
        elif pair:
            after = result.get("position_after") or {}
            entry = after.get("avg_price") or result.get("exec_price_est")
            size  = result.get("filled_volume") or result.get("notional") or 0
            if entry:
                manager.open_trade(pair=pair, direction="long", entry=float(entry),
                                   tp=None, sl=None, atr=0, size=size, features={},
                                   opened_by="tv_alert")
                log.info(f"[{task_id}] ✅ recorded {pair} long @ {entry}  size={size}")
            else:
                log.warning(f"[{task_id}] {pair} filled but no entry price available — not recorded")
    elif status in _SKIPPED_STATUSES:
        pass  # correctly decided no-op, nothing to record or alert
    elif status in _ALERT_STATUSES:
        _telegram_alert(
            f"⚠️ QMT trade {status}\n{pair}  task={task_id}\n"
            f"{result.get('detail') or result.get('error_msg') or ''}"
        )
    else:
        log.warning(f"[{task_id}] {pair} unknown status={status}")

    fpath.unlink(missing_ok=True)


def check_once() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for fpath in sorted(RESULTS_DIR.glob("*.json")):
        _handle_result(fpath)


def main() -> None:
    if "--loop" not in sys.argv:
        check_once()
        return
    log.info(f"QMT results watcher started — polling {RESULTS_DIR} every {_POLL_SECS}s "
             f"during China market hours (idle-polling every {_CLOSED_POLL_SECS}s otherwise)")
    while True:
        if not _is_china_market_hours():
            time.sleep(_CLOSED_POLL_SECS)
            continue
        try:
            check_once()
        except Exception as e:
            log.error(f"check_once failed: {e}")
        time.sleep(_POLL_SECS)


if __name__ == "__main__":
    main()
