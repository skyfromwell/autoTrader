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

_BASE = os.environ.get("OANDA_BASE_URL", "https://api-fxtrade.oanda.com/v3")

# OANDA uses a single API key across every sub-account on the login — only
# the account ID changes. Four accounts, split by signal timeframe:
#   mix   — the original account; everything not otherwise tagged (legacy
#           positions, any timeframe without a dedicated split below)
#   short — 1h signals   (account 001-001-10065189-013)
#   mid   — 4h signals   (account 001-001-10065189-009)
#   long  — 1D signals   (account 001-001-10065189-012)
# Pairs routed to short/mid/long are tracked internally under a distinct
# OANDA_SHORT:/OANDA_MID:/OANDA_LONG: prefix (see tv_alert_server.py's
# _route_forex_account()) so the same instrument on two different timeframes
# never collides as the same dict key in position_state.json. "mix" keeps
# the original "OANDA:" prefix — existing tracked positions need no migration.
_OANDA_KEY = os.environ.get("OANDA_API_KEY", "")
_ACCOUNTS = {
    "mix":   {"key": _OANDA_KEY, "account": os.environ.get("OANDA_ACCOUNT_ID", "")},
    "short": {"key": _OANDA_KEY, "account": os.environ.get("OANDA_ACCOUNT_ID_SHORT", "")},
    "mid":   {"key": _OANDA_KEY, "account": os.environ.get("OANDA_ACCOUNT_ID_MID", "")},
    "long":  {"key": _OANDA_KEY, "account": os.environ.get("OANDA_ACCOUNT_ID_LONG", "")},
}

_PREFIX_ACCOUNT = {
    "OANDA":       "mix",
    "FX":          "mix",
    "OANDA_SHORT": "short",
    "OANDA_MID":   "mid",
    "OANDA_LONG":  "long",
}


def account_for(tv_symbol: str) -> str:
    prefix = tv_symbol.split(":", 1)[0].upper() if ":" in tv_symbol else ""
    return _PREFIX_ACCOUNT.get(prefix, "mix")


def account_id_for(tv_symbol: str) -> str:
    return _ACCOUNTS[account_for(tv_symbol)]["account"]


# Two-tier margin wall, mirroring mcp_processor.py's existing Hyperliquid
# pattern (warn early, block hard) — applied per-account now, since each
# account's own margin usage should gate its own new entries rather than a
# single shared account's status blocking (or failing to block) trades on
# accounts it has nothing to do with.
_MARGIN_WARN_PCT  = 0.40
_MARGIN_BLOCK_PCT = 0.45


def margin_status(account: str) -> dict:
    """Query OANDA's marginCloseoutPercent for one of the 4 accounts.
    Returns {"pct", "available", "warn", "block"}. Missing credentials or a
    failed request are treated as blocked — safe default, matching the
    existing "margin check error: allowing/blocking" conventions elsewhere
    in this repo, erring toward blocking here since margin safety is the
    whole point of the check."""
    creds = _ACCOUNTS.get(account, _ACCOUNTS["mix"])
    if not (creds["key"] and creds["account"]):
        return {"pct": None, "available": 0.0, "warn": True, "block": True}
    try:
        data  = _request("GET", f"/accounts/{creds['account']}/summary", account=account)["account"]
        pct   = float(data.get("marginCloseoutPercent", 0))
        avail = float(data.get("marginAvailable", 0))
        return {
            "pct":       pct,
            "available": avail,
            "warn":      pct >= _MARGIN_WARN_PCT,
            "block":     pct >= _MARGIN_BLOCK_PCT,
        }
    except Exception as e:
        log.warning(f"[OANDA:{account}] margin check failed: {e} — blocking to be safe")
        return {"pct": None, "available": 0.0, "warn": True, "block": True}

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


def _request(method: str, path: str, body: dict | None = None,
             account: str = "mix") -> dict:
    url  = f"{_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {_ACCOUNTS[account]['key']}",
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

    account = account_for(tv_symbol)
    acct_id = _ACCOUNTS[account]["account"]
    log.info(f"[OANDA:{account}] {tv_symbol} → {instrument} {direction.upper()}  "
             f"units={units}  tp={tp}  sl={sl}")
    try:
        result = _request("POST", f"/accounts/{acct_id}/orders", {"order": order}, account=account)
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


def close_position(tv_symbol: str, units: int | str = "ALL",
                   direction: str | None = None) -> dict:
    """Close an Oanda position. Pass units + direction to close only part of it
    (e.g. a software-managed remainder left over after the FIFO safeguard
    blocked a broker-side TP/SL on a same-size trade)."""
    instrument = _oanda_instrument(tv_symbol)
    units_str  = str(units)
    if direction is None:
        body = {"longUnits": "ALL", "shortUnits": "ALL"}
    elif direction.lower() in ("long", "buy"):
        body = {"longUnits": units_str}
    else:
        body = {"shortUnits": units_str}
    account = account_for(tv_symbol)
    acct_id = _ACCOUNTS[account]["account"]
    log.info(f"[OANDA:{account}] closing {instrument}  {body}")
    try:
        result = _request("PUT", f"/accounts/{acct_id}/positions/{instrument}/close", body, account=account)
        return {"success": True, "raw": result}
    except Exception as e:
        log.error(f"[OANDA:{account}] close failed: {e}")
        return {"success": False, "error": str(e)}
