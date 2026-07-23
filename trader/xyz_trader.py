#!/usr/bin/env python3
from __future__ import annotations
"""
HIP-3 xyz DEX trader — gold, silver, crude oil perps on Hyperliquid.

Differences from standard HL perps:
  - All info calls need dex="xyz"
  - Isolated margin only (no cross-margin pool)
  - Must call update_isolated_margin BEFORE placing an order
  - Exchange initialised with perp_dexs=["xyz"]
"""

import logging
import os
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.signing import OrderType

load_dotenv(Path(__file__).parent.parent / ".env")

log = logging.getLogger(__name__)

_BASE_URL = "https://api.hyperliquid.xyz"
_DEX      = "xyz"

# ── Singleton exchange / info clients ─────────────────────────────────────────

_exchange: Exchange | None = None
_info:     Info     | None = None


def _clients() -> tuple[Exchange, Info]:
    global _exchange, _info
    if _exchange is None:
        pk     = os.environ["HL_PRIVATE_KEY"]
        wallet = Account.from_key(pk)
        _info     = Info(_BASE_URL, skip_ws=True, perp_dexs=[_DEX])
        _exchange = Exchange(wallet, _BASE_URL, perp_dexs=[_DEX])
    return _exchange, _info


# ── Market data helpers ───────────────────────────────────────────────────────

def get_mid(coin: str) -> float:
    """Return mid price for an xyz DEX coin (e.g. 'xyz:GOLD')."""
    _, info = _clients()
    book = info.l2_snapshot(coin)
    bid  = float(book["levels"][0][0]["px"])
    ask  = float(book["levels"][1][0]["px"])
    return (bid + ask) / 2


def compute_atr(coin: str, period: int = 14) -> float:
    """ATR from daily candles via xyz candleSnapshot."""
    _, info = _clients()
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - (period + 5) * 86_400_000
    candles  = info.candles_snapshot(coin, "1d", start_ms, end_ms)
    if not candles or len(candles) < period + 1:
        raise ValueError(f"{coin}: not enough candle data for ATR({period})")

    highs  = np.array([float(c["h"]) for c in candles])
    lows   = np.array([float(c["l"]) for c in candles])
    closes = np.array([float(c["c"]) for c in candles])

    prev_close = closes[:-1]
    h, l, pc   = highs[1:], lows[1:], prev_close
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return float(tr[-period:].mean())


def sz_decimals(coin: str) -> int:
    _, info = _clients()
    asset = info.name_to_asset(coin)
    return info.asset_to_sz_decimals[asset]


# ── Execution ─────────────────────────────────────────────────────────────────

