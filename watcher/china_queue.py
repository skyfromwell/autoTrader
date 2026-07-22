#!/usr/bin/env python3
from __future__ import annotations
"""
Shared China A-share signal submission.

POSTs to qmt_mailbox/mailbox_writer.py's FastAPI relay running on the
Windows QMT box (over Tailscale), which writes the order into its local
inbox for qmt_mailbox_executor.py — pasted into QMT's built-in Python
console — to pick up and execute. See qmt_mailbox/README.md for the full
architecture; miniQMT/README.md explains why this isn't a Syncthing-folder
handoff anymore (xtquant external access has been unsupported since
2026-07-01, and folder-sync proved unreliable for this even before that).

output/china_pending/ is now purely OUR OWN local bookkeeping — one JSON
file per pair, used only for "have we already submitted this" dedup and as
an audit trail. It does not drive execution; only the Windows box's local
inbox does that. If the mailbox relay is unreachable, queue_order() returns
None and does NOT write a bookkeeping file, so the caller's own "already
queued" check won't wrongly suppress a retry on the next scan.

All three signal sources that can submit a China trade import this module
so the request format never drifts between them again:
  - screener/china_sma_report.py   (type="sma_gold_cross")
  - watcher/mcp_processor.py       (type="watcher_pull")
  - watcher/tv_alert_server.py     (type="tv_alert")
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

QMT_MAILBOX_URL = os.environ.get("QMT_MAILBOX_URL", "http://100.64.0.7:8800")
QMT_MAILBOX_KEY = os.environ.get("QMT_MAILBOX_API_KEY", "")
PENDING_DIR     = Path("output/china_pending")   # local bookkeeping only
_TIMEOUT        = 10


def _headers() -> dict:
    return {"X-API-Key": QMT_MAILBOX_KEY} if QMT_MAILBOX_KEY else {}


def _tv_to_qmt_stock(pair: str) -> str:
    """SZSE:000725 → 000725.SZ,  SSE:600036 → 600036.SH"""
    prefix, code = pair.split(":", 1)
    return code + (".SZ" if prefix.upper() == "SZSE" else ".SH")


def pair_filename(pair: str) -> str:
    """SZSE:000725 → SZSE_000725.json"""
    return pair.replace(":", "_") + ".json"


def load_pending() -> dict:
    """Local bookkeeping of what WE'VE already submitted — dedup only, does
    not reflect the Windows box's actual inbox/outbox state."""
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
                 type_: str, reason: str = "") -> Optional[dict]:
    """Submit a China long signal to the QMT mailbox over HTTP.

    Returns the mailbox's response ({"id": signal_id, "status": "queued"})
    on success, or None if the request failed (Windows box unreachable,
    relay not running, etc.) — callers should treat None as "not actually
    submitted, try again next scan," matching every other broker_open()
    style function in this codebase that returns success/failure rather
    than assuming the call worked.
    """
    stock  = _tv_to_qmt_stock(pair)
    volume = int((notional // price) // 100) * 100 if price and price > 0 else None

    try:
        resp = requests.post(
            f"{QMT_MAILBOX_URL}/signal",
            json={"stock": stock, "side": "buy", "volume": volume, "source": type_},
            headers=_headers(), timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        log.error(f"[china_queue] mailbox submit failed for {pair}: {e}")
        return None

    order = {
        "pair":         pair,
        "type":         type_,
        "notional":     notional,
        "signal_price": price,
        "timeframe":    timeframe,
        "reason":       reason,
        "queued_at":    datetime.now().isoformat(timespec="seconds"),
        "signal_id":    result.get("id"),
    }
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    (PENDING_DIR / pair_filename(pair)).write_text(json.dumps(order, indent=2))
    log.info(f"[china_queue] submitted {pair} -> {stock} buy vol={volume}  id={result.get('id')}")
    return result


def close_order(pair: str, reason: str = "") -> Optional[dict]:
    """Submit a close (sell all available) signal to the QMT mailbox.
    Returns the mailbox's response on success, None on failure — same
    contract as queue_order()."""
    stock = _tv_to_qmt_stock(pair)
    try:
        resp = requests.post(
            f"{QMT_MAILBOX_URL}/signal",
            json={"stock": stock, "side": "close", "source": reason or "close"},
            headers=_headers(), timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        log.error(f"[china_queue] mailbox close submit failed for {pair}: {e}")
        return None
    log.info(f"[china_queue] submitted close {pair} -> {stock}  id={result.get('id')}")
    return result


def get_status(signal_id: str) -> Optional[dict]:
    """Poll the mailbox relay for a signal's real execution status
    (submitted/rejected/filled/stale) — not just our local bookkeeping."""
    try:
        resp = requests.get(f"{QMT_MAILBOX_URL}/signal/{signal_id}/status",
                            headers=_headers(), timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"[china_queue] status check failed for {signal_id}: {e}")
        return None


def dequeue(pair: str) -> None:
    (PENDING_DIR / pair_filename(pair)).unlink(missing_ok=True)
