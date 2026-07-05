"""
state_machine.py — 4-state T+0 trading state machine.

States:
  IDLE       — holding old_shares, no T action in progress, watching for sell signal
  SOLD       — sold old_shares, waiting for price to dip to buy back
  BOUGHT     — bought back, waiting for price to recover to resell
  DONE       — full T cycle complete (sell→buy→resell); resets to IDLE

Regime parameters (from regime_bridge) control trigger thresholds:
  t_mode: "aggressive" | "normal" | "conservative"
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from t0.data_feed import MarketState
from t0.position_ledger import PositionLedger

log = logging.getLogger(__name__)


class State(str, Enum):
    IDLE    = "IDLE"
    SOLD    = "SOLD"
    BOUGHT  = "BOUGHT"
    DONE    = "DONE"


@dataclass
class RegimeConfig:
    t_mode: str = "normal"

    # % above VWAP to trigger initial sell
    sell_above_vwap: float = 0.010
    # % below sell_price to trigger buy-back
    buy_below_sell: float = 0.020
    # % above buy_price to trigger resell
    resell_above_buy: float = 0.015
    # Max fraction of old_shares to sell per T cycle (0.5 = half position)
    sell_fraction: float = 0.5
    # Don't initiate new T sell after this time (HH:MM, China local)
    no_new_sell_after: str = "14:30"
    # Force buy-back if still SOLD after this time
    force_buyback_by: str = "14:45"

    _PRESETS = {
        "aggressive":   {"sell_above_vwap": 0.005, "buy_below_sell": 0.012, "resell_above_buy": 0.008},
        "normal":       {"sell_above_vwap": 0.010, "buy_below_sell": 0.020, "resell_above_buy": 0.015},
        "conservative": {"sell_above_vwap": 0.020, "buy_below_sell": 0.030, "resell_above_buy": 0.025},
    }

    def apply_mode(self, t_mode: str) -> None:
        preset = self._PRESETS.get(t_mode, self._PRESETS["normal"])
        self.t_mode = t_mode
        for k, v in preset.items():
            setattr(self, k, v)
        log.info(f"[RegimeConfig] mode={t_mode}  sell_above_vwap={self.sell_above_vwap:.1%}  "
                 f"buy_below_sell={self.buy_below_sell:.1%}  resell_above_buy={self.resell_above_buy:.1%}")


@dataclass
class Signal:
    action: str          # "sell" | "buy" | "resell" | "hold" | "force_buy"
    qty: int = 0
    reason: str = ""


class T0StateMachine:
    def __init__(self, ledger: PositionLedger, config: RegimeConfig):
        self.ledger  = ledger
        self.config  = config
        self.state   = State.IDLE
        self._cycles = 0          # completed T cycles today

    def on_tick(self, mkt: MarketState) -> Signal:
        """Evaluate current market state and return the next action signal."""
        now_str = _hhmm()

        if self.state == State.IDLE:
            return self._check_sell(mkt, now_str)

        elif self.state == State.SOLD:
            return self._check_buy(mkt, now_str)

        elif self.state == State.BOUGHT:
            return self._check_resell(mkt, now_str)

        elif self.state == State.DONE:
            # After completing a cycle, check if we can start another
            self.state = State.IDLE
            self._cycles += 1
            log.info(f"[{self.ledger.symbol}] T cycle #{self._cycles} complete — resetting to IDLE")
            return Signal("hold", reason="cycle_complete_reset")

        return Signal("hold")

    # ── State checks ─────────────────────────────────────────────────────────

    def _check_sell(self, mkt: MarketState, now_str: str) -> Signal:
        if self.ledger.sellable < 100:
            return Signal("hold", reason="no_sellable_shares")
        if now_str >= self.config.no_new_sell_after:
            return Signal("hold", reason="past_sell_cutoff")
        if mkt.price_vs_vwap >= self.config.sell_above_vwap:
            qty = max(100, int(self.ledger.sellable * self.config.sell_fraction // 100) * 100)
            self.state = State.SOLD
            log.info(f"[{self.ledger.symbol}] IDLE→SOLD  price={mkt.last_price:.3f}  "
                     f"vwap={mkt.vwap:.3f}  vs_vwap={mkt.price_vs_vwap:+.2%}  qty={qty}")
            return Signal("sell", qty=qty, reason=f"price {mkt.price_vs_vwap:+.2%} above VWAP")
        return Signal("hold")

    def _check_buy(self, mkt: MarketState, now_str: str) -> Signal:
        sell_px = self.ledger.t_sell_price
        if sell_px == 0:
            sell_px = mkt.last_price  # fallback

        drop_pct = (sell_px - mkt.last_price) / sell_px
        force    = now_str >= self.config.force_buyback_by

        if force:
            qty = self.ledger.buy_back_qty()
            self.state = State.BOUGHT
            log.info(f"[{self.ledger.symbol}] SOLD→BOUGHT (FORCED EOD)  qty={qty}")
            return Signal("buy", qty=qty, reason="force_buyback_eod")

        if drop_pct >= self.config.buy_below_sell:
            qty = self.ledger.buy_back_qty()
            self.state = State.BOUGHT
            log.info(f"[{self.ledger.symbol}] SOLD→BOUGHT  drop={drop_pct:.2%}  qty={qty}")
            return Signal("buy", qty=qty, reason=f"price dropped {drop_pct:.2%} from sell")

        return Signal("hold")

    def _check_resell(self, mkt: MarketState, now_str: str) -> Signal:
        buy_px   = self.ledger.t_buy_price
        if buy_px == 0:
            buy_px = mkt.last_price

        rise_pct = (mkt.last_price - buy_px) / buy_px

        if rise_pct >= self.config.resell_above_buy:
            # Resell the old_shares we still hold (not new_shares — those are locked)
            qty = self.ledger.sellable
            if qty < 100:
                # No old shares left to resell — just complete the cycle
                self.state = State.DONE
                return Signal("hold", reason="no_old_shares_for_resell")
            self.state = State.DONE
            log.info(f"[{self.ledger.symbol}] BOUGHT→DONE  rise={rise_pct:.2%}  qty={qty}")
            return Signal("resell", qty=qty, reason=f"price recovered {rise_pct:.2%} from buy")

        return Signal("hold")

    def force_reset(self) -> None:
        """Reset to IDLE at end of day."""
        log.info(f"[{self.ledger.symbol}] State machine reset  state={self.state} → IDLE")
        self.state = State.IDLE

    def __str__(self) -> str:
        return (f"T0StateMachine({self.ledger.symbol})  state={self.state}  "
                f"cycles={self._cycles}  mode={self.config.t_mode}")


def _hhmm() -> str:
    """Current time as HH:MM string (local machine time, should be CST on China server)."""
    return time.strftime("%H:%M")
