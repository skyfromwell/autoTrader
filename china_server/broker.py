#!/usr/bin/env python3
"""
xtquant broker wrapper — thin layer over miniQMT's XtQuantTrader.
All order logic lives here; api.py just translates HTTP ↔ these calls.
"""
from __future__ import annotations
import logging
import os
import time

from xtquant import xtconstant
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount

log = logging.getLogger(__name__)

ACCOUNT_ID = os.getenv("QMT_ACCOUNT", "测试66801935")
QMT_PATH   = os.getenv("QMT_PATH", r"P:\XUNTOU\金融街证券QMT模拟 - 交易终端\userdata_mini")

_trader:   XtQuantTrader | None = None
_acc:      StockAccount   | None = None
_connected: bool = False


class _Callback(XtQuantTraderCallback):
    def on_connected(self):
        global _connected
        _connected = True
        log.info("miniQMT on_connected fired — ready to trade")

    def on_disconnected(self):
        global _connected
        _connected = False
        log.warning("miniQMT disconnected")

    def on_account_status(self, status):
        log.info(f"account_status: {status}")


def connect() -> None:
    global _trader, _acc, _connected
    _connected = False
    _trader = XtQuantTrader(QMT_PATH, 1, callback=_Callback())
    _acc    = StockAccount(ACCOUNT_ID)
    _trader.start()
    result = _trader.connect()
    if result != 0:
        raise RuntimeError(f"miniQMT connect failed: code={result}")
    _trader.subscribe(_acc)
    log.info(f"miniQMT connect() returned 0 — waiting for account data...")
    _wait_for_account()


def _wait_for_account(timeout: int = 30) -> None:
    global _connected
    for i in range(timeout):
        asset = _trader.query_stock_asset(_acc)
        if asset is not None:
            _connected = True
            log.info(f"Account ready after {i+1}s — cash={asset.cash}  total={asset.total_asset}")
            return
        time.sleep(1)
    log.warning(f"Account data still None after {timeout}s — orders may be rejected")


def reconnect() -> dict:
    global _trader, _acc, _connected
    try:
        if _trader:
            _trader.stop()
    except Exception:
        pass
    connect()
    return {"status": "reconnected", "connected": _connected}


def _t() -> XtQuantTrader:
    if _trader is None:
        raise RuntimeError("Broker not connected — call connect() first")
    return _trader


def _price_type(symbol: str, price: float) -> int:
    if price > 0:
        return xtconstant.FIX_PRICE
    # Exchange-specific safe market orders (best-5-layers IOC)
    if symbol.endswith(".SH") or symbol.endswith(".BJ"):
        return xtconstant.MARKET_SH_CONVERT_5_CANCEL
    return xtconstant.MARKET_SZ_INSTBUSI_RESTCANCEL   # SZSE IOC


def place_order(symbol: str, direction: str, volume: int, price: float = 0) -> int:
    """
    symbol:    '600036.SH' or '000725.SZ'
    direction: 'buy' or 'sell'
    volume:    shares — must be multiple of 100 for A-shares
    price:     0 = market order
    Returns:   order_id (int)
    """
    order_type = xtconstant.STOCK_BUY if direction == "buy" else xtconstant.STOCK_SELL
    pt         = _price_type(symbol, price)
    order_id   = _t().order_stock(
        account      = _acc,
        stock_code   = symbol,
        order_type   = order_type,
        order_volume = volume,
        price_type   = pt,
        price        = price if price > 0 else 0,
        strategy_name= "autoTrader",
        order_remark = "",
    )
    log.info(f"order_stock  {direction.upper()} {volume} {symbol} "
             f"price={'market' if price == 0 else price}  → order_id={order_id}")
    return order_id


def cancel_order(order_id: int) -> bool:
    result = _t().cancel_order_stock(_acc, order_id)
    ok = result == 0
    log.info(f"cancel_order {order_id}  result={result}  ok={ok}")
    return ok


def get_positions() -> list[dict]:
    positions = _t().query_stock_positions(_acc)
    return [
        {
            "symbol":       p.stock_code,
            "volume":       p.volume,
            "available":    p.can_use_volume,
            "entry_price":  p.open_price,
            "market_value": p.market_value,
            "frozen":       p.frozen_volume,
        }
        for p in (positions or [])
        if p.volume > 0
    ]


def get_account() -> dict:
    asset = _t().query_stock_asset(_acc)
    if asset is None:
        return {"account_id": ACCOUNT_ID, "cash": None, "frozen_cash": None,
                "market_value": None, "total_asset": None, "note": "no data (market closed?)"}
    return {
        "account_id":   getattr(asset, "account_id",   ACCOUNT_ID),
        "cash":         getattr(asset, "cash",          None),
        "frozen_cash":  getattr(asset, "frozen_cash",   None),
        "market_value": getattr(asset, "market_value",  None),
        "total_asset":  getattr(asset, "total_asset",   None),
    }


def get_orders() -> list[dict]:
    orders = _t().query_stock_orders(_acc)
    return [
        {
            "order_id":     o.order_id,
            "symbol":       o.stock_code,
            "direction":    "buy" if o.order_type == xtconstant.STOCK_BUY else "sell",
            "volume":       o.order_volume,
            "filled":       o.traded_volume,
            "price":        o.price,
            "status":       o.order_status,
            "status_msg":   o.status_msg,
        }
        for o in (orders or [])
    ]
