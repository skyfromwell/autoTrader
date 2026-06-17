#!/usr/bin/env python3
from __future__ import annotations
"""
Hyperliquid broker integration — crypto execution via hyperliquid_trade.js.
"""

import json
import logging
import os
import subprocess
import urllib.request

log = logging.getLogger(__name__)

_JS_SCRIPT = os.path.join(
    os.path.dirname(__file__), "..", "..", "tradingview-mcp", "scripts", "hyperliquid_trade.js"
)
_NODE = os.environ.get("NODE_BIN", "node")
_HL_WALLET = os.environ.get("HL_WALLET_ADDRESS", "")
_HL_API = "https://api.hyperliquid.xyz/info"

_TV_TO_HL: dict[str, str] = {}
_leverage_cache: dict[str, int] = {}


def _hl_post(body: dict) -> dict:
    req = urllib.request.Request(
        _HL_API,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def _get_leverage(coin: str) -> int:
    """Return current account leverage for coin, defaulting to 1 if unknown."""
    if coin in _leverage_cache:
        return _leverage_cache[coin]
    try:
        data = _hl_post({"type": "clearinghouseState", "user": _HL_WALLET})
        for p in data.get("assetPositions", []):
            pos = p["position"]
            lev = pos.get("leverage", {})
            _leverage_cache[pos["coin"]] = int(lev.get("value", 1))
        return _leverage_cache.get(coin, 1)
    except Exception as e:
        log.warning(f"[HYPERLIQUID] could not fetch leverage for {coin}: {e}")
        return 1


def _hl_coin(tv_symbol: str) -> str:
    """Convert TradingView symbol (BYBIT:INJUSDC.P) → Hyperliquid coin name (INJ)."""
    if tv_symbol in _TV_TO_HL:
        return _TV_TO_HL[tv_symbol]
    base = tv_symbol.split(":")[1] if ":" in tv_symbol else tv_symbol
    # Strip exchange suffix: INJUSDC.P → INJ, ETHUSDT → ETH
    for sep in ("USDC.P", "USDT.P", "USDC", "USDT", "USD"):
        if sep in base:
            base = base.split(sep)[0]
            break
    return base


def execute_trade(tv_symbol: str, direction: str, price: float | None = None,
                  notional: int = 10_000, tp: float | None = None,
                  sl: float | None = None) -> dict:
    """Open a Hyperliquid perp position.

    `notional` is treated as margin (USDC). The actual position size sent to
    Hyperliquid = notional × leverage, matching the account's current leverage
    setting for that coin.
    """
    coin = _hl_coin(tv_symbol)
    is_long = direction.lower() in ("long", "buy")

    leverage = _get_leverage(coin)
    position_usd = notional * leverage

    cmd = [
        _NODE, os.path.abspath(_JS_SCRIPT),
        coin,
        "long" if is_long else "short",
        str(price or 0),
        str(tp) if tp is not None else "undefined",
        str(sl) if sl is not None else "undefined",
        "--size", str(position_usd),
    ]

    log.info(f"[HYPERLIQUID] {tv_symbol} → {coin} {direction.upper()}  "
             f"margin=${notional}  leverage={leverage}x  notional=${position_usd}  "
             f"tp={tp}  sl={sl}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30,
            cwd=os.path.dirname(os.path.abspath(_JS_SCRIPT)),
        )
        out = result.stdout.strip()
        data = json.loads(out) if out else {}
        if data.get("success"):
            log.info(f"[HYPERLIQUID] FILLED {coin}  fill={data.get('fill_price')}  "
                     f"size={data.get('size')}")
        else:
            log.error(f"[HYPERLIQUID] ORDER FAILED {coin}: {data.get('error')}")
        return data
    except subprocess.TimeoutExpired:
        log.error(f"[HYPERLIQUID] timeout placing {coin} order")
        return {"success": False, "error": "timeout"}
    except Exception as e:
        log.error(f"[HYPERLIQUID] exception: {e}")
        return {"success": False, "error": str(e)}


def close_position(tv_symbol: str) -> dict:
    """Close an open Hyperliquid position."""
    coin = _hl_coin(tv_symbol)
    cmd = [_NODE, os.path.abspath(_JS_SCRIPT), coin, "--close"]
    log.info(f"[HYPERLIQUID] closing {coin}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            cwd=os.path.dirname(os.path.abspath(_JS_SCRIPT)),
        )
        return json.loads(result.stdout.strip() or "{}")
    except Exception as e:
        log.error(f"[HYPERLIQUID] close failed: {e}")
        return {"success": False, "error": str(e)}
