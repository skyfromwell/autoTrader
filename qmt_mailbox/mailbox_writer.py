#!/usr/bin/env python3
"""
mailbox_writer.py

Writer side of the QMT signal mailbox. Runs as a small FastAPI relay ON the
Windows QMT box, reachable over Tailscale from wherever the actual signal
generation runs (MacBook / Mac Mini): mcp_processor.py, tv_alert_server.py,
and screener/china_sma_report.py all POST here via watcher/china_queue.py.

This machine's mailbox lives on the P: drive, alongside the QMT
installation itself. The QMT-side poller (qmt_mailbox_executor.py, pasted
into QMT's built-in Python console) only ever reads local files here — it
never touches the network, by design, since it runs inside QMT's single
shared strategy thread and must never block on I/O.

Run on the Windows box:
    uvicorn mailbox_writer:app --host 0.0.0.0 --port 8800
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

load_dotenv()

INBOX_DIR  = r"P:\qmt_signal_mailbox\inbox"
OUTBOX_DIR = r"P:\qmt_signal_mailbox\outbox"
API_KEY    = os.getenv("QMT_MAILBOX_API_KEY", "")

for _d in (INBOX_DIR, OUTBOX_DIR):
    os.makedirs(_d, exist_ok=True)


def submit_signal(stock: str, side: str, volume: Optional[int] = None,
                  max_position_volume: Optional[int] = None,
                  source: str = "tv_signal",
                  tp: Optional[float] = None, sl: Optional[float] = None) -> str:
    """Atomically writes one signal file into the inbox. Returns the
    signal id so the caller can later poll get_status(signal_id).

    tp/sl (buy signals only) get stored by qmt_mailbox_executor.py against
    the filled position — QMT has no broker-side conditional-order support
    for A-shares, so the QMT-side script polls price against these each
    cycle and fires its own sell when crossed."""
    side = side.lower()
    if side not in ("buy", "sell", "close"):
        raise ValueError(f"invalid side: {side}")

    signal_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    payload = {"id": signal_id, "stock": stock, "side": side, "source": source}
    if volume is not None:
        payload["volume"] = int(volume)
    if max_position_volume is not None:
        payload["max_position_volume"] = int(max_position_volume)
    if tp is not None:
        payload["tp"] = float(tp)
    if sl is not None:
        payload["sl"] = float(sl)

    final_path = os.path.join(INBOX_DIR, f"{signal_id}.json")
    tmp_path   = final_path + ".tmp"
    # write to a temp name, then atomic rename -- the QMT-side poller
    # will never see a half-written file this way
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp_path, final_path)
    return signal_id


def get_status(signal_id: str) -> Optional[dict]:
    """Reads back the outbox status file for a signal, if the QMT-side
    executor has written one yet (submitted / rejected / filled / stale)."""
    path = os.path.join(OUTBOX_DIR, f"{signal_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── FastAPI relay ─────────────────────────────────────────────────────────────

app = FastAPI(title="QMT Signal Mailbox Relay")


def _verify_key(x_api_key: str = Header(None, alias="X-API-Key")) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class SignalRequest(BaseModel):
    stock:               str
    side:                str
    volume:              Optional[int] = None
    max_position_volume: Optional[int] = None
    source:              str = "tv_signal"
    tp:                  Optional[float] = None
    sl:                  Optional[float] = None


@app.get("/health")
def health():
    return {"status": "ok", "inbox_dir": INBOX_DIR, "outbox_dir": OUTBOX_DIR}


@app.post("/signal")
def post_signal(req: SignalRequest, x_api_key: str = Header(None, alias="X-API-Key")):
    _verify_key(x_api_key)
    try:
        signal_id = submit_signal(stock=req.stock, side=req.side, volume=req.volume,
                                  max_position_volume=req.max_position_volume,
                                  source=req.source, tp=req.tp, sl=req.sl)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": signal_id, "status": "queued"}


@app.get("/signal/{signal_id}/status")
def get_signal_status(signal_id: str, x_api_key: str = Header(None, alias="X-API-Key")):
    _verify_key(x_api_key)
    status = get_status(signal_id)
    if status is None:
        return {"id": signal_id, "status": "pending", "detail": "not yet processed"}
    return status


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mailbox_writer:app", host="0.0.0.0", port=int(os.getenv("QMT_MAILBOX_PORT", "8800")))
