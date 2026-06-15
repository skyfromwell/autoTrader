#!/usr/bin/env python3
from __future__ import annotations
"""
Paper Trading Engine — Alpaca Markets.
Receives signals from the watcher and places simulated trades.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

TRADE_NOTIONAL    = 1_000     # USD per position
MAX_POSITIONS     = 10
STOP_LOSS_PCT     = 5.0
TAKE_PROFIT_PCT   = 15.0

OUTPUT_DIR  = Path("output")
TRADE_LOG   = OUTPUT_DIR / "trade_log.csv"

log = logging.getLogger(__name__)


# ── Alpaca client ─────────────────────────────────────────────────────────────

def _get_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")
    from alpaca.trading.client import TradingClient
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)


def _open_position_symbols(client) -> set[str]:
    try:
        return {p.symbol for p in client.get_all_positions()}
    except Exception as e:
        log.error(f"Could not fetch positions: {e}")
        return set()


# ── Trade execution ───────────────────────────────────────────────────────────

def _alpaca_symbol(tv_symbol: str) -> str:
    """Convert TradingView symbol (e.g. LSE:HSBA) to Alpaca format.
    Non-US symbols on Alpaca use ticker only for paper trading OTC lookup.
    Adjust this mapping as needed when live trading.
    """
    return tv_symbol.split(":")[-1]


def _place_order(client, symbol: str, side: str, notional: float) -> bool:
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    alpaca_side = OrderSide.BUY if side == "long" else OrderSide.SELL
    try:
        order = client.submit_order(MarketOrderRequest(
            symbol=symbol,
            notional=notional,
            side=alpaca_side,
            time_in_force=TimeInForce.DAY,
        ))
        log.info(f"  Order submitted: {order.id}  {side.upper()} {symbol}  ${notional:.0f}")
        return True
    except Exception as e:
        log.error(f"  Order failed for {symbol}: {e}")
        return False


def _close_position(client, symbol: str) -> bool:
    try:
        client.close_position(symbol)
        log.info(f"  Closed position: {symbol}")
        return True
    except Exception as e:
        log.warning(f"  Could not close {symbol}: {e}")
        return False


def _log_trade(tv_symbol: str, direction: str, notional: float,
               status: str, note: str = "") -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    record = pd.DataFrame([{
        "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tv_symbol":  tv_symbol,
        "direction":  direction,
        "notional":   notional,
        "status":     status,
        "note":       note,
    }])
    record.to_csv(TRADE_LOG, mode="a", header=not TRADE_LOG.exists(), index=False)


# ── Public entry point ────────────────────────────────────────────────────────

def execute_trade(tv_symbol: str, direction: str, price: float | None = None) -> bool:
    """
    Place a paper trade on Alpaca for the given TradingView symbol and direction.
    direction: 'long' | 'short'
    Returns True if order was placed successfully.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    symbol = _alpaca_symbol(tv_symbol)

    log.info(f"TRADE  {tv_symbol} ({symbol})  {direction.upper()}  price≈{price}")

    try:
        client = _get_client()
    except RuntimeError as e:
        log.error(str(e))
        _log_trade(tv_symbol, direction, 0, "SKIPPED", str(e))
        return False

    open_positions = _open_position_symbols(client)

    if len(open_positions) >= MAX_POSITIONS and symbol not in open_positions:
        log.info(f"  Max positions ({MAX_POSITIONS}) reached — skipping {symbol}")
        _log_trade(tv_symbol, direction, 0, "SKIPPED", "max positions reached")
        return False

    # Close opposite position on flip
    if symbol in open_positions:
        log.info(f"  Closing existing position before flip: {symbol}")
        _close_position(client, symbol)

    success = _place_order(client, symbol, direction, TRADE_NOTIONAL)
    status  = "PLACED" if success else "FAILED"
    _log_trade(tv_symbol, direction, TRADE_NOTIONAL, status)
    return success


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s %(message)s",
        handlers=[logging.FileHandler(OUTPUT_DIR / "trader.log"), logging.StreamHandler()],
    )
    # Quick test: print open positions
    try:
        client = _get_client()
        positions = client.get_all_positions()
        print(f"Open positions ({len(positions)}):")
        for p in positions:
            print(f"  {p.symbol}  {p.side}  qty={p.qty}  uPNL=${float(p.unrealized_pl):.2f}")
    except Exception as e:
        print(f"Error: {e}")
