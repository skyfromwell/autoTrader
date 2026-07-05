"""
position_ledger.py — T+0 position accounting.

Tracks old_shares (T+1 sellable today) vs new_shares (bought today, locked until tomorrow).
At start of each trading day call unlock() to promote new_shares → old_shares.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class PositionLedger:
    symbol: str
    old_shares: int = 0     # sellable today
    new_shares: int = 0     # bought today — locked, cannot sell until tomorrow
    avg_cost: float = 0.0

    # T action tracking within current session
    t_sell_qty: int = 0         # shares sold in current T cycle
    t_sell_price: float = 0.0   # price at which T sell was made
    t_buy_qty: int = 0          # shares bought back
    t_buy_price: float = 0.0

    def sell(self, qty: int, price: float) -> int:
        """Sell from old_shares. Returns actual qty sold (rounded down to lot of 100)."""
        qty = _lot(min(qty, self.old_shares))
        if qty <= 0:
            log.warning(f"[{self.symbol}] sell rejected — old_shares={self.old_shares}, requested={qty}")
            return 0
        self.old_shares -= qty
        self.t_sell_qty = qty
        self.t_sell_price = price
        self.t_buy_qty = 0
        self.t_buy_price = 0.0
        log.info(f"[{self.symbol}] SELL {qty} @ {price:.3f}  remaining_old={self.old_shares}")
        return qty

    def buy(self, qty: int, price: float) -> int:
        """Buy shares → goes into new_shares (locked today). Returns actual qty bought."""
        qty = _lot(qty)
        if qty <= 0:
            return 0
        total_value = self.avg_cost * (self.old_shares + self.new_shares) + price * qty
        self.new_shares += qty
        total_qty = self.old_shares + self.new_shares
        self.avg_cost = total_value / total_qty if total_qty > 0 else price
        self.t_buy_qty = qty
        self.t_buy_price = price
        log.info(f"[{self.symbol}] BUY  {qty} @ {price:.3f}  new_shares={self.new_shares}  avg_cost={self.avg_cost:.3f}")
        return qty

    def resell(self, qty: int, price: float) -> int:
        """Resell new_shares bought earlier today (T+0 second sell). Returns qty sold."""
        # new_shares cannot be sold on A-shares normally — only old_shares can.
        # Resell here means we sell old_shares that we hadn't sold yet (second T action).
        qty = _lot(min(qty, self.old_shares))
        if qty <= 0:
            log.warning(f"[{self.symbol}] resell rejected — old_shares={self.old_shares}")
            return 0
        self.old_shares -= qty
        log.info(f"[{self.symbol}] RESELL {qty} @ {price:.3f}  remaining_old={self.old_shares}")
        return qty

    def unlock(self):
        """Call at start of each new trading day: promote new_shares → old_shares."""
        self.old_shares += self.new_shares
        self.new_shares = 0
        self.t_sell_qty = 0
        self.t_sell_price = 0.0
        self.t_buy_qty = 0
        self.t_buy_price = 0.0
        log.info(f"[{self.symbol}] Day unlock — sellable={self.old_shares}")

    def buy_back_qty(self) -> int:
        """How many shares to buy back (matches the T sell qty)."""
        return self.t_sell_qty - self.t_buy_qty

    @property
    def total_shares(self) -> int:
        return self.old_shares + self.new_shares

    @property
    def sellable(self) -> int:
        return self.old_shares

    def __str__(self) -> str:
        return (f"{self.symbol}: old={self.old_shares} new={self.new_shares} "
                f"avg_cost={self.avg_cost:.3f} | T_sell={self.t_sell_qty}@{self.t_sell_price:.3f} "
                f"T_buy={self.t_buy_qty}@{self.t_buy_price:.3f}")


def _lot(qty: int) -> int:
    """Round down to nearest 100-share lot."""
    return (qty // 100) * 100
