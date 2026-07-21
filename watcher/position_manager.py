#!/usr/bin/env python3
from __future__ import annotations
"""Trade state, history, and cooldown tracking per symbol."""

import contextlib
import fcntl
import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Fields in position_state.json that the file is authoritative for.
# When the file is newer than our last save, every listed field is synced
# from disk into memory — so manual edits and TV-alert writes always win.
_EXTERNAL_EDIT_FIELDS = frozenset({
    "direction", "entry", "size",
    "tp", "sl", "atr", "price_triggers",
    "runner_active", "protected_sl",
    "closed", "close_price", "close_reason", "result",
    "manual_tp", "manual_sl",
    "watcher_tp", "watcher_sl",
    "sl_tp_source", "software_watch_units",
    "opened_by",
})

STATE_FILE    = Path("output/position_state.json")
LOCK_FILE     = Path("output/position_state.json.lock")
INTENDED_FILE = Path("output/intended_positions.json")
LEDGER_FILE   = Path("output/trade_history.jsonl")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "2130465973")

_CLOSE_REASON_LABELS = {
    "sl_hit":                    "🔴 Stop Loss Triggered",
    "tp_hit":                    "🟢 Take Profit Triggered",
    "runner_exit":               "🏁 Runner Exit",
    "low_confidence":            "🏁 Runner Exit (low confidence)",
    "consolidation":             "🏁 Runner Exit (consolidation)",
    "reversal":                  "🏁 Runner Exit (reversal)",
    "mixed":                     "🏁 Runner Exit (mixed regime)",
    "reconcile_not_on_exchange": "⚠️ Position Closed (drift — confirm on broker)",
    "reconcile_not_on_broker":   "⚠️ Position Closed (drift — confirm on broker)",
}


def _notify_close(trade: "Trade", pnl_pct: Optional[float], win_loss: Optional[str]) -> None:
    """Push a close notification to Telegram — the equivalent of the native
    'Stop Loss Triggered' / 'Take Profit Triggered' alerts OANDA's own app
    sends, but covering every broker this bot trades on (OANDA has no such
    alert for Hyperliquid, Alpaca, or China positions at all).
    """
    if not TELEGRAM_TOKEN:
        return
    try:
        label = _CLOSE_REASON_LABELS.get(trade.close_reason, f"Position Closed ({trade.close_reason})")
        pnl_str = f"{pnl_pct:+.2f}% ({win_loss})" if pnl_pct is not None else "n/a"
        text = (
            f"{label}\n"
            f"{trade.pair}  {trade.direction.upper()}\n"
            f"Entry: {trade.entry:.5f}  →  Close: {trade.close_price:.5f}\n"
            f"P&L: {pnl_str}"
        )
        payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=payload, headers={"Content-Type": "application/json"},
        ), timeout=8)
    except Exception as e:
        log.warning(f"[{trade.pair}] could not send close notification: {e}")

# A trade younger than this can't plausibly have round-tripped through disk
# yet across every writer of position_state.json — see _save()'s delete guard.
_DELETE_GRACE_SECONDS = 60


