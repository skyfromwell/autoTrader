#!/usr/bin/env python3
"""
China A-share broker client — calls the FastAPI bridge running on the
remote miniQMT machine over Tailscale.

Env vars:
    CHINA_SERVER_URL   http://<tailscale-ip>:8888
    CHINA_API_KEY      must match the server's CHINA_API_KEY
"""
from __future__ import annotations
import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

log        = logging.getLogger(__name__)
_BASE_URL  = os.getenv("CHINA_SERVER_URL", "http://localhost:8888")
_API_KEY   = os.getenv("CHINA_API_KEY", "")
_TIMEOUT   = 10


def _headers() -> dict:
    return {"X-API-Key": _API_KEY, "Content-Type": "application/json"}


def _tv_to_xt(pair: str) -> str:
    """Convert TradingView symbol to xtquant stock code.
    SSE:600036  → 600036.SH
    SZSE:000725 → 000725.SZ
    """
    prefix, code = pair.split(":", 1)
    suffix = {"SSE": ".SH", "SZSE": ".SZ"}.get(prefix.upper(), ".SH")
    return f"{code}{suffix}"


def _round_to_lot(volume: float) -> int:
    """A-shares trade in lots of 100."""
    return max(100, int(volume // 100) * 100)


def execute_trade(pair: str, direction: str,
                  price: float = 0, notional: int = 10000) -> dict:
    """
    Place a market order on the China bridge.

    pair:      TV symbol, e.g. 'SSE:600036' or 'SZSE:000725'
    direction: 'long' → buy, 'short' → sell
    price:     last price used to compute share volume; 0 falls back to /account
    notional:  CNY amount to deploy
    """
    symbol    = _tv_to_xt(pair)
    side      = "buy" if direction == "long" else "sell"
    volume    = _round_to_lot(notional / price) if price > 0 else 100

    log.info(f"[{pair}] china execute_trade  {side.upper()} {volume} shares "
             f"symbol={symbol}  notional=¥{notional:,}")

    resp = requests.post(
        f"{_BASE_URL}/order",
        json={"symbol": symbol, "direction": side,
              "volume": volume, "price": 0},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    log.info(f"[{pair}] order placed  order_id={data.get('order_id')}")
    return data


def close_position(pair: str, volume: int | None = None) -> dict:
    """Sell (close) an existing A-share position."""
    symbol = _tv_to_xt(pair)

    if volume is None:
        positions = get_positions()
        pos = next((p for p in positions if p["symbol"] == symbol), None)
        if not pos:
            log.warning(f"[{pair}] close_position: no open position found")
            return {}
        volume = pos["available"]

    volume = _round_to_lot(volume)
    log.info(f"[{pair}] close_position  SELL {volume} shares  symbol={symbol}")

    resp = requests.post(
        f"{_BASE_URL}/order",
        json={"symbol": symbol, "direction": "sell",
              "volume": volume, "price": 0},
        headers=_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def cancel_order(order_id: int) -> bool:
    resp = requests.delete(f"{_BASE_URL}/order/{order_id}",
                           headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("cancelled", False)


def get_positions() -> list[dict]:
    resp = requests.get(f"{_BASE_URL}/positions",
                        headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_account() -> dict:
    resp = requests.get(f"{_BASE_URL}/account",
                        headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_orders() -> list[dict]:
    resp = requests.get(f"{_BASE_URL}/orders",
                        headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()
