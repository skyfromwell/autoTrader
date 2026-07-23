#!/usr/bin/env python3
from __future__ import annotations
"""
Hyperliquid Fill Stream Listener.

Holds a persistent WebSocket connection to Hyperliquid's public API and
subscribes to userFills for our wallet. Closing fills (closedPnl != 0) are
debounced per-coin for a couple seconds — a single SL/TP trigger commonly
fills as many partial fills in the same burst (we've seen 10-15 for one
close) — then the size-weighted average close is forwarded to
tv_alert_server's /hl-fill webhook, which classifies it as sl_hit or tp_hit
by comparing the close price against the position's recorded sl/tp levels
and calls close_trade() with the real fill price.

This is the Hyperliquid analogue of watcher/oanda_stream_listener.py.

Run standalone:
    python -m watcher.hyperliquid_stream_listener
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

import requests
import websocket
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger("hl_stream")

WALLET = os.environ["HL_WALLET_ADDRESS"]
WS_URL = "wss://api.hyperliquid.xyz/ws"

WEBHOOK_URL    = os.environ.get("HL_FILL_WEBHOOK_URL", "http://localhost:9999/hl-fill")
WEBHOOK_SECRET = os.environ.get("TV_WEBHOOK_SECRET", "")

# How long to wait after the last fill in a burst before treating the close
# as complete and forwarding an aggregated webhook call.
_DEBOUNCE_SECONDS = 2.5

_lock    = threading.Lock()
_buffers: dict[str, dict] = {}   # coin -> {"fills": [...], "timer": Timer}

# Highest fill `time` processed so far, or None before the very first
# snapshot. This connection reconnects often (network blips are frequent —
# see the reconnect log), and Hyperliquid resends a fresh "isSnapshot: true"
# userFills payload on every reconnect, not just the first one ever. Treating
# every snapshot as pure historical replay meant a fill landing exactly in
# one of these frequent resyncs was silently discarded — that's how a real
# ONDO close disappeared entirely despite the connection being "up". Instead,
# only the very first snapshot establishes the watermark (so old history at
# startup is skipped); every fill after that — snapshot or live push — is
# processed if it's newer than the watermark.
#
# Uses STRICT less-than against the watermark (not <=) plus a per-tid seen
# set — a single market order that sweeps several price levels (a real SL/TP
# trigger commonly does, 10-15 fills in one burst) produces multiple fills
# that all share the *exact same* millisecond `time` value. The old `t <=
# _last_seen_time` check treated every fill after the first in such a burst
# as an already-processed duplicate and silently dropped it — so a close
# that should have aggregated to the position's full size instead forwarded
# only one tiny leftover fragment. That under-sized "close" then closed the
# tracked trade in our books while most of the real position stayed open on
# the exchange (confirmed against Hyperliquid's userFillsByTime — RENDER's
# 2026-07-22 SL execution reduced 8328.4→1815.2, but only a single 331.3-unit
# fragment ever reached tv_alert_server's /hl-fill, and 24 minutes later that
# leftover long got closed out as part of a fresh short's flip execution,
# producing another tiny fragment that closed the *new* short in our books
# instead — a live position vanished from tracking twice). `tid` is unique
# per fill even within a same-timestamp burst, so dedup on that instead.
_last_seen_time: int | None = None
_seen_tids: dict[int, int] = {}   # tid -> time(ms), pruned periodically
_SEEN_TID_RETENTION_MS = 24 * 60 * 60 * 1000  # bursts flush in seconds; a day is generous


def _flush(coin: str) -> None:
    with _lock:
        buf = _buffers.pop(coin, None)
    if not buf or not buf["fills"]:
        return

    fills = buf["fills"]
    total_sz  = sum(f["sz"] for f in fills)
    total_pnl = sum(f["closedPnl"] for f in fills)
    avg_px    = sum(f["px"] * f["sz"] for f in fills) / total_sz
    last_time = max(f["time"] for f in fills)

    payload = {
        "coin":       coin,
        "avg_price":  avg_px,
        "total_size": total_sz,
        "closed_pnl": total_pnl,
        "time":       last_time,
    }
    try:
        resp = requests.post(
            WEBHOOK_URL, params={"secret": WEBHOOK_SECRET}, json=payload, timeout=10,
        )
        log.info(f"Forwarded {coin} close avg_px={avg_px:.6g} sz={total_sz:.6g} "
                 f"pnl={total_pnl:.2f} ({len(fills)} fill(s)) -> {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.error(f"Failed to forward {coin} close: {e}")


def _handle_fill(f: dict) -> None:
    global _last_seen_time
    t   = f.get("time", 0)
    tid = f.get("tid")

    if _last_seen_time is not None and t < _last_seen_time:
        return  # strictly older than anything we've processed — historical replay

    if tid is not None:
        if tid in _seen_tids:
            return  # exact same fill re-delivered (e.g. reconnect snapshot replay)
        _seen_tids[tid] = t
        if len(_seen_tids) > 20_000:
            cutoff = t - _SEEN_TID_RETENTION_MS
            for old_tid, old_t in list(_seen_tids.items()):
                if old_t < cutoff:
                    del _seen_tids[old_tid]

    if _last_seen_time is None or t > _last_seen_time:
        _last_seen_time = t

    try:
        closed_pnl = float(f.get("closedPnl", 0) or 0)
    except (TypeError, ValueError):
        closed_pnl = 0.0
    if closed_pnl == 0:
        return  # opening fill, not a close — nothing to report

    coin = f["coin"]
    entry = {"px": float(f["px"]), "sz": float(f["sz"]), "closedPnl": closed_pnl,
              "time": f.get("time", 0)}

    with _lock:
        buf = _buffers.setdefault(coin, {"fills": [], "timer": None})
        buf["fills"].append(entry)
        if buf["timer"]:
            buf["timer"].cancel()
        timer = threading.Timer(_DEBOUNCE_SECONDS, _flush, args=(coin,))
        timer.daemon = True
        buf["timer"] = timer
        timer.start()


def _on_message(ws, message: str) -> None:
    global _last_seen_time
    try:
        msg = json.loads(message)
    except json.JSONDecodeError:
        return
    if msg.get("channel") != "userFills":
        return
    data = msg.get("data", {})
    fills = data.get("fills", [])

    if data.get("isSnapshot") and _last_seen_time is None:
        # The very first snapshot this process has ever seen replays fill
        # history — establish the watermark from it so we don't try to
        # "close" already-closed trades, but don't blanket-ignore every
        # future snapshot: Hyperliquid resends one on every reconnect (this
        # connection drops often), and a fill landing exactly in one of
        # those would otherwise be silently lost.
        _last_seen_time = max((f.get("time", 0) for f in fills), default=0)
        log.info(f"Initial snapshot processed ({len(fills)} historical fills) — "
                 f"watermark set to {_last_seen_time}")
        return

    for f in fills:
        _handle_fill(f)


def _on_open(ws) -> None:
    log.info(f"Connected — subscribing to userFills for {WALLET}")
    ws.send(json.dumps({
        "method": "subscribe",
        "subscription": {"type": "userFills", "user": WALLET},
    }))


def _on_error(ws, error) -> None:
    log.warning(f"WebSocket error: {error}")


def _on_close(ws, status_code, msg) -> None:
    log.warning(f"WebSocket closed (status={status_code} msg={msg})")


def run() -> None:
    backoff = 5
    while True:
        started = time.monotonic()
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
            # Hyperliquid drops idle connections — keep it alive with pings.
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            log.error(f"run_forever crashed: {e}")

        # A connection that lasted a while was fine — the disconnect was
        # probably just a normal network blip, not a persistent problem.
        # Reset backoff instead of ratcheting it up forever.
        if time.monotonic() - started > 60:
            backoff = 5

        log.warning(f"Reconnecting in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 120)


if __name__ == "__main__":
    run()