@contextlib.contextmanager
def _state_lock():
    """Exclusive advisory lock spanning STATE_FILE's read-merge-write cycle.

    Multiple processes (tv_alert_server, watcher, qmt_results_watcher) share
    this file with no other coordination. Without this lock, one process can
    read a pre-edit snapshot, sit on it while doing broker I/O, then write it
    back out after another process's edit has already landed — silently
    reverting that edit even though each individual write looked correct in
    isolation.
    """
    LOCK_FILE.parent.mkdir(exist_ok=True)
    with open(LOCK_FILE, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@dataclass
class IntendedTrade:
    """
    A signal that fired and is intended to become a real position.
    Lives in intended_positions.json until the broker confirms execution,
    at which point it is removed and a Trade is written to position_state.json.
    Pending-margin signals also stay here until margin frees up.
    """
    pair:          str
    direction:     str
    signal_price:  float
    tp:            Optional[float]
    sl:            Optional[float]
    atr:           float
    size:          float
    notional:      int
    features:      dict
    bar_time:      Optional[str]
    status:        str            = "pending_execution"  # pending_execution | pending_margin
    created_at:    str            = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    margin_required: Optional[float] = None
    timeframe:     str            = "240"  # TradingView minutes-string: "60"=1h, "240"=4h


# Bar duration in hours, keyed by the TradingView timeframe strings this repo
# actually produces. A signal is considered stale once it's this many bars
# old with no broker confirmation — two full closed bars, since a bar or two
# of network/margin/risk-gate delay is normal but by the third bar the chart
# has moved on and the entry no longer reflects current price action.
_INTENDED_STALE_BARS = 2
_TIMEFRAME_HOURS = {"60": 1, "240": 4, "1D": 24}


@dataclass
class Trade:
    """A broker-confirmed open position tracked in position_state.json."""
    pair:          str
    direction:     str
    entry:         float
    tp:            Optional[float]   = None
    sl:            Optional[float]   = None
    atr:           float             = 0.0
    size:          float             = 0.0
    notional:      Optional[int]     = None
    features:      dict              = field(default_factory=dict)
    bar_time:      Optional[str]     = None
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
    # TP/SL split: watcher-calculated vs manual override
    watcher_tp: Optional[float] = None
    watcher_sl: Optional[float] = None
    manual_tp:  Optional[float] = None
    manual_sl:  Optional[float] = None
    # "broker" = TP/SL live as a real OANDA order; "software" = no broker order
    # exists (blocked by FIFO safeguard) and forex_sl_tp_watcher.py must close
    # software_watch_units itself when price crosses tp/sl; "mixed" = only part
    # of size has a broker order, software_watch_units covers the remainder.
    sl_tp_source: Optional[str] = None
    software_watch_units: float = 0.0
    # Who created this trade record: "tv_alert", "watcher", "manual", etc.
    # Lets the external-edit merge in _save() attribute a disk change to its
    # source instead of silently letting one writer clobber another's trade.
    opened_by: Optional[str] = None


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


class IntendedPositionManager:
    """
    Manages intended_positions.json — signals that passed all gates but
    haven't yet been confirmed by a broker.  Two states live here:
      pending_execution  — sent to broker, awaiting confirmation
      pending_margin     — margin insufficient; retry after next close
    On broker success the entry is removed and a Trade is written to
    position_state via PositionManager.open_trade().
    """

    def __init__(self):
        self._intended: dict[str, IntendedTrade] = {}
        self._load()

    def _load(self) -> None:
        try:
            if INTENDED_FILE.exists():
                data  = json.loads(INTENDED_FILE.read_text())
                known = {f.name for f in fields(IntendedTrade)}
                for pair, td in data.items():
                    self._intended[pair] = IntendedTrade(
                        **{k: v for k, v in td.items() if k in known})
        except Exception as e:
            log.warning(f"Could not load intended positions: {e}")

    def _save(self) -> None:
        try:
            INTENDED_FILE.parent.mkdir(exist_ok=True)
            INTENDED_FILE.write_text(json.dumps(
                {pair: t.__dict__ for pair, t in self._intended.items()},
                indent=2,
            ))
        except Exception as e:
            log.warning(f"Could not save intended positions: {e}")

    def add(self, pair: str, direction: str, signal_price: float,
            tp: Optional[float], sl: Optional[float], atr: float,
            size: float, notional: int, features: dict,
            bar_time=None, timeframe: str = "240") -> IntendedTrade:
        t = IntendedTrade(pair=pair, direction=direction,
                          signal_price=signal_price, tp=tp, sl=sl,
                          atr=atr, size=size, notional=notional,
                          features=features, bar_time=bar_time,
                          timeframe=timeframe)
        self._intended[pair] = t
        self._save()
        log.info(f"[{pair}] 📋 Intended {direction.upper()} signal recorded "
                 f"signal_price={signal_price} notional=${notional:,}")
        return t

    def evict_stale(self, now: Optional[datetime] = None) -> list[str]:
        """Remove intended trades that are >= _INTENDED_STALE_BARS bars old.

        These are signals that passed every entry gate and were queued for
        broker execution, but never got confirmed — stuck on margin, dropped
        by a network hiccup, or just orphaned by a broker error partway
        through. By the second closed bar the chart has moved on and the
        recorded entry/tp/sl no longer reflect current price action, so
        there's nothing left to retry against — only to discard.
        """
        now = now or datetime.now()
        evicted = []
        for pair, t in list(self._intended.items()):
            hours = _TIMEFRAME_HOURS.get(t.timeframe, 4)
            try:
                age_hours = (now - datetime.fromisoformat(t.created_at)).total_seconds() / 3600
            except (TypeError, ValueError):
                continue
            if age_hours >= _INTENDED_STALE_BARS * hours:
                log.info(f"[{pair}] 🗑️ Intended signal stale "
                         f"({age_hours:.1f}h old, {t.timeframe}min timeframe, "
                         f"status={t.status}) — removing")
                del self._intended[pair]
                evicted.append(pair)
        if evicted:
            self._save()
        return evicted

    def mark_pending_margin(self, pair: str, margin_required: float) -> None:
        t = self._intended.get(pair)
        if t:
            t.status           = "pending_margin"
            t.margin_required  = margin_required
            self._save()
            log.warning(f"[{pair}] ⏳ PENDING MARGIN — need ${margin_required:,.0f}, "
                        f"signal kept for retry")

    def remove(self, pair: str) -> None:
        if pair in self._intended:
            del self._intended[pair]
            self._save()

    def get(self, pair: str) -> Optional[IntendedTrade]:
        return self._intended.get(pair)

    def has(self, pair: str) -> bool:
        return pair in self._intended

    def get_pending_margin(self) -> list[tuple[str, IntendedTrade]]:
        return [(p, t) for p, t in self._intended.items()
                if t.status == "pending_margin"]

    def all_intended(self) -> dict[str, IntendedTrade]:
        return dict(self._intended)


class PositionManager:
    def __init__(self):
        self._trades:       dict[str, Trade]        = {}
        self._history:      dict[str, list[Trade]]  = {}
        self._sl_streaks:   dict[str, int]          = {}
        # symbol_state: {pair: {"last_pull": iso, "last_signal": iso, "last_signal_val": int}}
        self._symbol_state: dict[str, dict]         = {}
        self.cooldown       = CooldownManager()
        self._file_mtime:   float                   = 0.0
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            with _state_lock():
                if STATE_FILE.exists():
                    data = json.loads(STATE_FILE.read_text())
                    self._file_mtime   = STATE_FILE.stat().st_mtime
                    self._sl_streaks   = data.get("sl_streaks", {})
                    self._symbol_state = data.get("symbol_state", {})
                    known = {f.name for f in fields(Trade)}
                    for pair, td in data.get("open_trades", {}).items():
                        kw = {k: v for k, v in td.items() if k in known}
                        kw["pair"] = pair  # outer key is authoritative
                        self._trades[pair] = Trade(**kw)
        except Exception as e:
            log.warning(f"Could not load position state: {e}")

    @staticmethod
    def _apply_overrides(t: Trade) -> None:
        """Recompute effective tp/sl from (manual_* ?? watcher_*). Called after any field sync."""
        if t.manual_tp is not None:
            t.tp = t.manual_tp
        elif t.watcher_tp is not None:
            t.tp = t.watcher_tp
        if t.manual_sl is not None:
            t.sl = t.manual_sl
        elif t.watcher_sl is not None:
            t.sl = t.watcher_sl

    def _merge_from_disk_locked(self) -> None:
        """Unconditionally merge external state from disk into memory.

        Caller must already hold _state_lock(). No mtime gate: this repo has
        one process (watcher.watcher) that saves every ~17s via record_pull()
        for unrelated symbols, so its own bookmark is almost always "latest"
        from its own point of view — an mtime-gated merge would skip syncing
        external corrections from other processes/direct edits almost every
        time, since the narrow window where disk looks newer rarely lines up
        with this process's save cadence. Re-parsing a small JSON file on
        every save is cheap; silently ignoring peer state for hours isn't.
        """
        if not STATE_FILE.exists():
            return
        try:
            disk_data   = json.loads(STATE_FILE.read_text())
            disk_trades = disk_data.get("open_trades", {})
            self._file_mtime   = STATE_FILE.stat().st_mtime
            self._sl_streaks   = disk_data.get("sl_streaks", self._sl_streaks)
            self._symbol_state = disk_data.get("symbol_state", self._symbol_state)
            known = {f.name for f in fields(Trade)}

            # File is authoritative: sync all external-edit fields for
            # pairs already in memory — unless the disk record predates
            # the in-memory trade's open_time, which means the pair was
            # (re)opened after this disk snapshot was written (e.g. a
            # stale closed record from before open_trade() ran). Merging
            # a pre-reopen snapshot onto a freshly opened trade would
            # clobber its direction/entry/closed/etc. with old data.
            for pair, ext in disk_trades.items():
                if pair in self._trades:
                    t = self._trades[pair]
                    disk_open_time = ext.get("open_time")
                    if disk_open_time and t.open_time and disk_open_time < t.open_time:
                        continue
                    for fld in _EXTERNAL_EDIT_FIELDS:
                        if fld in ext:
                            setattr(t, fld, ext[fld])
                    self._apply_overrides(t)
                else:
                    # External add: pair appeared in file → load into memory.
                    try:
                        t = Trade(**{k: v for k, v in ext.items() if k in known})
                        self._apply_overrides(t)
                        self._trades[pair] = t
                        log.info(f"[{pair}] loaded from external file edit")
                    except Exception as e:
                        log.warning(f"[{pair}] could not load externally-added trade: {e}")

            # External delete: pair removed from file → remove from memory.
            # Skip trades opened very recently: this same process may have
            # just added one and called _save() before any other writer's
            # snapshot could possibly include it — that's not a deletion,
            # it's the disk catching up. Without this grace period, a fresh
            # open_trade() call racing another process's stale write would
            # wipe the brand-new trade seconds after it was recorded.
            now = datetime.now()
            for pair in list(self._trades.keys()):
                if pair not in disk_trades:
                    t = self._trades[pair]
                    try:
                        age = (now - datetime.fromisoformat(t.open_time)).total_seconds()
                    except (TypeError, ValueError):
                        age = _DELETE_GRACE_SECONDS  # malformed/missing open_time: don't protect it
                    if age < _DELETE_GRACE_SECONDS:
                        continue
                    log.info(f"[{pair}] removed from memory (deleted from file externally)")
                    self._trades.pop(pair)
        except Exception:
            pass

    def _write_to_disk_locked(self) -> None:
        """Serialize self._trades to disk. Caller must already hold _state_lock()."""
        STATE_FILE.parent.mkdir(exist_ok=True)
        open_trades = {pair: {k: v for k, v in t.__dict__.items()}
                       for pair, t in self._trades.items()}
        STATE_FILE.write_text(json.dumps(
            {
                "sl_streaks":   self._sl_streaks,
                "symbol_state": self._symbol_state,
                "open_trades":  open_trades,
            },
            indent=2
        ))
        self._file_mtime = STATE_FILE.stat().st_mtime

    def _save(self) -> None:
        """Sync from disk, then persist. Only safe for callers that do not
        mutate Trade fields in the same breath — see _atomic_update()."""
        try:
            with _state_lock():
                self._merge_from_disk_locked()
                self._write_to_disk_locked()
        except Exception as e:
            log.warning(f"Could not save position state: {e}")

    def _atomic_update(self, mutate_fn) -> None:
        """Merge from disk, apply mutate_fn(), then persist — all under one
        lock acquisition, so no peer write can land between the sync and the
        write and get silently clobbered (or clobber us). Use this from any
        method that mutates a Trade field and needs the change to stick.
        """
        try:
            with _state_lock():
                self._merge_from_disk_locked()
                mutate_fn()
                self._write_to_disk_locked()
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

    def find_by_ticker(self, ticker: str, hint_entry: Optional[float] = None) -> Optional[Trade]:
        """Resolve an open trade by bare ticker (no exchange prefix).

        TV *event* alerts (sub_tp/tp_hit/close) send just the ticker (e.g.
        "HYPEUSDC.P"), while *action* alerts (open_long/open_short) send the
        full tickerid ("HYPERLIQUID:HYPEUSDC.P") that open_trades is keyed
        by. Falls back to exact match first, then matches on the ticker
        suffix after the exchange prefix.

        A pair can now be open on two different tracked keys at once — e.g.
        "OANDA:EURUSD" (1h account) and "OANDA4H:EURUSD" (4h account) — and a
        sub_tp alert carries no timeframe field to disambiguate which one it
        means. When more than one key matches the bare ticker, `hint_entry`
        (the alert's own chart_entry_price) picks whichever candidate's
        recorded entry is closest — the two accounts' entries won't usually
        coincide, so this is a reliable enough tiebreaker in practice.
        """
        trade = self._trades.get(ticker)
        if trade:
            return trade
        ticker_u = ticker.upper()
        candidates = [t for key, t in self._trades.items()
                     if key.split(":", 1)[-1].upper() == ticker_u]
        if not candidates:
            return None
        if len(candidates) == 1 or hint_entry is None:
            return candidates[0]
        return min(candidates, key=lambda t: abs(t.entry - hint_entry))

    def open_trade(self, pair: str, direction: str, entry: float,
                   tp: Optional[float], sl: Optional[float], atr: float,
                   size: float, features: dict, bar_time=None,
                   notional: int = 0, opened_by: str = "unknown") -> Trade:
        trade = Trade(pair=pair, direction=direction, entry=entry,
                      tp=tp, sl=sl, atr=atr, size=size,
                      features=features, bar_time=bar_time,
                      watcher_tp=tp, watcher_sl=sl, notional=notional,
                      opened_by=opened_by)

        def _mutate():
            self._trades[pair] = trade
        self._atomic_update(_mutate)
        return trade

    def close_trade(self, pair: str, reason: str, close_price: float) -> None:
        closed_trade = None

        def _mutate():
            nonlocal closed_trade
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
            closed_trade = trade

        self._atomic_update(_mutate)
        if closed_trade:
            self._append_ledger(closed_trade)

    @staticmethod
    def _append_ledger(trade: Trade) -> None:
        """Durably record a closed trade to LEDGER_FILE (append-only JSONL).

        close_trade() previously left history in an in-memory dict only —
        never persisted, so it vanished on every process restart and gave
        no reliable answer to "what have we lost money on lately." This is
        the fix: one line per close, independent of position_state.json's
        own save/merge races, so a partial write here can't corrupt or be
        corrupted by that file's read-merge-write cycle.

        win_loss/pnl_pct are derived from `close_price` vs `entry`, which for
        crypto is often the *chart's* reported close, not the exact broker
        fill — treat these as directional estimates, not authoritative P&L.
        Use the broker's own fill history for precise realized P&L.
        """
        pnl_pct = None
        win_loss = None
        if trade.entry and trade.close_price:
            if trade.direction == "long":
                pnl_pct = (trade.close_price - trade.entry) / trade.entry * 100
            else:
                pnl_pct = (trade.entry - trade.close_price) / trade.entry * 100
            win_loss = "win" if pnl_pct > 0 else ("loss" if pnl_pct < 0 else "breakeven")

        entry = {
            "pair":          trade.pair,
            "direction":     trade.direction,
            "entry":         trade.entry,
            "close_price":   trade.close_price,
            "size":          trade.size,
            "notional":      trade.notional,
            "tp":            trade.tp,
            "sl":            trade.sl,
            "atr":           trade.atr,
            "open_time":     trade.open_time,
            "close_time":    datetime.now().isoformat(timespec="seconds"),
            "close_reason":  trade.close_reason,
            "pnl_pct":       round(pnl_pct, 4) if pnl_pct is not None else None,
            "win_loss":      win_loss,
            "opened_by":     trade.opened_by,
            "sl_tp_source":  trade.sl_tp_source,
        }
        try:
            with _state_lock():
                LEDGER_FILE.parent.mkdir(exist_ok=True)
                with open(LEDGER_FILE, "a") as fh:
                    fh.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.warning(f"[{trade.pair}] could not append trade ledger: {e}")

        _notify_close(trade, pnl_pct, win_loss)

    def move_sl(self, pair: str, new_sl: float, reason: str = "") -> None:
        def _mutate():
            trade = self._trades.get(pair)
            if trade:
                trade.sl           = new_sl
                trade.protected_sl = new_sl
                # watcher_sl must move too: _apply_overrides() re-derives sl
                # from watcher_sl on every merge (when manual_sl is unset),
                # so leaving it at the old value would silently revert this
                # move the next time any process syncs disk state.
                trade.watcher_sl   = new_sl
        self._atomic_update(_mutate)

    def close_software_watch(self, pair: str, closed_units: float,
                             reason: str, close_price: float) -> None:
        """Record a software-side TP/SL fill for `closed_units`. If that covers
        the whole trade, close it; otherwise shrink size and hand the remainder
        (already covered by a real broker order) back to broker-only tracking."""
        trade = self._trades.get(pair)
        if trade and closed_units >= trade.size:
            self.close_trade(pair, reason=reason, close_price=close_price)
            return

        def _mutate():
            trade = self._trades.get(pair)
            if not trade:
                return
            trade.size                 -= closed_units
            trade.software_watch_units  = 0
            trade.sl_tp_source          = "broker"
        self._atomic_update(_mutate)

    def fire_price_trigger(self, pair: str, idx: int) -> None:
        """Remove a price trigger by index after it has fired."""
        def _mutate():
            trade = self._trades.get(pair)
            if trade and 0 <= idx < len(trade.price_triggers):
                trade.price_triggers.pop(idx)
        self._atomic_update(_mutate)

    def update_runner_decision(self, pair: str, regime: str, confidence: float) -> None:
        def _mutate():
            trade = self._trades.get(pair)
            if trade:
                trade.runner_active = True
                trade.runner_regime = regime
        self._atomic_update(_mutate)

    # ── Exchange reconciliation ───────────────────────────────────────────────

    def reconcile(self) -> list[dict]:
        """
        Compare position_state.json against real exchange positions.
        Auto-closes state entries where broker has no matching position (drift fix).
        Call periodically (e.g. every 4h) to catch drift.
        """
        issues: list[dict] = []
        try:
            import os, urllib.request
            from trader.hyperliquid_trader import _hl_post
            from trader.oanda_trader import _ACCOUNTS as _OANDA_ACCOUNTS

            # ── OANDA reconciliation ──────────────────────────────────────────
            # Two accounts now (1h default + 4h split) — loop both, tagging
            # each with its own pair prefix, so a 4h position doesn't get
            # misread as "not on OANDA" just because it's checked against the
            # wrong account's positions.
            oanda_url = os.environ.get("OANDA_BASE_URL", "https://api-fxtrade.oanda.com/v3")
            _ACCOUNT_PREFIX = {"mix": "OANDA", "short": "OANDA_SHORT",
                              "mid": "OANDA_MID", "long": "OANDA_LONG"}
            oanda_live: dict[str, dict] = {}
            oanda_tpsl: dict[str, dict] = {}
            any_oanda_account = False

            for _acct_key, _creds in _OANDA_ACCOUNTS.items():
                oanda_key, oanda_acct = _creds["key"], _creds["account"]
                if not (oanda_key and oanda_acct):
                    continue
                any_oanda_account = True
                prefix = _ACCOUNT_PREFIX[_acct_key]
                try:
                    req = urllib.request.Request(
                        f"{oanda_url}/accounts/{oanda_acct}/openPositions",
                        headers={"Authorization": f"Bearer {oanda_key}"},
                    )
                    with urllib.request.urlopen(req, timeout=8) as r:
                        oanda_data = __import__("json").loads(r.read())
                    for p in oanda_data.get("positions", []):
                        instr = prefix + ":" + p["instrument"].replace("_", "")
                        lg, sh = p.get("long", {}), p.get("short", {})
                        if float(lg.get("units", 0)) > 0:
                            oanda_live[instr] = {"direction": "long",  "entry": float(lg["averagePrice"]), "units": float(lg["units"])}
                        elif float(sh.get("units", 0)) < 0:
                            oanda_live[instr] = {"direction": "short", "entry": float(sh["averagePrice"]), "units": float(sh["units"])}

                    # tp/sl per instrument, from each open trade's linked orders —
                    # a pair can have multiple trade tickets (FIFO-safeguard splits),
                    # so collect every tp/sl actually resting on the broker for it.
                    try:
                        treq = urllib.request.Request(
                            f"{oanda_url}/accounts/{oanda_acct}/openTrades",
                            headers={"Authorization": f"Bearer {oanda_key}"},
                        )
                        with urllib.request.urlopen(treq, timeout=8) as r:
                            trades_data = __import__("json").loads(r.read())
                        for t in trades_data.get("trades", []):
                            instr = prefix + ":" + t["instrument"].replace("_", "")
                            entry = oanda_tpsl.setdefault(instr, {"tp": set(), "sl": set()})
                            if t.get("takeProfitOrder"):
                                entry["tp"].add(float(t["takeProfitOrder"]["price"]))
                            if t.get("stopLossOrder"):
                                entry["sl"].add(float(t["stopLossOrder"]["price"]))
                    except Exception as e:
                        log.warning(f"[reconcile] OANDA[{_acct_key}] openTrades (tp/sl) check failed: {e}")
                except Exception as e:
                    log.warning(f"[reconcile] OANDA[{_acct_key}] openPositions check failed: {e}")

            if any_oanda_account:
                    for pair, trade in list(self._trades.items()):
                        if not pair.startswith(("OANDA:", "OANDA_SHORT:", "OANDA_MID:", "OANDA_LONG:")) or trade.closed:
                            continue
                        if pair not in oanda_live:
                            issues.append({"pair": pair, "issue": "in_state_not_on_oanda",
                                           "state_dir": trade.direction})
                            log.warning(f"[reconcile] {pair} — in state but NOT on OANDA; auto-closing")
                            self.close_trade(pair, reason="reconcile_not_on_broker", close_price=trade.entry)
                            continue

                        real = oanda_live[pair]
                        if trade.direction != real["direction"]:
                            issues.append({"pair": pair, "issue": "direction_mismatch",
                                           "state": trade.direction, "exchange": real["direction"]})
                            log.warning(f"[reconcile] {pair} direction mismatch state={trade.direction} oanda={real['direction']}; fixing")
                            trade.direction = real["direction"]
                            trade.entry     = real["entry"]
                            # tp/sl were computed for the OLD direction — stale and
                            # direction-inverted now, so don't carry them forward.
                            if trade.tp is not None or trade.sl is not None:
                                issues.append({"pair": pair, "issue": "tp_sl_cleared_on_direction_fix",
                                               "old_tp": trade.tp, "old_sl": trade.sl})
                                log.warning(f"[reconcile] {pair} clearing stale tp={trade.tp} sl={trade.sl}")
                            trade.tp = trade.sl = trade.watcher_tp = trade.watcher_sl = None
                            trade.manual_tp = trade.manual_sl = None
                            self._save()

                        # Report-only: which tp/sl is "right" when a pair has multiple
                        # trade tickets isn't always obvious, so flag rather than auto-fix.
                        broker_tpsl = oanda_tpsl.get(pair)
                        if broker_tpsl and (broker_tpsl["tp"] or broker_tpsl["sl"]):
                            if trade.tp is not None and broker_tpsl["tp"] and \
                               not any(abs(trade.tp - x) < 0.001 for x in broker_tpsl["tp"]):
                                issues.append({"pair": pair, "issue": "tp_mismatch",
                                               "state": trade.tp, "broker": sorted(broker_tpsl["tp"])})
                                log.warning(f"[reconcile] {pair} tp mismatch state={trade.tp} broker={sorted(broker_tpsl['tp'])}")
                            if trade.sl is not None and broker_tpsl["sl"] and \
                               not any(abs(trade.sl - x) < 0.001 for x in broker_tpsl["sl"]):
                                issues.append({"pair": pair, "issue": "sl_mismatch",
                                               "state": trade.sl, "broker": sorted(broker_tpsl["sl"])})
                                log.warning(f"[reconcile] {pair} sl mismatch state={trade.sl} broker={sorted(broker_tpsl['sl'])}")

            wallet = os.environ.get("HL_WALLET_ADDRESS", "")
            if not wallet:
                return issues

            # HL perp positions
            hl_state = _hl_post({"type": "clearinghouseState", "user": wallet})
            hl_pos   = {
                ap["position"]["coin"]: ap["position"]
                for ap in hl_state.get("assetPositions", [])
                if float(ap["position"]["szi"]) != 0
            }

            # XYZ DEX positions
            xyz_state = _hl_post({"type": "clearinghouseState", "user": wallet, "dex": "xyz"})
            xyz_pos   = {
                ap["position"]["coin"]: ap["position"]
                for ap in xyz_state.get("assetPositions", [])
                if float(ap["position"]["szi"]) != 0
            }

            # Longest/most-specific suffix first — "USD" alone would otherwise
            # match inside "USDT" and mangle tickers like BONKUSDT → BONKT.
            def _coin(pair: str) -> str:
                base = pair.split(":")[1]
                for sep in ("USDC.P", "USDT.P", "USDC", "USDT", "USD"):
                    if sep in base:
                        return base.split(sep)[0]
                return base

            # Only these exchanges route through Hyperliquid — everything else
            # (Alpaca stocks/indices/bonds via BATS/NYSE/NASDAQ/TVC, Oanda forex,
            # China A-shares) has no HL position to compare against, and checking
            # them here just produces false "not on exchange" alarms.
            _HL_ROUTED_PREFIXES = {"BINANCE", "BYBIT", "COINBASE", "KRAKEN",
                                    "BITMEX", "PIONEX", "BLOFIN", "HYPERLIQUID"}

            for pair, trade in list(self._trades.items()):
                if trade.closed:
                    continue
                prefix = pair.split(":")[0].upper()
                if prefix not in ("XYZ", "HIP3XYZ") and prefix not in _HL_ROUTED_PREFIXES:
                    continue  # not an HL-routed market — nothing to compare here
                if prefix in ("XYZ", "HIP3XYZ"):
                    coin   = "xyz:" + _coin(pair)
                    real   = xyz_pos.get(coin)
                else:
                    coin   = _coin(pair)
                    real   = hl_pos.get(coin)

                if real is None:
                    issues.append({"pair": pair, "issue": "in_state_not_on_exchange",
                                   "state_dir": trade.direction, "state_entry": trade.entry})
                    log.warning(f"[reconcile] {pair} — in state but NOT on exchange; auto-closing")
                    # No exact fill price available here without an extra userFills
                    # round-trip per symbol; trade.entry is the same fallback the
                    # OANDA branch above already uses (0% pnl estimate) — the point
                    # is to stop the position from silently rotting as "open" forever,
                    # not to get exact realized P&L (query broker fill history for that).
                    self.close_trade(pair, reason="reconcile_not_on_exchange", close_price=trade.entry)
                    continue

                real_szi   = float(real["szi"])
                real_entry = float(real["entryPx"])
                state_dir  = trade.direction
                real_dir   = "long" if real_szi > 0 else "short"

                fixed = False
                if state_dir != real_dir:
                    issues.append({"pair": pair, "issue": "direction_mismatch",
                                   "state": state_dir, "exchange": real_dir})
                    log.warning(f"[reconcile] {pair} direction mismatch: state={state_dir} exchange={real_dir}; fixing")
                    trade.direction = real_dir
                    # tp/sl were computed for the OLD direction — e.g. a tp
                    # below entry is correct for a short but backwards (and
                    # silently naked-looking) for a long. Clear rather than
                    # carry stale, direction-inverted levels forward.
                    if trade.tp is not None or trade.sl is not None:
                        issues.append({"pair": pair, "issue": "tp_sl_cleared_on_direction_fix",
                                       "old_tp": trade.tp, "old_sl": trade.sl})
                        log.warning(f"[reconcile] {pair} clearing stale tp={trade.tp} sl={trade.sl} "
                                    f"(computed for {state_dir}, now {real_dir})")
                    trade.tp = trade.sl = trade.watcher_tp = trade.watcher_sl = None
                    trade.manual_tp = trade.manual_sl = None
                    fixed = True

                if abs(real_entry - trade.entry) / max(trade.entry, 1e-9) > 0.001:
                    issues.append({"pair": pair, "issue": "entry_mismatch",
                                   "state": trade.entry, "exchange": real_entry})
                    log.warning(f"[reconcile] {pair} entry mismatch: state={trade.entry} exchange={real_entry}; fixing")
                    trade.entry = real_entry
                    fixed = True

                if fixed:
                    self._save()

            # Pairs on exchange but missing from state
            all_hl  = {_coin(p): p for p in self._trades
                       if p.split(":")[0].upper() in _HL_ROUTED_PREFIXES}
            all_xyz = {p.replace("XYZ:", "xyz:"): p for p in self._trades if "XYZ" in p}

            for coin, real in hl_pos.items():
                if coin not in all_hl:
                    issues.append({"pair": coin, "issue": "on_exchange_not_in_state",
                                   "exchange_szi": real["szi"], "exchange_entry": real["entryPx"]})
                    log.warning(f"[reconcile] {coin} — on HL exchange but NOT in state")

            for coin, real in xyz_pos.items():
                if coin not in all_xyz:
                    issues.append({"pair": coin, "issue": "on_exchange_not_in_state",
                                   "exchange_szi": real["szi"], "exchange_entry": real["entryPx"]})
                    log.warning(f"[reconcile] {coin} — on XYZ exchange but NOT in state")

        except Exception as e:
            log.warning(f"reconcile error: {e}")

        if not issues:
            log.info("[reconcile] state matches exchange ✓")
        return issues

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
