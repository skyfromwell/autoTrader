#!/usr/bin/env python3
from __future__ import annotations
"""Trade state, history, and cooldown tracking per symbol."""

import json
import logging
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

STATE_FILE = Path("output/position_state.json")
log = logging.getLogger(__name__)


@dataclass
class Trade:
    pair:          str
    direction:     str
    entry:         float
    tp:            float
    sl:            float
    atr:           float
    size:          float
    features:      dict
    bar_time:      Optional[str]
    open_time:     str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    closed:        bool = False
    close_price:   Optional[float] = None
    close_reason:  Optional[str]   = None
    result:        Optional[str]   = None
    protected_sl:  Optional[float] = None
    runner_active: bool = False
    runner_regime: Optional[str]   = None
    bars_since_close: int = 0
    price_triggers: list = field(default_factory=list)


class CooldownManager:
    def __init__(self):
        self._expiry: dict[str, datetime] = {}

    def activate(self, pair: str, sl_streak: int) -> None:
        hours = {1: 4, 2: 12, 3: 24}.get(min(sl_streak, 3), 48)
        self._expiry[pair] = datetime.now() + timedelta(hours=hours)
        log.info(f"[{pair}] Cooldown activated: {hours}h (streak={sl_streak})")

    def is_cooling_down(self, pair: str) -> bool:
        expiry = self._expiry.get(pair)
        return expiry is not None and datetime.now() < expiry

    def clear(self, pair: str) -> None:
        self._expiry.pop(pair, None)

    def remaining_minutes(self, pair: str) -> int:
        expiry = self._expiry.get(pair)
        if not expiry or datetime.now() >= expiry:
            return 0
        return int((expiry - datetime.now()).total_seconds() / 60)


BAR_SECONDS = 4 * 3600  # 4H bars


class PositionManager:
    def __init__(self):
        self._trades:       dict[str, Trade]        = {}
        self._history:      dict[str, list[Trade]]  = {}
        self._sl_streaks:   dict[str, int]          = {}
        # symbol_state: {pair: {"last_pull": iso, "last_signal": iso, "last_signal_val": int}}
        self._symbol_state: dict[str, dict]         = {}
        self.cooldown       = CooldownManager()
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
                self._sl_streaks   = data.get("sl_streaks", {})
                self._symbol_state = data.get("symbol_state", {})
                known = {f.name for f in fields(Trade)}
                for pair, td in data.get("open_trades", {}).items():
                    self._trades[pair] = Trade(**{k: v for k, v in td.items() if k in known})
        except Exception as e:
            log.warning(f"Could not load position state: {e}")

    def _save(self) -> None:
        try:
            STATE_FILE.parent.mkdir(exist_ok=True)
            open_trades = {}
            for pair, t in self._trades.items():
                open_trades[pair] = {k: v for k, v in t.__dict__.items()}
            STATE_FILE.write_text(json.dumps(
                {
                    "sl_streaks":   self._sl_streaks,
                    "symbol_state": self._symbol_state,
                    "open_trades":  open_trades,
                },
                indent=2
            ))
        except Exception as e:
            log.warning(f"Could not save position state: {e}")

    # ── Symbol pull / signal tracking ────────────────────────────────────────

    def record_pull(self, pair: str, signal_val: int = 0) -> int:
        """
        Record a successful data pull for pair. Returns bars missed since last pull
        (0 = on schedule, 1 = one bar late, etc.). Also records signal timestamp
        if signal_val is non-zero.
        """
        now     = datetime.now()
        state   = self._symbol_state.setdefault(pair, {})
        last_ts = state.get("last_pull")

        bars_missed = 0
        if last_ts:
            elapsed     = (now - datetime.fromisoformat(last_ts)).total_seconds()
            bars_missed = max(0, int(elapsed / BAR_SECONDS) - 1)

        state["last_pull"] = now.isoformat(timespec="seconds")
        if signal_val != 0:
            state["last_signal"]     = now.isoformat(timespec="seconds")
            state["last_signal_val"] = signal_val

        self._save()
        return bars_missed

    def get_symbol_state(self, pair: str) -> dict:
        return self._symbol_state.get(pair, {})

    # ── Trade CRUD ────────────────────────────────────────────────────────────

    def get_trade(self, pair: str) -> Optional[Trade]:
        return self._trades.get(pair)

    def open_trade(self, pair: str, direction: str, entry: float,
                   tp: float, sl: float, atr: float, size: float,
                   features: dict, bar_time=None) -> Trade:
        trade = Trade(pair=pair, direction=direction, entry=entry,
                      tp=tp, sl=sl, atr=atr, size=size,
                      features=features, bar_time=bar_time)
        self._trades[pair] = trade
        return trade

    def close_trade(self, pair: str, reason: str, close_price: float) -> None:
        trade = self._trades.pop(pair, None)
        if not trade:
            return
        trade.closed       = True
        trade.close_price  = close_price
        trade.close_reason = reason
        trade.result       = reason

        hist = self._history.setdefault(pair, [])
        for t in hist:
            t.bars_since_close += 1
        hist.append(trade)

        if reason == "sl_hit":
            self._sl_streaks[pair] = self._sl_streaks.get(pair, 0) + 1
            self.cooldown.activate(pair, self._sl_streaks[pair])
        else:
            self._sl_streaks[pair] = 0
            self.cooldown.clear(pair)

        self._save()

    def move_sl(self, pair: str, new_sl: float, reason: str = "") -> None:
        trade = self._trades.get(pair)
        if trade:
            trade.sl           = new_sl
            trade.protected_sl = new_sl
            self._save()

    def fire_price_trigger(self, pair: str, idx: int) -> None:
        """Remove a price trigger by index after it has fired."""
        trade = self._trades.get(pair)
        if trade and 0 <= idx < len(trade.price_triggers):
            trade.price_triggers.pop(idx)
            self._save()

    def update_runner_decision(self, pair: str, regime: str, confidence: float) -> None:
        trade = self._trades.get(pair)
        if trade:
            trade.runner_active = True
            trade.runner_regime = regime

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_history(self, pair: str, n: int = 4) -> list[Trade]:
        return list(self._history.get(pair, []))[-n:]

    def get_sl_streak(self, pair: str) -> int:
        return self._sl_streaks.get(pair, 0)

    def summary(self) -> str:
        if not self._trades:
            return "no open positions"
        parts = [f"{p}:{t.direction}@{t.entry:.4f}" for p, t in self._trades.items()]
        return "open: " + ", ".join(parts)
