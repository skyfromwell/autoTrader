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

    # Recalculate TP/SL from actual fill
    tp = entry_px + atr_tp_mult * atr
    sl = entry_px - atr_sl_mult * atr

    # ── 3. TP limit order (sell, reduce-only) ────────────────────────────────
    def _px_wire(px: float) -> float:
        """Round to 5 significant figures."""
        if px == 0:
            return 0.0
        mag    = 10 ** (5 - int(np.floor(np.log10(abs(px)))) - 1)
        return round(px * mag) / mag

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
    }


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
