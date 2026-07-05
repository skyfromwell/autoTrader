"""
data_feed.py — Intraday tick data and VWAP/high-low calculations.

MockTickFeed:      generates synthetic ticks for local testing.
XtQuantTickFeed:   subscribes to real ticks via xtquant on the Windows QMT machine.
"""
from __future__ import annotations
import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class Tick:
    symbol: str
    price: float
    volume: int       # shares in this tick
    timestamp: float  # unix seconds

    def __str__(self) -> str:
        return f"{self.symbol} {self.price:.3f} vol={self.volume} t={self.timestamp:.0f}"


@dataclass
class MarketState:
    symbol: str
    last_price: float = 0.0
    vwap: float = 0.0
    day_high: float = 0.0
    day_low: float = float("inf")
    open_price: float = 0.0
    cum_volume: int = 0
    cum_turnover: float = 0.0   # price × volume accumulated

    def update(self, tick: Tick) -> None:
        self.last_price = tick.price
        self.day_high = max(self.day_high, tick.price)
        self.day_low = min(self.day_low, tick.price)
        self.cum_volume += tick.volume
        self.cum_turnover += tick.price * tick.volume
        self.vwap = self.cum_turnover / self.cum_volume if self.cum_volume else tick.price
        if self.open_price == 0.0:
            self.open_price = tick.price

    @property
    def price_vs_vwap(self) -> float:
        """Positive = above VWAP, negative = below."""
        return (self.last_price - self.vwap) / self.vwap if self.vwap else 0.0

    @property
    def price_vs_open(self) -> float:
        return (self.last_price - self.open_price) / self.open_price if self.open_price else 0.0

    def __str__(self) -> str:
        return (f"{self.symbol}: {self.last_price:.3f}  VWAP={self.vwap:.3f}  "
                f"H={self.day_high:.3f}  L={self.day_low:.3f}  "
                f"vs_vwap={self.price_vs_vwap:+.2%}")


class BaseTickFeed(ABC):
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.state = MarketState(symbol=symbol)
        self._callbacks: list[Callable[[Tick, MarketState], None]] = []

    def subscribe(self, callback: Callable[[Tick, MarketState], None]) -> None:
        self._callbacks.append(callback)

    def _emit(self, tick: Tick) -> None:
        self.state.update(tick)
        for cb in self._callbacks:
            cb(tick, self.state)

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


class MockTickFeed(BaseTickFeed):
    """Synthetic tick feed for local testing. Simulates intraday price walk."""

    def __init__(self, symbol: str, start_price: float = 10.0,
                 tick_interval: float = 1.0, volatility: float = 0.002):
        super().__init__(symbol)
        self._price = start_price
        self._interval = tick_interval
        self._vol = volatility
        self._running = False
        self._thread = None

    def start(self) -> None:
        import threading
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info(f"[MockTickFeed] started {self.symbol} @ {self._price:.3f}")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while self._running:
            # random walk with mean reversion toward open
            drift = (self.state.open_price - self._price) * 0.001 if self.state.open_price else 0
            change = random.gauss(drift, self._vol * self._price)
            self._price = max(0.01, self._price + change)
            volume = random.randint(100, 10000)
            tick = Tick(self.symbol, round(self._price, 3), volume, time.time())
            self._emit(tick)
            time.sleep(self._interval)


class XtQuantTickFeed(BaseTickFeed):
    """
    Real tick feed via xtquant on the Windows QMT machine.

    To implement:
    1. Install xtquant on the Windows machine running miniQMT.
    2. Call xt_trader.subscribe(symbol) and wire on_tick → self._emit().
    3. See xtquant docs: xtquant.xttype.XtTick for field names.
    """

    def __init__(self, symbol: str):
        super().__init__(symbol)
        raise NotImplementedError(
            "XtQuantTickFeed requires xtquant running on the Windows QMT machine. "
            "Fill in the subscription logic using xtquant's callback API."
        )

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError
