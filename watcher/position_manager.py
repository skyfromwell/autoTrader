#!/usr/bin/env python3
from __future__ import annotations
"""Trade state, history, and cooldown tracking per symbol."""

import json
import logging
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
    "sl_tp_source", "software_watch_units",
})

STATE_FILE    = Path("output/position_state.json")
INTENDED_FILE = Path("output/intended_positions.json")
log = logging.getLogger(__name__)


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
            bar_time=None) -> IntendedTrade:
        t = IntendedTrade(pair=pair, direction=direction,
                          signal_price=signal_price, tp=tp, sl=sl,
                          atr=atr, size=size, notional=notional,
                          features=features, bar_time=bar_time)
        self._intended[pair] = t
        self._save()
        log.info(f"[{pair}] 📋 Intended {direction.upper()} signal recorded "
                 f"signal_price={signal_price} notional=${notional:,}")
        return t

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

    def _save(self) -> None:
        try:
            STATE_FILE.parent.mkdir(exist_ok=True)

            if STATE_FILE.exists():
                try:
                    disk_mtime = STATE_FILE.stat().st_mtime
                    if disk_mtime > self._file_mtime:
                        disk_data   = json.loads(STATE_FILE.read_text())
                        disk_trades = disk_data.get("open_trades", {})
                        self._file_mtime = disk_mtime
                        known = {f.name for f in fields(Trade)}

                        # File is authoritative: sync all external-edit fields for
                        # pairs already in memory.
                        for pair, ext in disk_trades.items():
                            if pair in self._trades:
                                t = self._trades[pair]
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
                        for pair in list(self._trades.keys()):
                            if pair not in disk_trades:
                                log.info(f"[{pair}] removed from memory (deleted from file externally)")
                                self._trades.pop(pair)

                except Exception:
                    pass

            open_trades: dict = {}
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
            self._file_mtime = STATE_FILE.stat().st_mtime
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
                   tp: Optional[float], sl: Optional[float], atr: float,
                   size: float, features: dict, bar_time=None,
                   notional: int = 0) -> Trade:
        trade = Trade(pair=pair, direction=direction, entry=entry,
                      tp=tp, sl=sl, atr=atr, size=size,
                      features=features, bar_time=bar_time,
                      watcher_tp=tp, watcher_sl=sl, notional=notional)
        self._trades[pair] = trade
        self._save()
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

    def close_software_watch(self, pair: str, closed_units: float,
                             reason: str, close_price: float) -> None:
        """Record a software-side TP/SL fill for `closed_units`. If that covers
        the whole trade, close it; otherwise shrink size and hand the remainder
        (already covered by a real broker order) back to broker-only tracking."""
        trade = self._trades.get(pair)
        if not trade:
            return
        if closed_units >= trade.size:
            self.close_trade(pair, reason=reason, close_price=close_price)
            return
        trade.size                 -= closed_units
        trade.software_watch_units  = 0
        trade.sl_tp_source          = "broker"
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

            # ── OANDA reconciliation ──────────────────────────────────────────
            oanda_key  = os.environ.get("OANDA_API_KEY", "")
            oanda_acct = os.environ.get("OANDA_ACCOUNT_ID", "")
            oanda_url  = os.environ.get("OANDA_BASE_URL", "https://api-fxtrade.oanda.com/v3")
            if oanda_key and oanda_acct:
                try:
                    req = urllib.request.Request(
                        f"{oanda_url}/accounts/{oanda_acct}/openPositions",
                        headers={"Authorization": f"Bearer {oanda_key}"},
                    )
                    with urllib.request.urlopen(req, timeout=8) as r:
                        oanda_data = __import__("json").loads(r.read())
                    oanda_live: dict[str, dict] = {}
                    for p in oanda_data.get("positions", []):
                        instr = "OANDA:" + p["instrument"].replace("_", "")
                        lg, sh = p.get("long", {}), p.get("short", {})
                        if float(lg.get("units", 0)) > 0:
                            oanda_live[instr] = {"direction": "long",  "entry": float(lg["averagePrice"]), "units": float(lg["units"])}
                        elif float(sh.get("units", 0)) < 0:
                            oanda_live[instr] = {"direction": "short", "entry": float(sh["averagePrice"]), "units": float(sh["units"])}

                    for pair, trade in list(self._trades.items()):
                        if not pair.startswith("OANDA:") or trade.closed:
                            continue
                        if pair not in oanda_live:
                            issues.append({"pair": pair, "issue": "in_state_not_on_oanda",
                                           "state_dir": trade.direction})
                            log.warning(f"[reconcile] {pair} — in state but NOT on OANDA; auto-closing")
                            self.close_trade(pair, reason="reconcile_not_on_broker", close_price=trade.entry)
                        else:
                            real = oanda_live[pair]
                            if trade.direction != real["direction"]:
                                issues.append({"pair": pair, "issue": "direction_mismatch",
                                               "state": trade.direction, "exchange": real["direction"]})
                                log.warning(f"[reconcile] {pair} direction mismatch state={trade.direction} oanda={real['direction']}; fixing")
                                trade.direction = real["direction"]
                                trade.entry     = real["entry"]
                                self._save()
                except Exception as e:
                    log.warning(f"[reconcile] OANDA check failed: {e}")

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

            for pair, trade in self._trades.items():
                prefix = pair.split(":")[0].upper()
                if prefix != "XYZ" and prefix not in _HL_ROUTED_PREFIXES:
                    continue  # not an HL-routed market — nothing to compare here
                if prefix == "XYZ":
                    coin   = pair.replace("XYZ:", "xyz:")
                    real   = xyz_pos.get(coin)
                else:
                    coin   = _coin(pair)
                    real   = hl_pos.get(coin)

                if real is None:
                    issues.append({"pair": pair, "issue": "in_state_not_on_exchange",
                                   "state_dir": trade.direction, "state_entry": trade.entry})
                    log.warning(f"[reconcile] {pair} — in state but NOT on exchange")
                    continue

                real_szi   = float(real["szi"])
                real_entry = float(real["entryPx"])
                state_dir  = trade.direction
                real_dir   = "long" if real_szi > 0 else "short"

                if state_dir != real_dir:
                    issues.append({"pair": pair, "issue": "direction_mismatch",
                                   "state": state_dir, "exchange": real_dir})
                    log.warning(f"[reconcile] {pair} direction mismatch: state={state_dir} exchange={real_dir}")

                if abs(real_entry - trade.entry) / max(trade.entry, 1e-9) > 0.001:
                    issues.append({"pair": pair, "issue": "entry_mismatch",
                                   "state": trade.entry, "exchange": real_entry})
                    log.warning(f"[reconcile] {pair} entry mismatch: state={trade.entry} exchange={real_entry}")

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
