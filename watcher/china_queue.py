#!/usr/bin/env python3
from __future__ import annotations
"""
Shared China A-share pending-order queue — one JSON file per pair in
output/china_pending/. Syncthing replicates that folder to the Windows QMT
bridge, where china_server/china_executor.py polls it continuously and fires
each order at the next market open (its own _is_trading_hours() gate decides
when). Nothing on this side triggers execution — writing a file here is the
entire hand-off.

All three signal sources that can queue a China trade import this module so
the on-disk format never drifts between them again:
  - screener/china_sma_report.py   (type="sma_gold_cross")
  - watcher/mcp_processor.py       (type="watcher_pull")
  - watcher/tv_alert_server.py     (type="tv_alert")
"""

import json
from pathlib import Path

PENDING_DIR = Path("output/china_pending")


def pair_filename(pair: str) -> str:
    """SZSE:000725 → SZSE_000725.json"""
    return pair.replace(":", "_") + ".json"


def load_pending() -> dict:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    result = {}
    for f in sorted(PENDING_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            pair = data.get("pair") or f.stem.replace("_", ":", 1)
            result[pair] = data
        except Exception:
            pass
    return result


def queue_order(pair: str, price: float, timeframe: str, notional: int,
                 type_: str, reason: str = "") -> None:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    order = {
        "pair":         pair,
        "type":         type_,
        "notional":     notional,
        "signal_price": price,
        "timeframe":    timeframe,
        "reason":       reason,
        "queued_at":    __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    (PENDING_DIR / pair_filename(pair)).write_text(json.dumps(order, indent=2))


def dequeue(pair: str) -> None:
    (PENDING_DIR / pair_filename(pair)).unlink(missing_ok=True)