def open_long(
    coin: str,
    margin_usd: float = 100.0,
    leverage:   int   = 5,
    atr_tp_mult: float = 8.0,
    atr_sl_mult: float = 3.0,
    slippage:   float = 0.01,
) -> dict:
    """
    Open a long on xyz DEX:
      1. update_isolated_margin(margin_usd)
      2. market_open at margin * leverage notional
      3. place TP limit + SL trigger as reduce-only orders

    Returns summary dict with entry, tp, sl, size, atr.
    """
    exchange, info = _clients()

    # ── Prices & ATR ─────────────────────────────────────────────────────────
    mid = get_mid(coin)
    atr = compute_atr(coin)
    tp  = mid + atr_tp_mult * atr
    sl  = mid - atr_sl_mult * atr

    notional = margin_usd * leverage
    dec      = sz_decimals(coin)
    size     = round(notional / mid, dec)

    log.info(f"[{coin}] mid=${mid:.4f}  atr={atr:.4f}  "
             f"tp=${tp:.4f}  sl=${sl:.4f}  size={size}  margin=${margin_usd}  lev={leverage}x")

    # ── 1. Set isolated margin ────────────────────────────────────────────────
    log.info(f"[{coin}] setting isolated margin ${margin_usd}")
    margin_res = exchange.update_isolated_margin(margin_usd, coin)
    log.info(f"[{coin}] margin result: {margin_res}")

    # ── 2. Entry market order ─────────────────────────────────────────────────
    log.info(f"[{coin}] placing market long  size={size}")
    entry_res = exchange.market_open(coin, is_buy=True, sz=size, slippage=slippage)
    log.info(f"[{coin}] entry result: {entry_res}")

    # Extract fill price if available
    statuses = (entry_res.get("response", {}).get("data", {}).get("statuses", [{}]))
    filled   = statuses[0].get("filled", {})
    entry_px = float(filled.get("avgPx", mid)) if filled else mid
    entry_oid = filled.get("oid")

    # Recalculate TP/SL from actual fill
    tp = entry_px + atr_tp_mult * atr
    sl = entry_px - atr_sl_mult * atr

    # ── 3. TP limit order (sell, reduce-only) ────────────────────────────────
    log.info(f"[{coin}] placing TP at ${tp:.4f}")
    tp_res = exchange.order(
        coin,
        is_buy=False,
        sz=size,
        limit_px=_px_wire(tp),
        order_type=OrderType({"limit": {"tif": "Gtc"}}),
        reduce_only=True,
    )
    log.info(f"[{coin}] TP result: {tp_res}")

    # ── 4. SL trigger order (sell, reduce-only) ───────────────────────────────
    log.info(f"[{coin}] placing SL at ${sl:.4f}")
    sl_res = exchange.order(
        coin,
        is_buy=False,
        sz=size,
        limit_px=_px_wire(sl * 0.95),          # limit below trigger for guaranteed fill
        order_type=OrderType({"trigger": {"isMarket": True, "triggerPx": _px_wire(sl), "tpsl": "sl"}}),
        reduce_only=True,
    )
    log.info(f"[{coin}] SL result: {sl_res}")

    return {
        "coin":    coin,
        "mid":     mid,
        "entry":   entry_px,
        "atr":     atr,
        "tp":      tp,
        "sl":      sl,
        "size":    size,
        "margin":  margin_usd,
        "leverage": leverage,
        "oid":     entry_oid,
    }


# ── SL management ────────────────────────────────────────────────────────────

def move_sl(coin: str, new_sl: float, direction: str) -> dict:
    """
    Cancel the existing SL order on xyz DEX and place a new one at new_sl.
    direction: 'long' or 'short'
    """
    exchange, info = _clients()
    wallet = os.environ["HL_WALLET_ADDRESS"]

    from trader.hyperliquid_trader import _hl_post
    open_orders = _hl_post({'type': 'openOrders', 'user': wallet, 'dex': 'xyz'})
    coin_orders  = [o for o in open_orders if o['coin'] == coin]

    is_long = direction.lower() == 'long'
    if is_long:
        # SL for long = lowest sell (trigger) order
        sl_orders = sorted(
            [o for o in coin_orders if o['side'] == 'A'],
            key=lambda o: float(o['limitPx'])
        )
        sl_order = sl_orders[0] if sl_orders else None
    else:
        # SL for short = highest buy (trigger) order
        sl_orders = sorted(
            [o for o in coin_orders if o['side'] == 'B'],
            key=lambda o: float(o['limitPx']), reverse=True
        )
        sl_order = sl_orders[0] if sl_orders else None

    asset = info.name_to_asset(coin)
    dec   = sz_decimals(coin)

    # Cancel old SL
    if sl_order:
        from hyperliquid.utils.signing import CancelRequest
        result = exchange.cancel(coin, sl_order['oid'])
        log.info(f"[{coin}] cancelled old SL oid={sl_order['oid']}  result={result}")

    # Get current size from position
    from trader.hyperliquid_trader import _hl_post
    state = _hl_post({'type': 'clearinghouseState', 'user': wallet, 'dex': 'xyz'})
    pos   = next((ap['position'] for ap in state.get('assetPositions', [])
                  if ap['position']['coin'] == coin), None)
    size  = round(abs(float(pos['szi'])), dec) if pos else (sl_order['sz'] if sl_order else 0)

    # Place new SL trigger
    new_sl_wired = _px_wire(new_sl)
    limit_px     = _px_wire(new_sl * 0.95 if is_long else new_sl * 1.05)
    from hyperliquid.utils.signing import OrderType
    result = exchange.order(
        coin,
        is_buy=not is_long,
        sz=float(size),
        limit_px=limit_px,
        order_type=OrderType({"trigger": {"isMarket": True, "triggerPx": new_sl_wired, "tpsl": "sl"}}),
        reduce_only=True,
    )
    log.info(f"[{coin}] new SL placed at ${new_sl}  result={result}")
    return result


