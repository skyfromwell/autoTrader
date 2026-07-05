"""
executor.py — Order execution layer for T+0 trading.

MockExecutor:      prints orders, no real trades (local testing).
BrokerAPIExecutor: routes to the miniQMT China bridge via china_trader.
"""
from __future__ import annotations
import logging
import sys
from abc import ABC, abstractmethod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from t0.position_ledger import PositionLedger, _lot

log = logging.getLogger(__name__)


class BaseExecutor(ABC):
    @abstractmethod
    def sell(self, ledger: PositionLedger, qty: int, price: float) -> dict: ...

    @abstractmethod
    def buy(self, ledger: PositionLedger, qty: int, price: float) -> dict: ...

    @abstractmethod
    def resell(self, ledger: PositionLedger, qty: int, price: float) -> dict: ...


class MockExecutor(BaseExecutor):
    """Simulates order execution locally — no real orders placed."""

    def sell(self, ledger: PositionLedger, qty: int, price: float) -> dict:
        actual = ledger.sell(qty, price)
        result = {"action": "sell", "qty": actual, "price": price, "mock": True}
        log.info(f"[MOCK] SELL {actual} {ledger.symbol} @ {price:.3f}  {result}")
        return result

    def buy(self, ledger: PositionLedger, qty: int, price: float) -> dict:
        actual = ledger.buy(qty, price)
        result = {"action": "buy", "qty": actual, "price": price, "mock": True}
        log.info(f"[MOCK] BUY  {actual} {ledger.symbol} @ {price:.3f}  {result}")
        return result

    def resell(self, ledger: PositionLedger, qty: int, price: float) -> dict:
        actual = ledger.resell(qty, price)
        result = {"action": "resell", "qty": actual, "price": price, "mock": True}
        log.info(f"[MOCK] RESELL {actual} {ledger.symbol} @ {price:.3f}  {result}")
        return result


class BrokerAPIExecutor(BaseExecutor):
    """
    Live executor — routes orders to the miniQMT bridge via china_trader.

    The China bridge runs on Tailscale at 100.77.221.56:8888.
    CHINA_SERVER_URL and CHINA_API_KEY must be set in .env.
    """

    def __init__(self):
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / ".env")
        from trader.china_trader import execute_trade, close_position
        self._execute  = execute_trade
        self._close    = close_position

    def sell(self, ledger: PositionLedger, qty: int, price: float) -> dict:
        actual = ledger.sell(qty, price)
        if actual <= 0:
            return {"action": "sell", "qty": 0, "skipped": True, "reason": "no_sellable_shares"}
        try:
            result = self._close(ledger.symbol, volume=actual)
            log.info(f"[BROKER] SELL {actual} {ledger.symbol} @ {price:.3f}  {result}")
            return {"action": "sell", "qty": actual, "price": price, **result}
        except Exception as e:
            ledger.old_shares += actual  # rollback ledger
            ledger.t_sell_qty = 0
            log.error(f"[BROKER] sell failed for {ledger.symbol}: {e}")
            return {"action": "sell", "error": str(e)}

    def buy(self, ledger: PositionLedger, qty: int, price: float) -> dict:
        actual = _lot(qty)
        if actual <= 0:
            return {"action": "buy", "qty": 0, "skipped": True}
        try:
            notional = int(actual * price)
            result   = self._execute(ledger.symbol, direction="long",
                                     price=price, notional=notional)
            ledger.buy(actual, price)
            log.info(f"[BROKER] BUY  {actual} {ledger.symbol} @ {price:.3f}  {result}")
            return {"action": "buy", "qty": actual, "price": price, **result}
        except Exception as e:
            log.error(f"[BROKER] buy failed for {ledger.symbol}: {e}")
            return {"action": "buy", "error": str(e)}

    def resell(self, ledger: PositionLedger, qty: int, price: float) -> dict:
        actual = ledger.resell(qty, price)
        if actual <= 0:
            return {"action": "resell", "qty": 0, "skipped": True, "reason": "no_old_shares"}
        try:
            result = self._close(ledger.symbol, volume=actual)
            log.info(f"[BROKER] RESELL {actual} {ledger.symbol} @ {price:.3f}  {result}")
            return {"action": "resell", "qty": actual, "price": price, **result}
        except Exception as e:
            ledger.old_shares += actual  # rollback
            log.error(f"[BROKER] resell failed for {ledger.symbol}: {e}")
            return {"action": "resell", "error": str(e)}
