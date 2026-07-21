#!/usr/bin/env python3
from __future__ import annotations
"""
OANDA Transaction Stream Listener.

Holds a persistent connection to OANDA's own transactions/stream endpoint —
one per account, now that signals are split across two accounts by
timeframe (see trader/oanda_trader.py) — and forwards STOP_LOSS_ORDER /
TAKE_PROFIT_ORDER / TRAILING_STOP_LOSS_ORDER fills to tv_alert_server's
/oanda-fill webhook. This is how position_manager learns about broker-side
closes in near-real-time, instead of waiting on Jingda's own chart-side exit
signal (which can miss a close entirely once the SL has been ratcheted via
sub_tp and no longer matches the broker's actual order) or on reconcile()'s
drift detection (which only runs once per 4h bar).

Run standalone:
    python -m watcher.oanda_stream_listener
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger("oanda_stream")

# label -> (api_key, account_id) — mirrors trader/oanda_trader.py's _ACCOUNTS.
# One API key across every account; only the account ID differs. "mix" is
# the original account; short/mid/long are the 1h/4h/1D splits.
_OANDA_KEY = os.environ.get("OANDA_API_KEY", "")
_ACCOUNTS = {
    "mix":   (_OANDA_KEY, os.environ.get("OANDA_ACCOUNT_ID", "")),
    "short": (_OANDA_KEY, os.environ.get("OANDA_ACCOUNT_ID_SHORT", "")),
    "mid":   (_OANDA_KEY, os.environ.get("OANDA_ACCOUNT_ID_MID", "")),
    "long":  (_OANDA_KEY, os.environ.get("OANDA_ACCOUNT_ID_LONG", "")),
}

WEBHOOK_URL    = os.environ.get("OANDA_FILL_WEBHOOK_URL", "http://localhost:9999/oanda-fill")
WEBHOOK_SECRET = os.environ.get("TV_WEBHOOK_SECRET", "")

# OrderFillTransaction.reason values we care about — see OANDA v20 API docs.
_RELEVANT_REASONS = {"STOP_LOSS_ORDER", "TAKE_PROFIT_ORDER", "TRAILING_STOP_LOSS_ORDER"}

_MAX_BACKOFF = 120


def _forward(txn: dict, account_label: str) -> None:
    instrument = txn.get("instrument")
    reason     = txn.get("reason")
    price      = txn.get("price") or txn.get("fullPrice") or txn.get("fullVWAP")
    pl         = float(txn.get("pl", 0) or 0)

    trades_closed = txn.get("tradesClosed") or []
    trade_reduced = txn.get("tradeReduced") or {}
    trade_id = trades_closed[0]["tradeID"] if trades_closed else trade_reduced.get("tradeID")

    if not instrument or price is None:
        log.warning(f"[{account_label}] ORDER_FILL missing instrument/price, skipping: {txn}")
        return

    payload = {
        "instrument": instrument,
        "reason":     reason,
        "price":      float(price),
        "pl":         pl,
        "trade_id":   trade_id,
        "time":       txn.get("time"),
        "account":    account_label,
    }
    try:
        resp = requests.post(
            WEBHOOK_URL, params={"secret": WEBHOOK_SECRET}, json=payload, timeout=10,
        )
        log.info(f"[{account_label}] Forwarded {instrument} {reason} @ {price} pl={pl} -> "
                 f"{resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.error(f"[{account_label}] Failed to forward {instrument} {reason}: {e}")


def _handle_transaction(txn: dict, account_label: str) -> None:
    ttype = txn.get("type")
    if ttype != "ORDER_FILL":
        return  # includes HEARTBEAT and every other transaction type
    if txn.get("reason") not in _RELEVANT_REASONS:
        return
    _forward(txn, account_label)


def run_account(account_label: str, api_key: str, account_id: str) -> None:
    stream_url = f"https://stream-fxtrade.oanda.com/v3/accounts/{account_id}/transactions/stream"
    headers = {"Authorization": f"Bearer {api_key}", "Accept-Datetime-Format": "RFC3339"}
    backoff = 5
    while True:
        started = time.monotonic()
        try:
            log.info(f"[{account_label}] Connecting to OANDA transaction stream (account {account_id})...")
            with requests.get(stream_url, headers=headers, stream=True, timeout=90) as resp:
                if resp.status_code != 200:
                    log.error(f"[{account_label}] Stream connect failed: {resp.status_code} {resp.text[:300]}")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
                    continue

                log.info(f"[{account_label}] Connected — streaming transactions")
                backoff = 5
                for line in resp.iter_lines():
                    if not line:
                        continue
                    try:
                        txn = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        _handle_transaction(txn, account_label)
                    except Exception as e:
                        log.error(f"[{account_label}] Error handling transaction {txn.get('id')}: {e}")

                log.warning(f"[{account_label}] Stream ended without error — reconnecting")
        except requests.exceptions.RequestException as e:
            log.warning(f"[{account_label}] Stream disconnected ({e}) — reconnecting in {backoff}s")
        except Exception as e:
            log.error(f"[{account_label}] Unexpected error: {e} — reconnecting in {backoff}s")

        if time.monotonic() - started > 60:
            backoff = 5
        time.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF)


def run() -> None:
    threads = []
    for label, (api_key, account_id) in _ACCOUNTS.items():
        if not (api_key and account_id):
            log.warning(f"[{label}] no credentials configured — skipping this account's stream")
            continue
        t = threading.Thread(target=run_account, args=(label, api_key, account_id),
                             name=f"oanda-stream-{label}", daemon=True)
        t.start()
        threads.append(t)

    if not threads:
        log.error("No OANDA accounts configured at all — nothing to stream. Exiting.")
        return

    for t in threads:
        t.join()


if __name__ == "__main__":
    run()