# ── TV symbol routing ────────────────────────────────────────────────────────

_TV_TO_XYZ: dict[str, str] = {
    # jingda's current TV prefix for xyz-DEX commodities (as of 2026-07-15a-subtp)
    "HIP3XYZ:GOLDUSDC.P":         "xyz:GOLD",
    "HIP3XYZ:SILVERUSDC.P":       "xyz:SILVER",
    "HIP3XYZ:BRENTOILUSDC.P":     "xyz:BRENTOIL",
    "HIP3XYZ:CLUSDC.P":           "xyz:CL",
    # Older Hyperliquid-prefixed TV symbols (kept for backward compat)
    "HYPERLIQUID:GOLDUSDC.P":     "xyz:GOLD",
    "HYPERLIQUID:SILVERUSDC.P":   "xyz:SILVER",
    "HYPERLIQUID:BRENTOILUSDC.P": "xyz:BRENTOIL",
    "HYPERLIQUID:CLUSDC.P":       "xyz:CL",
    # Legacy TV symbols (kept for backward compat)
    "COINBASE:GOLDUSDC.P":        "xyz:GOLD",
    "COINBASE:SILVERUSDC.P":      "xyz:SILVER",
    "XYZ:GOLD":                   "xyz:GOLD",
    "XYZ:SILVER":                 "xyz:SILVER",
    "XYZ:CL":                     "xyz:CL",
    "XYZ:BRENTOIL":               "xyz:BRENTOIL",
}

def tv_to_xyz(tv_pair: str) -> str | None:
    return _TV_TO_XYZ.get(tv_pair.upper())


def open_short(
    coin: str,
    margin_usd: float = 100.0,
    leverage:   int   = 5,
    atr_tp_mult: float = 8.0,
    atr_sl_mult: float = 3.0,
    slippage:   float = 0.01,
) -> dict:
    exchange, info = _clients()
    mid = get_mid(coin)
    atr = compute_atr(coin)
    tp  = mid - atr_tp_mult * atr
    sl  = mid + atr_sl_mult * atr
    notional = margin_usd * leverage
    dec  = sz_decimals(coin)
    size = round(notional / mid, dec)

    log.info(f"[{coin}] SHORT mid=${mid:.4f}  atr={atr:.4f}  tp=${tp:.4f}  sl=${sl:.4f}")
    exchange.update_isolated_margin(margin_usd, coin)
    entry_res = exchange.market_open(coin, is_buy=False, sz=size, slippage=slippage)
    statuses  = entry_res.get("response", {}).get("data", {}).get("statuses", [{}])
    filled    = statuses[0].get("filled", {})
    entry_px  = float(filled.get("avgPx", mid)) if filled else mid

    tp = entry_px - atr_tp_mult * atr
    sl = entry_px + atr_sl_mult * atr

    # TP limit (buy back, reduce-only)
    exchange.order(coin, is_buy=True, sz=size, limit_px=_px_wire(tp),
                   order_type=OrderType({"limit": {"tif": "Gtc"}}), reduce_only=True)
    # SL trigger
    exchange.order(coin, is_buy=True, sz=size, limit_px=_px_wire(sl * 1.05),
                   order_type=OrderType({"trigger": {"isMarket": True, "triggerPx": _px_wire(sl), "tpsl": "sl"}}),
                   reduce_only=True)

    return {"coin": coin, "mid": mid, "entry": entry_px, "atr": atr, "tp": tp, "sl": sl,
            "size": size, "margin": margin_usd, "leverage": leverage, "oid": filled.get("oid")}


