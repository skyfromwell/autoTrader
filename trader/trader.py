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

TRADE_NOTIONAL    = 10_000    # USD default (A-grade=20K, B-grade=10K)
MAX_POSITIONS     = 20
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


def _cancel_bracket_orders(client, symbol: str) -> None:
    """Cancel any open stop/limit orders for this symbol (clear stale SL/TP before close or re-bracket)."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    try:
        orders = client.get_orders(filter=GetOrdersRequest(
            symbol=symbol, status=QueryOrderStatus.OPEN))
        for o in orders:
            if str(o.order_type) in ("stop", "limit"):
                client.cancel_order_by_id(o.id)
                log.info(f"  Cancelled {o.order_type} order {o.id} for {symbol}")
    except Exception as e:
        log.warning(f"  Could not cancel bracket orders for {symbol}: {e}")


def _place_order(client, symbol: str, side: str, notional: float,
                 tp: float | None = None, sl: float | None = None,
                 entry_price: float | None = None) -> bool:
    from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, StopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    alpaca_side = OrderSide.BUY if side == "long" else OrderSide.SELL
    close_side  = OrderSide.SELL if side == "long" else OrderSide.BUY
    try:
        # Alpaca requires qty (not notional) for short sells; use qty whenever entry_price known
        if entry_price and entry_price > 0:
            qty_shares = round(notional / entry_price, 6)
            order = client.submit_order(MarketOrderRequest(
                symbol=symbol, qty=qty_shares, side=alpaca_side,
                time_in_force=TimeInForce.DAY))
        elif alpaca_side == OrderSide.BUY:
            order = client.submit_order(MarketOrderRequest(
                symbol=symbol, notional=notional, side=alpaca_side,
                time_in_force=TimeInForce.DAY))
        else:
            log.error(f"  Cannot short {symbol}: no entry price to calculate qty")
            return False
        log.info(f"  Order submitted: {order.id}  {side.upper()} {symbol}  ${notional:.0f}")

        if entry_price and entry_price > 0 and (tp or sl):
            qty = round(notional / entry_price, 6)
            if tp:
                try:
                    client.submit_order(LimitOrderRequest(
                        symbol=symbol, qty=qty, side=close_side,
                        limit_price=round(tp, 2), time_in_force=TimeInForce.GTC))
                    log.info(f"  TP limit: {close_side} {qty} {symbol} @ {tp:.2f}")
                except Exception as e:
                    log.warning(f"  TP order failed for {symbol}: {e}")
            if sl:
                try:
                    client.submit_order(StopOrderRequest(
                        symbol=symbol, qty=qty, side=close_side,
                        stop_price=round(sl, 2), time_in_force=TimeInForce.GTC))
                    log.info(f"  SL stop: {close_side} {qty} {symbol} @ {sl:.2f}")
                except Exception as e:
                    log.warning(f"  SL order failed for {symbol}: {e}")
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

def execute_trade(tv_symbol: str, direction: str, price: float | None = None,
                  notional: int = TRADE_NOTIONAL, tp: float | None = None,
                  sl: float | None = None) -> bool:
    """
    Place a paper trade on Alpaca for the given TradingView symbol and direction.
    direction: 'long' | 'short'
    notional: USD position size (A-grade=20K, B-grade=10K)
    tp/sl: price levels — bracket orders placed after market fill if entry price known
    Returns True if order was placed successfully.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    symbol = _alpaca_symbol(tv_symbol)

    log.info(f"TRADE  {tv_symbol} ({symbol})  {direction.upper()}  price≈{price}  ${notional:,}  tp={tp}  sl={sl}")

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

    # Close opposite position on flip (cancel its bracket orders first)
    if symbol in open_positions:
        log.info(f"  Closing existing position before flip: {symbol}")
        _cancel_bracket_orders(client, symbol)
        _close_position(client, symbol)

    success = _place_order(client, symbol, direction, notional,
                           tp=tp, sl=sl, entry_price=price)
    status  = "PLACED" if success else "FAILED"
    _log_trade(tv_symbol, direction, notional, status)
    return success


def close_alpaca_position(tv_symbol: str) -> bool:
    """Close an Alpaca position and cancel its open bracket orders."""
    symbol = _alpaca_symbol(tv_symbol)
    try:
        client = _get_client()
        _cancel_bracket_orders(client, symbol)
        return _close_position(client, symbol)
    except RuntimeError as e:
        log.error(str(e))
        return False


def move_alpaca_sl(tv_symbol: str, new_sl: float, direction: str) -> bool:
    """Cancel existing stop order for this symbol and place a new one at new_sl."""
    symbol = _alpaca_symbol(tv_symbol)
    try:
        client = _get_client()
        from alpaca.trading.requests import GetOrdersRequest, StopOrderRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide, TimeInForce
        close_side = OrderSide.SELL if direction == "long" else OrderSide.BUY
        # Cancel existing stop orders on the close side
        orders = client.get_orders(filter=GetOrdersRequest(
            symbol=symbol, status=QueryOrderStatus.OPEN))
        for o in orders:
            if str(o.order_type) == "stop" and o.side == close_side:
                client.cancel_order_by_id(o.id)
                log.info(f"  Cancelled stop order {o.id} for {symbol}")
        # Place new stop at updated SL level
        pos = client.get_open_position(symbol)
        qty = abs(float(pos.qty))
        client.submit_order(StopOrderRequest(
            symbol=symbol, qty=qty, side=close_side,
            stop_price=round(new_sl, 2), time_in_force=TimeInForce.GTC))
        log.info(f"  New SL stop: {close_side} {qty} {symbol} @ {new_sl:.2f}")
        return True
    except Exception as e:
        log.error(f"  move_alpaca_sl failed for {symbol}: {e}")
        return False


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
