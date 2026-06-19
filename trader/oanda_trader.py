#!/usr/bin/env python3
from __future__ import annotations
"""
Oanda broker integration — forex execution via REST API v3.
"""

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)

_BASE    = os.environ.get("OANDA_BASE_URL", "https://api-fxtrade.oanda.com/v3")
_KEY     = os.environ.get("OANDA_API_KEY", "")
_ACCOUNT = os.environ.get("OANDA_ACCOUNT_ID", "")

# Map TradingView FX symbols → Oanda instrument names
_TV_TO_OANDA: dict[str, str] = {
    "FX:EURUSD":  "EUR_USD",
    "FX:GBPUSD":  "GBP_USD",
    "FX:USDJPY":  "USD_JPY",
    "FX:AUDUSD":  "AUD_USD",
    "FX:NZDUSD":  "NZD_USD",
    "FX:USDCAD":  "USD_CAD",
    "FX:USDCHF":  "USD_CHF",
    "OANDA:EURUSD": "EUR_USD",
    "OANDA:GBPUSD": "GBP_USD",
    "OANDA:USDJPY": "USD_JPY",
    "OANDA:AUDUSD": "AUD_USD",
    "OANDA:NZDUSD": "NZD_USD",
}


def _oanda_instrument(tv_symbol: str) -> str:
    if tv_symbol in _TV_TO_OANDA:
        return _TV_TO_OANDA[tv_symbol]
    # Generic fallback: FX:EURUSD → EUR_USD
    base = tv_symbol.split(":")[1] if ":" in tv_symbol else tv_symbol
    if len(base) == 6:
        return base[:3].upper() + "_" + base[3:].upper()
    return base.upper()


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url  = f"{_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {_KEY}",
            "Content-Type":  "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _price_str(px: float, instrument: str) -> str:
    """Format price to correct decimal places for the instrument."""
    decimals = 3 if "JPY" in instrument else 5
    return f"{px:.{decimals}f}"


def execute_trade(tv_symbol: str, direction: str, price: float | None = None,
                  notional: int = 10_000, tp: float | None = None,
                  sl: float | None = None) -> dict:
    """Open a forex position via Oanda market order."""
    instrument = _oanda_instrument(tv_symbol)
    is_long    = direction.lower() in ("long", "buy")
    units      = str(notional if is_long else -notional)

    order: dict = {
        "type":          "MARKET",
        "instrument":    instrument,
        "units":         units,
        "timeInForce":   "FOK",
        "positionFill":  "DEFAULT",
    }
    if tp is not None:
        order["takeProfitOnFill"] = {
            "price":       _price_str(tp, instrument),
            "timeInForce": "GTC",
        }
    if sl is not None:
        order["stopLossOnFill"] = {
            "price":       _price_str(sl, instrument),
            "timeInForce": "GTC",
        }

    log.info(f"[OANDA] {tv_symbol} → {instrument} {direction.upper()}  "
             f"units={units}  tp={tp}  sl={sl}")
    try:
        result = _request("POST", f"/accounts/{_ACCOUNT}/orders", {"order": order})
        fill   = result.get("orderFillTransaction", {})
        filled = bool(fill.get("tradeOpened") or fill.get("tradesClosed"))
        price_filled = fill.get("price")
        trade_id     = fill.get("tradeOpened", {}).get("tradeID")
        if filled:
            log.info(f"[OANDA] FILLED {instrument}  price={price_filled}  trade={trade_id}")
        else:
            log.warning(f"[OANDA] unexpected response: {result}")
        return {"success": filled, "fill_price": price_filled, "trade_id": trade_id, "raw": result}
    except Exception as e:
        log.error(f"[OANDA] order failed: {e}")
        return {"success": False, "error": str(e)}


def close_position(tv_symbol: str) -> dict:
    """Close all units of an open Oanda position."""
    instrument = _oanda_instrument(tv_symbol)
    log.info(f"[OANDA] closing {instrument}")
    try:
        result = _request("PUT", f"/accounts/{_ACCOUNT}/positions/{instrument}/close",
                          {"longUnits": "ALL", "shortUnits": "ALL"})
        return {"success": True, "raw": result}
    except Exception as e:
        log.error(f"[OANDA] close failed: {e}")
        return {"success": False, "error": str(e)}