def execute_trade(tv_pair: str, direction: str, price: float | None = None,
                  notional: int = 10_000) -> dict:
    """Route TV symbol to xyz open_long / open_short."""
    coin = tv_to_xyz(tv_pair)
    if not coin:
        raise ValueError(f"Unknown xyz TV pair: {tv_pair}")
    mid  = get_mid(coin)
    # Price check: skip if execution would be worse than signal price
    if price and price > 0:
        if direction == "short" and mid < price * 0.998:
            log.warning(f"[{coin}] SHORT skip — mid {mid:.4f} < signal {price:.4f} (price moved away)")
            return {"skipped": True, "reason": "price_worse_than_signal", "mid": mid, "signal": price}
        if direction == "long" and mid > price * 1.002:
            log.warning(f"[{coin}] LONG skip — mid {mid:.4f} > signal {price:.4f} (price moved away)")
            return {"skipped": True, "reason": "price_worse_than_signal", "mid": mid, "signal": price}
    margin = notional // 5   # default 5x leverage for xyz
    if direction == "long":
        return open_long(coin, margin_usd=margin)
    else:
        return open_short(coin, margin_usd=margin)


def close_position(tv_pair: str) -> dict:
    """Close all xyz position for a TV symbol."""
    coin = tv_to_xyz(tv_pair)
    if not coin:
        raise ValueError(f"Unknown xyz TV pair: {tv_pair}")
    exchange, _ = _clients()
    wallet = os.environ["HL_WALLET_ADDRESS"]
    from trader.hyperliquid_trader import _hl_post
    state = _hl_post({'type': 'clearinghouseState', 'user': wallet, 'dex': 'xyz'})
    pos   = next((ap['position'] for ap in state.get('assetPositions', [])
                  if ap['position']['coin'] == coin), None)
    if not pos or float(pos['szi']) == 0:
        return {"skipped": True, "reason": "no_position"}
    szi    = float(pos['szi'])
    is_buy = szi < 0   # closing short → buy back; closing long → sell
    size   = round(abs(szi), sz_decimals(coin))
    # market_close doesn't work for xyz DEX — use market_open in opposite direction
    result = exchange.market_open(coin, is_buy=is_buy, sz=size, slippage=0.01)
    fill   = result.get('response', {}).get('data', {}).get('statuses', [{}])[0].get('filled', {})
    avg_px = float(fill.get('avgPx', 0))
    log.info(f"[{coin}] closed {size} @ {avg_px}  result={result}")
    return {"coin": coin, "closed": True, "size": size, "avg_px": avg_px}


def _px_wire(px: float) -> float:
    if px == 0:
        return 0.0
    mag = 10 ** (5 - int(np.floor(np.log10(abs(px)))) - 1)
    return round(px * mag) / mag


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s %(message)s",
    )

    TRADES = [
        {"coin": "xyz:GOLD",     "label": "Gold"},
        {"coin": "xyz:SILVER",   "label": "Silver"},
        {"coin": "xyz:BRENTOIL", "label": "Brent Oil"},
        {"coin": "xyz:CL",       "label": "WTI Crude"},
    ]

    results = []
    for t in TRADES:
        log.info(f"\n{'='*50}\n  Opening {t['label']} long\n{'='*50}")
        try:
            res = open_long(
                coin=t["coin"],
                margin_usd=100.0,
                leverage=5,
                atr_tp_mult=8.0,
                atr_sl_mult=3.0,
            )
            results.append({"status": "ok", **res})
        except Exception as e:
            log.error(f"{t['coin']}: FAILED — {e}")
            results.append({"coin": t["coin"], "status": "error", "error": str(e)})
        time.sleep(1)

    print("\n" + "="*60)
    print("  SUMMARY")
    print("="*60)
    for r in results:
        if r["status"] == "ok":
            print(f"  {r['coin']:16s}  entry=${r['entry']:.4f}  "
                  f"tp=${r['tp']:.4f} (+{r['atr']*8:.4f})  "
                  f"sl=${r['sl']:.4f} (-{r['atr']*3:.4f})  "
                  f"size={r['size']}  margin=${r['margin']}")
        else:
            print(f"  {r['coin']:16s}  ERROR: {r['error']}")
