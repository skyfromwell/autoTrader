#!/usr/bin/env python3
from __future__ import annotations
"""
MCP Processor — adapted from Jingda AI Trading Agent v2.
Processes Jingda indicator plot values and manages trade lifecycle:
  - 5-gate entry filter (cooldown, whipsaw, revenge trade, rules, sizing)
  - Trade protection (SL move to BE on danger candle)
  - Exit handling (TP partial close, SL, runner exit, signal flip)
  - Post-TP regime classification → runner management
Broker execution wired to Alpaca via trader.execute_trade().
"""

import csv
import glob
import logging
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from watcher.regime_classifier import RegimeClassifier
from watcher.position_manager  import PositionManager, IntendedPositionManager
from trader.trader             import execute_trade as alpaca_execute, close_alpaca_position, move_alpaca_sl
from trader.oanda_trader       import execute_trade as oanda_execute
from trader.oanda_trader       import account_for as oanda_account_for, margin_status as oanda_margin_status
import trader.oanda_trader     as _oanda_trader
from trader.hyperliquid_trader import execute_trade as hl_execute
from trader.hyperliquid_trader import close_position as hl_close, partial_close_position as hl_partial_close, _hl_coin, _hl_post
from trader.xyz_trader         import move_sl as xyz_move_sl
from trader.xyz_trader         import execute_trade as xyz_execute
from trader.xyz_trader         import close_position as xyz_close
from trader.xyz_trader         import tv_to_xyz

log          = logging.getLogger(__name__)
classifier   = RegimeClassifier()
manager      = PositionManager()
intended_mgr = IntendedPositionManager()

_NOTIONAL_BY_GRADE = {"A": 20_000, "B": 10_000}
_NOTIONAL_DEFAULT  = 10_000
_MIN_MARGIN_USD    = float(os.getenv("MIN_MARGIN_USD", "500"))


def _grade_for(tv_symbol: str) -> str:
    """Look up the most recent screener grade for a tv_symbol (e.g. 'NYSE:CAT')."""
    output = Path(__file__).parent.parent / "output"
    csvs = sorted(glob.glob(str(output / "results_*.csv")), reverse=True)
    for path in csvs:
        try:
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("tv_symbol") == tv_symbol:
                        return row.get("grade", "B")
        except Exception:
            continue
    return "B"


def _notional_for(tv_symbol: str, size: float) -> int:
    grade    = _grade_for(tv_symbol)
    base     = _NOTIONAL_BY_GRADE.get(grade, _NOTIONAL_DEFAULT)
    notional = int(base * size)
    log.info(f"[{tv_symbol}] grade={grade}  base=${base:,}  size={size}  notional=${notional:,}")
    return notional

# ─── Margin helpers ──────────────────────────────────────────────────────────

def _margin_needed(pair: str, notional: int) -> float:
    """Actual margin locked = notional / leverage for HL; notional for others."""
    prefix = pair.split(":")[0].upper() if ":" in pair else ""
    if prefix in _CRYPTO_PREFIXES and not tv_to_xyz(pair):
        try:
            from trader.hyperliquid_trader import _get_leverage
            coin = pair.split(":", 1)[1].replace("USDC.P", "")
            lev  = _get_leverage(coin) or 1
            return notional / lev
        except Exception:
            pass
    return float(notional)


_HL_USAGE_WARN       = 0.60   # Margin Usage % warn threshold
_HL_USAGE_BLOCK      = 0.80   # Margin Usage % block threshold
_HL_FROM_LIQ_WARN    = 0.50   # From Liquidation % warn threshold
_HL_FROM_LIQ_BLOCK   = 0.30   # From Liquidation % block threshold

def _hl_margin_stats(dex: str | None = None) -> dict:
    """
    Return HL margin metrics matching the dashboard's top-level Account Value /
    Withdrawable numbers, not just the perps clearinghouseState sub-view.

    clearinghouseState's own "accountValue" only reflects margin actively
    deployed to positions (~ totalMarginUsed) — it does NOT include the
    account's undeployed USDC, which is still real, immediately-usable perps
    collateral (confirmed against the dashboard's "Withdrawable now" figure,
    2026-07-15: API said accountValue=$2,316/marginUsed=$2,283 i.e. ~100%
    "used", while the dashboard showed Account Value $8,868 / Withdrawable
    $6,587 i.e. ~25.7% used — the gap is exactly the undeployed USDC this
    function was missing). So total capital = perps accountValue + undeployed
    USDC (spot total minus what's already held/locked).

      margin_usage = totalMarginUsed / total_capital
      from_liq     = (total_capital - crossMaintenanceMarginUsed) / total_capital
      free_margin  = max(0, total_capital - totalMarginUsed)
    """
    wallet = os.getenv("HL_WALLET_ADDRESS", "")
    payload = {"type": "clearinghouseState", "user": wallet}
    if dex:
        payload["dex"] = dex
    state  = _hl_post(payload)
    ms     = state.get("marginSummary", {})
    perps_acv = float(ms.get("accountValue", 0))
    mu        = float(ms.get("totalMarginUsed", 0))
    maint     = float(state.get("crossMaintenanceMarginUsed", 0))

    spot_free = 0.0
    try:
        spot = _hl_post({"type": "spotClearinghouseState", "user": wallet})
        for b in spot.get("balances", []):
            if b.get("coin") == "USDC":
                spot_free = float(b.get("total", 0)) - float(b.get("hold", 0))
                break
    except Exception as e:
        log.warning(f"[HL] spot balance fetch failed, margin check will undercount capital: {e}")

    acv = perps_acv + spot_free
    free = max(acv - mu, 0.0)
    usage = mu / acv if acv else 1.0
    from_liq = (acv - maint) / acv if acv else 0.0
    return {"acv": acv, "margin_used": mu, "maint_used": maint,
            "free": free, "margin_usage": usage, "from_liq": from_liq}


def _oanda_available_margin(pair: str = "OANDA:") -> float:
    """Return 0 if this pair's OANDA account is at/above the block wall
    (45%), else marginAvailable for that specific account. Each of the 4
    split accounts gates its own new entries off its own margin usage —
    see trader.oanda_trader.margin_status()."""
    account = oanda_account_for(pair)
    status = oanda_margin_status(account)
    pct = status["pct"]
    if pct is None:
        return 0.0
    log.info(f"[OANDA:{account}] marginCloseoutPercent={pct:.3f}  marginAvailable=${status['available']:,.0f}")
    if status["block"]:
        log.warning(f"[OANDA:{account}] 🚨 Margin closeout at {pct*100:.1f}% >= "
                    f"{_oanda_trader._MARGIN_BLOCK_PCT*100:.0f}% wall — blocking new forex entries")
        return 0.0
    if status["warn"]:
        log.warning(f"[OANDA:{account}] ⚠️ Margin closeout at {pct*100:.1f}% >= "
                    f"{_oanda_trader._MARGIN_WARN_PCT*100:.0f}% — proceeding, but getting close")
    return status["available"]


def _available_margin(pair: str) -> float:
    """Return available margin in USD, or 0 if any safety cap is breached."""
    prefix = pair.split(":")[0].upper() if ":" in pair else ""
    try:
        if tv_to_xyz(pair):
            stats = _hl_margin_stats(dex="xyz")
        elif prefix in _CRYPTO_PREFIXES:
            stats = _hl_margin_stats()
        elif prefix in _FOREX_PREFIXES:
            return _oanda_available_margin(pair)
        else:
            return float("inf")  # Alpaca paper: no margin gate

        usage    = stats["margin_usage"]
        from_liq = stats["from_liq"]
        free     = stats["free"]
        log.info(f"[{pair}] HL margin: usage={usage*100:.1f}%  from_liq={from_liq*100:.1f}%  free=${free:,.0f}")
        if usage >= _HL_USAGE_BLOCK or from_liq <= _HL_FROM_LIQ_BLOCK:
            log.warning(f"[{pair}] 🚨 HL margin critical — usage={usage*100:.1f}% from_liq={from_liq*100:.1f}% — blocking new entries")
            return 0.0
        return free
    except Exception as e:
        log.warning(f"[{pair}] margin check error: {e} — allowing trade")
        return float("inf")


def retry_pending_margin() -> None:
    """After a position closes, attempt to open any pending_margin signals that now fit."""
    pending = intended_mgr.get_pending_margin()
    if not pending:
        return
    min_margin = float(os.getenv("MIN_MARGIN_USD", _MIN_MARGIN_USD))
    for pair, intended in pending:
        notional = intended.notional or _notional_for(pair, intended.size)
        avail    = _available_margin(pair)
        needed   = _margin_needed(pair, notional)
        if avail - needed >= min_margin:
            log.info(f"[{pair}] 💰 MARGIN AVAILABLE — promoting pending "
                     f"{intended.direction.upper()} (notional=${notional:,} margin=${needed:,.0f} avail=${avail:.0f})")
            ok = _broker_execute(pair, intended.direction, price=None, notional=notional,
                                tp=intended.tp, sl=intended.sl)
            if not ok:
                log.error(f"[{pair}] ❌ retry_pending_margin broker execute failed — leaving pending")
                continue
            manager.open_trade(pair=pair, direction=intended.direction,
                               entry=intended.signal_price,
                               tp=intended.tp, sl=intended.sl, atr=intended.atr,
                               size=intended.size, features=intended.features,
                               bar_time=intended.bar_time, notional=notional,
                               opened_by="watcher")
            intended_mgr.remove(pair)
        else:
            log.info(f"[{pair}] ⏳ Still pending margin — avail=${avail:.0f} "
                     f"need=${needed:,.0f} + ${min_margin:.0f} reserve")


# Exchange prefix → broker / asset-class routing
_CRYPTO_PREFIXES  = {"HYPERLIQUID", "BINANCE", "BYBIT", "COINBASE", "KRAKEN", "BITMEX", "BITSTAMP", "PIONEX", "BLOFIN"}
_FOREX_RAW_PREFIXES = {"FX", "OANDA", "FXCM", "FOREXCOM", "PEPPERSTONE"}
_FOREX_PREFIXES   = _FOREX_RAW_PREFIXES | {"OANDA_SHORT", "OANDA_MID", "OANDA_LONG"}
_CHINESE_PREFIXES = {"SSE", "SZSE", "HKEX", "SHSE"}   # no broker yet

def _broker_execute(pair: str, direction: str, price: float | None = None,
                    notional: int = _NOTIONAL_DEFAULT, tp: float | None = None,
                    sl: float | None = None) -> bool:
    """Route trade execution to the correct broker based on exchange prefix.

    Returns whether the broker confirms the order succeeded — callers must
    check this before recording the trade in position_state.json, otherwise
    a rejected/failed order still gets tracked as if it were live (this is
    exactly how HYPERLIQUID:XRPUSDC.P etc. ended up as phantom positions:
    the return value was previously discarded entirely).
    """
    prefix = pair.split(":")[0].upper() if ":" in pair else ""
    if tv_to_xyz(pair):
        res = xyz_execute(pair, direction, price=price, notional=notional)
        return not (isinstance(res, dict) and res.get("skipped"))
    if prefix in _CHINESE_PREFIXES:
        # No shorting A-shares, and a long here isn't a live fill yet — it
        # submits to the QMT mailbox (watcher/china_queue.py -> Windows box
        # over HTTP) for the pasted QMT strategy to pick up. Always return
        # False so no caller records this as an already-open position_state
        # trade; the handle_entry() call site queues explicitly before ever
        # reaching here, this is just a backstop for the other
        # _broker_execute call sites (margin-deferred entries, flips,
        # runner exits).
        if direction == "short":
            log.warning(f"[{pair}] ❌ BLOCKED — cannot short A-shares")
            return False
        from watcher.china_queue import queue_order as _china_queue_order
        sent = _china_queue_order(pair, price or 0, "240", notional, type_="watcher_pull", tp=tp, sl=sl)
        if sent is None:
            log.error(f"[{pair}] ❌ mailbox submit failed — not queued")
        else:
            log.info(f"[{pair}] Queued LONG (China, next-session open)  id={sent.get('id')}")
        return False
    if prefix in _CRYPTO_PREFIXES:
        res = hl_execute(pair, direction, price=price, notional=notional, tp=tp, sl=sl)
        return bool(res.get("success"))
    elif prefix in _FOREX_PREFIXES:
        res = oanda_execute(pair, direction, price=price, notional=notional, tp=tp, sl=sl)
        return bool(res.get("success"))
    else:
        return bool(alpaca_execute(pair, direction, price=price, notional=notional, tp=tp, sl=sl))

# ─── Signal / Exit Maps ───────────────────────────────────────────────────────

SIGNAL_MAP = { 1: "long_entry", -1: "short_entry" }
EXIT_MAP   = {
     2: "long_runner_exit",
    -2: "short_runner_exit",
     3: "tp_hit_long",
    -3: "tp_hit_short",
     4: "sl_hit_long",
    -4: "sl_hit_short",
}

# Entry confirmation thresholds — tightened after consecutive SL hits.
# F4 ADX is Lorentzian-normalized to 0-1 (ml.n_adx), not raw 0-100.
# ATR Ratio is a raw ratio (current ATR / avg ATR), not normalized.
ENTRY_RULES = {
    "normal":   {"min_prediction": 2, "min_adx": 0.20, "min_atr_ratio": 0.80},
    "cautious": {"min_prediction": 4, "min_adx": 0.30, "min_atr_ratio": 0.90},
    "strict":   {"min_prediction": 6, "min_adx": 0.40, "min_atr_ratio": 1.00},
}


# ─── Main Entry Point ─────────────────────────────────────────────────────────

def process_mcp_data(raw: dict) -> None:
    """
    Called at each 4H candle close for one symbol.
    raw: dict of Jingda plot name → value (from TradingView data window).
    """
    pair = raw.get("pair", "UNKNOWN")
    from watcher.china_queue import normalize_china_prefix as _normalize_china_prefix
    pair = _normalize_china_prefix(pair)
    # This whole pull loop reads the chart on a fixed 4h timeframe
    # (_read_indicator_data hardcodes "240" — see watcher.py), so any OANDA
    # pair coming through here always routes to the mid (4h) account. Accounts
    # are independent capital pools, not shared tracking of "the same trade" —
    # a mix-account position on this pair has no bearing on whether mid
    # should open its own. This pull path is a separate route into
    # position_manager from the TradingView webhook flow in tv_alert_server.py
    # (which has its own timeframe-aware rewrite mirroring this one).
    _prefix = pair.split(":", 1)[0].upper() if ":" in pair else ""
    if _prefix in _FOREX_RAW_PREFIXES:
        pair = "OANDA_MID:" + pair.split(":", 1)[-1]
    features   = extract_features(raw)
    signal_val = _safe_int(raw.get("Signal Stream", 0))
    exit_val   = _safe_int(raw.get("Exit Stream",   0))

    bars_missed = manager.record_pull(pair, signal_val)
    if bars_missed > 0:
        log.warning(f"[{pair}] ⚠️  MISSED {bars_missed} bar(s) since last pull — "
                    f"signal carry-forward covers 1 bar only")

    log.info(f"[{pair}] signal={signal_val}  exit={exit_val}  "
             f"pred={raw.get('Prediction', 'n/a')}  "
             f"atr={raw.get('ATR', 'n/a')}")

    # Order: protection → price triggers → exits → entries
    check_trade_protection(pair, raw, features)
    mark = _safe_float(raw.get("close"))
    if mark:
        check_price_triggers(pair, mark)

    if exit_val != 0:
        event = EXIT_MAP.get(exit_val)
        if event:
            handle_exit(pair, event, features, raw)

    if signal_val != 0:
        event = SIGNAL_MAP.get(signal_val)
        if event:
            handle_entry(pair, event, features, raw)


# ─── Trade Protection ─────────────────────────────────────────────────────────

def check_trade_protection(pair: str, raw: dict, features: dict) -> None:
    trade = manager.get_trade(pair)
    if not trade or trade.closed or trade.protected_sl is not None:
        return

    protect_key = "Protect Short" if trade.direction == "short" else "Protect Long"
    if _safe_int(raw.get(protect_key, 0)) != 1:
        return

    tp_progress = _safe_float(raw.get("TP Progress", 0))
    atr         = _safe_float(raw.get("ATR", trade.atr))

    if   tp_progress >= 0.80: level = "high"
    elif tp_progress >= 0.65: level = "medium"
    else: return

    new_sl = _calc_protective_sl(trade.direction, trade.entry, atr, level)
    log.info(f"[{pair}] 🛡️ PROTECTION  progress={tp_progress:.0%}  "
             f"level={level}  sl {trade.sl:.5f} → {new_sl:.5f}")
    manager.move_sl(pair, new_sl, reason=f"protection_{level}")
    # Note: stock SL moves are tracked in state only (Alpaca doesn't support
    # modifying orders on paper account easily — add OCO when going live)


# ─── Price-level Trigger Checker ─────────────────────────────────────────────

_XYZ_PREFIX = "XYZ:"

def _broker_move_sl(pair: str, new_sl: float) -> None:
    """Push the new SL to the actual broker order so it survives watcher downtime."""
    trade = manager.get_trade(pair)
    if not trade:
        return
    try:
        prefix = pair.split(":")[0].upper() if ":" in pair else ""
        if prefix not in _CRYPTO_PREFIXES:
            # Alpaca paper stocks
            move_alpaca_sl(pair, new_sl, trade.direction)
            return
        if tv_to_xyz(pair):
            xyz_coin = tv_to_xyz(pair)   # returns "xyz:BRENTOIL" etc.
            xyz_move_sl(xyz_coin, new_sl, trade.direction)
        else:
            # Standard HL perp: use JS-based cancel+replace
            import subprocess, os, json
            js = str(Path(__file__).parent.parent.parent /
                     "tradingview-mcp" / "scripts" / "move_sl.mjs")
            result = subprocess.run(
                [os.environ.get("NODE_BIN", "node"), "--input-type=module",
                 "--eval",
                 f'import("{js}").then(m => m.moveSl("{_hl_coin(pair)}", {new_sl}, "{"long" if trade.direction=="long" else "short"}"))'],
                capture_output=True, text=True, timeout=20,
            )
            log.info(f"[{pair}] broker SL move result: {result.stdout.strip()}")
    except Exception as e:
        log.error(f"[{pair}] _broker_move_sl failed: {e}")


def check_price_triggers(pair: str, mark_price: float) -> None:
    """
    Fire any pending price-level triggers for this pair.
    Trigger format (stored in trade.price_triggers):
      {"condition": "price_lte"|"price_gte", "price": float,
       "action": "move_sl", "value": float, "note": str}
    """
    trade = manager.get_trade(pair)
    if not trade or trade.closed or not trade.price_triggers:
        return

    fired = []
    for i, trig in enumerate(trade.price_triggers):
        cond  = trig.get("condition")
        level = float(trig.get("price", 0))
        met   = (cond == "price_lte" and mark_price <= level) or \
                (cond == "price_gte" and mark_price >= level)
        if not met:
            continue

        action   = trig.get("action")
        value    = float(trig.get("value", 0))
        notional = int(trig.get("notional", _NOTIONAL_DEFAULT))
        note     = trig.get("note", "")
        if action == "move_sl":
            old_sl = trade.sl
            manager.move_sl(pair, value, reason=f"price_trigger({cond}@{level})")
            log.info(f"[{pair}] 🎯 PRICE TRIGGER fired — {cond}@{level}  "
                     f"sl {old_sl:.5f} → {value:.5f}  {note}")
            _broker_move_sl(pair, value)
        elif action == "flip_long":
            log.info(f"[{pair}] 🔄 PRICE TRIGGER flip_long — {cond}@{level}  {note}")
            _close_broker_position(pair)
            old_tp, old_atr = trade.tp, trade.atr
            manager.close_trade(pair, reason=f"price_trigger_flip@{level}",
                                close_price=mark_price)
            _broker_execute(pair, "long", price=mark_price, notional=notional)
            manager.open_trade(pair=pair, direction="long", entry=mark_price,
                               tp=old_tp, sl=None, atr=old_atr, size=notional, features={},
                               opened_by="watcher")
            log.info(f"[{pair}] ✅ Flipped to LONG at {mark_price} notional=${notional:,}")
        elif action == "flip_short":
            log.info(f"[{pair}] 🔄 PRICE TRIGGER flip_short — {cond}@{level}  {note}")
            _close_broker_position(pair)
            old_tp, old_atr = trade.tp, trade.atr
            manager.close_trade(pair, reason=f"price_trigger_flip@{level}",
                                close_price=mark_price)
            _broker_execute(pair, "short", price=mark_price, notional=notional)
            manager.open_trade(pair=pair, direction="short", entry=mark_price,
                               tp=old_tp, sl=None, atr=old_atr, size=notional, features={},
                               opened_by="watcher")
            log.info(f"[{pair}] ✅ Flipped to SHORT at {mark_price} notional=${notional:,}")
        elif action == "partial_close":
            fraction = float(trig.get("fraction", 0.5))
            prefix = pair.split(":")[0].upper()
            if prefix in {"BYBIT", "HYPERLIQUID"}:
                hl_partial_close(pair, fraction=fraction)
            else:
                log.warning(f"[{pair}] partial_close trigger not supported for {prefix}")
            log.info(f"[{pair}] 🎯 PRICE TRIGGER partial_close {fraction*100:.0f}% — {cond}@{level}  {note}")
        fired.append(i)

    for i in reversed(fired):
        manager.fire_price_trigger(pair, i)


# ─── Flip Detection / Close ───────────────────────────────────────────────────

def _broker_position_dir(pair: str) -> str | None:
    """Return 'long', 'short', or None if no position (or non-crypto / error)."""
    prefix = pair.split(":")[0].upper() if ":" in pair else ""
    if prefix not in _CRYPTO_PREFIXES:
        return None
    try:
        import os as _os
        coin = _hl_coin(pair)
        d    = _hl_post({"type": "clearinghouseState", "user": _os.getenv("HL_WALLET_ADDRESS", "")})
        for ap in d.get("assetPositions", []):
            p   = ap["position"]
            szi = float(p["szi"])
            if p["coin"] == coin and szi != 0:
                existing_dir = "long" if szi > 0 else "short"
                log.info(f"[{pair}] broker position: {existing_dir.upper()} szi={szi}")
                return existing_dir
        log.info(f"[{pair}] broker position: none")
    except Exception as e:
        log.warning(f"[{pair}] broker position check failed: {e}")
    return None


def _broker_has_opposite(pair: str, direction: str) -> bool:
    """Return True if the broker holds a position opposite to `direction`."""
    broker_dir = _broker_position_dir(pair)
    return broker_dir is not None and broker_dir != direction


def _close_broker_position(pair: str) -> None:
    """Close any open broker position for this pair (used on signal flip or exit)."""
    if tv_to_xyz(pair):
        xyz_close(pair)
        return
    prefix = pair.split(":")[0].upper() if ":" in pair else ""
    if prefix in _CRYPTO_PREFIXES:
        hl_close(pair)
    elif prefix in _CHINESE_PREFIXES:
        from watcher.china_queue import close_order as _china_close_order
        _china_close_order(pair, reason="watcher_exit")
    elif prefix in _FOREX_PREFIXES:
        log.info(f"[{pair}] Forex close delegated to broker SL/TP order")
    else:
        # Alpaca paper stocks — cancel bracket orders then close
        close_alpaca_position(pair)


# ─── Entry Handler ────────────────────────────────────────────────────────────

def handle_entry(pair: str, event: str, features: dict, raw: dict) -> None:
    direction = "long" if event == "long_entry" else "short"
    sl_streak = manager.get_sl_streak(pair)
    history   = manager.get_history(pair, n=4)
    is_flip   = False

    existing = manager.get_trade(pair)
    if existing and not existing.closed:
        if existing.direction == direction:
            # State says same direction — but verify broker isn't actually opposite
            # (can happen if prior session's position was manually reversed or SL-hit externally)
            broker_dir = _broker_position_dir(pair)
            if broker_dir is not None and broker_dir != direction:
                log.info(f"[{pair}] 🔄 FLIP (state/broker mismatch) — state={existing.direction.upper()} "
                         f"broker={broker_dir.upper()} → entering {direction.upper()}")
                _close_broker_position(pair)
                manager.close_trade(pair, reason="signal_flip",
                                    close_price=_safe_float(raw.get("close")))
                is_flip = True
            else:
                log.warning(f"[{pair}] Signal ignored — {direction} already open")
                return
        else:
            # Managed flip: close existing first, then open new
            log.info(f"[{pair}] 🔄 FLIP — closing {existing.direction.upper()} → entering {direction.upper()}")
            _close_broker_position(pair)
            manager.close_trade(pair, reason="signal_flip",
                                close_price=_safe_float(raw.get("close")))
            is_flip = True
    elif _broker_has_opposite(pair, direction):
        # Broker has an untracked opposite position (e.g. opened by a prior session)
        log.info(f"[{pair}] 🔄 FLIP (broker-detected) — closing stale opposite → {direction.upper()}")
        _close_broker_position(pair)
        manager.close_trade(pair, reason="broker_detected_flip",
                            close_price=_safe_float(raw.get("close")))
        is_flip = True

    # Compute whipsaw regardless — used for sizing even on flips
    whipsaw = _whipsaw_score(pair, raw, history, sl_streak)

    if not is_flip:
        # Gate 1: Cooldown
        if manager.cooldown.is_cooling_down(pair):
            mins = manager.cooldown.remaining_minutes(pair)
            log.info(f"[{pair}] ❌ BLOCKED — cooldown ({mins}m remaining)")
            return

        # Gate 2: Whipsaw block
        if whipsaw["action"] == "block":
            log.warning(f"[{pair}] ❌ BLOCKED — whipsaw score={whipsaw['score']} "
                        f"{whipsaw['reasons']}")
            return

        # Gate 3: Revenge trade filter
        if _is_revenge_trade(pair, direction, history):
            log.warning(f"[{pair}] ❌ BLOCKED — revenge trade pattern")
            return

        # Gate 4: Entry confirmation rules
        rule_level = ("strict"   if sl_streak >= 2 else
                      "cautious" if sl_streak == 1 else "normal")
        if not _passes_entry_rules(raw, ENTRY_RULES[rule_level]):
            log.info(f"[{pair}] ❌ BLOCKED — failed {rule_level} entry rules")
            return
    else:
        rule_level = "normal"
        if whipsaw["action"] == "block":
            log.info(f"[{pair}] ⚠️ Flip bypasses whipsaw block (score={whipsaw['score']}) — reducing size")

    # Gate 5: Position sizing
    size = 0.5 if (whipsaw["action"] in ("block", "reduce") or sl_streak >= 1) else 1.0
    if size < 1.0:
        log.info(f"[{pair}] ⚠️ Reduced size to 50% (streak={sl_streak}, whipsaw={whipsaw['action']})")

    entry = _safe_float(raw.get("close"))
    atr   = _safe_float(raw.get("ATR"))
    pred  = _safe_float(raw.get("Prediction", 0))

    # Always use Jingda's pre-calculated levels from the chart.
    # Pine Script auto-detects multipliers via syminfo.type, so chart and
    # broker orders stay identical across stocks, forex, and crypto.
    tp = _safe_float(raw.get("TP Level")) or None
    sl = _safe_float(raw.get("SL Level")) or None

    notional = _notional_for(pair, size)

    # Record signal intent before any broker interaction. This whole pull
    # loop reads the Jingda study on a fixed 4h chart (_read_indicator_data
    # hardcodes timeframe "240"), so every intended signal from this path is
    # 4h-bar — see evict_stale() for how that governs staleness.
    intended_mgr.add(pair=pair, direction=direction, signal_price=entry,
                     tp=tp, sl=sl, atr=atr, size=size, notional=notional,
                     features=features, bar_time=raw.get("bar_time"),
                     timeframe="240")

    # Gate 6: Margin check (use actual margin locked = notional / leverage)
    avail  = _available_margin(pair)
    needed = _margin_needed(pair, notional)
    if avail - needed < _MIN_MARGIN_USD:
        log.warning(f"[{pair}] ⚠️ MARGIN INSUFFICIENT — "
                    f"need ${needed:,.0f} + ${_MIN_MARGIN_USD:.0f} reserve, have ${avail:.0f}")
        intended_mgr.mark_pending_margin(pair, margin_required=notional)
        return

    log.info(f"[{pair}] ✅ NEW {direction.upper()} | "
             f"entry={entry:.5f} tp={tp} sl={sl} "
             f"atr={atr:.5f} pred={pred:.0f} size={size} notional=${notional:,} rules={rule_level}")

    # China A-shares: no shorting, and a long entry submits to the QMT
    # mailbox (watcher/china_queue.py -> Windows box over HTTP) rather than
    # executing immediately — there's no live fill yet at this point, so
    # don't call manager.open_trade() here.
    if pair.split(":")[0].upper() in _CHINESE_PREFIXES:
        if direction == "short":
            log.warning(f"[{pair}] ❌ BLOCKED — cannot short A-shares")
            intended_mgr.remove(pair)
            return
        from watcher.china_queue import queue_order as _china_queue_order
        sent = _china_queue_order(pair, entry or 0, "240", notional,
                                  type_="watcher_pull", reason=f"pred={pred:.0f}",
                                  tp=tp, sl=sl)
        if sent is None:
            log.error(f"[{pair}] ❌ mailbox submit failed — leaving intended signal queued to retry")
            return
        log.info(f"[{pair}] ✅ Queued LONG (China, next-session open)  entry≈{entry}  id={sent.get('id')}")
        intended_mgr.remove(pair)
        return

    # Execute on broker first; only write to position_state on success.
    ok = _broker_execute(pair, direction, price=entry if entry else None, notional=notional, tp=tp, sl=sl)
    if not ok:
        log.error(f"[{pair}] ❌ broker execute failed — not recording position_state")
        return
    manager.open_trade(pair=pair, direction=direction, entry=entry,
                       tp=tp, sl=sl, atr=atr, size=size,
                       features=features, bar_time=raw.get("bar_time"),
                       notional=notional, opened_by="watcher")
    intended_mgr.remove(pair)


# ─── Exit Handler ─────────────────────────────────────────────────────────────

def handle_exit(pair: str, event: str, features: dict, raw: dict) -> None:
    trade = manager.get_trade(pair)
    if not trade or trade.closed:
        log.warning(f"[{pair}] Exit {event} — no open trade, ignoring")
        return

    if event in ("tp_hit_long", "tp_hit_short"):
        direction = "long" if event == "tp_hit_long" else "short"
        tp_price  = _safe_float(raw.get("TP Level"))
        log.info(f"[{pair}] 🎯 TP HIT {direction.upper()} at {tp_price:.5f}")

        prefix = pair.split(":")[0].upper() if ":" in pair else ""
        if prefix not in _CRYPTO_PREFIXES:
            # Stocks/forex: close fully at TP (bracket order may have already closed it)
            _close_broker_position(pair)
            manager.close_trade(pair, reason="tp_hit", close_price=tp_price)
            retry_pending_margin()
            return

        log.info(f"[{pair}] Closing 50% at TP — runner continues")
        post_tp = {
            "f1_rsi14":    features.get("f1_rsi14"),
            "f2_wt":       features.get("f2_wt"),
            "f3_cci":      features.get("f3_cci"),
            "f4_adx":      features.get("f4_adx"),
            "f5_rsi9":     features.get("f5_rsi9"),
            "pivot_slope": (features.get("pivot_slope_high") if direction == "short"
                            else features.get("pivot_slope_low")),
            "atr_ratio":   _safe_float(raw.get("ATR")) / (trade.atr + 1e-10),
            "ema_distance": features.get("ema_distance"),
            "kernel_dir":  _infer_kernel_dir(features, direction),
        }
        regime, conf = classifier.classify(post_tp, pair, direction)
        log.info(f"[{pair}] 📊 Post-TP regime: {regime} (conf={conf:.2f})")
        handle_runner(pair, direction, regime, conf, tp_price)

    elif event in ("sl_hit_long", "sl_hit_short"):
        direction = "long" if event == "sl_hit_long" else "short"
        sl_price  = _safe_float(raw.get("SL Level"))
        log.info(f"[{pair}] ❌ SL HIT {direction.upper()} at {sl_price:.5f}")
        prefix = pair.split(":")[0].upper() if ":" in pair else ""
        if prefix in _CRYPTO_PREFIXES:
            # For crypto: verify position still exists before executing.
            # Exchange-side SL orders may have already closed the position.
            broker_dir = _broker_position_dir(pair)
            if broker_dir is not None:
                _broker_execute(pair, "short" if direction == "long" else "long",
                                price=sl_price)
            else:
                log.info(f"[{pair}] Position already closed on exchange — state sync only")
        else:
            # Stocks/forex: just close; broker stop order may have already executed it
            _close_broker_position(pair)
        manager.close_trade(pair, reason="sl_hit", close_price=sl_price)
        retry_pending_margin()

    elif event in ("long_runner_exit", "short_runner_exit"):
        direction   = "long" if event == "long_runner_exit" else "short"
        close_price = _safe_float(raw.get("close"))
        log.info(f"[{pair}] 🔚 RUNNER EXIT {direction.upper()} at {close_price:.5f}")
        _close_broker_position(pair)
        manager.close_trade(pair, reason="runner_exit", close_price=close_price)
        retry_pending_margin()


# ─── Runner Manager ───────────────────────────────────────────────────────────

def handle_runner(pair: str, direction: str, regime: str,
                  confidence: float, tp_price: float) -> None:
    if regime == "CONTINUATION":
        if confidence >= 0.75:
            log.info(f"[{pair}] 🟢 CONTINUATION high — keeping runner")
            manager.update_runner_decision(pair, regime, confidence)
        elif confidence >= 0.60:
            log.info(f"[{pair}] 🟡 CONTINUATION med — small runner")
            manager.update_runner_decision(pair, regime, confidence)
        else:
            log.info(f"[{pair}] 🔴 CONTINUATION low conf — closing")
            manager.close_trade(pair, reason="low_confidence", close_price=tp_price)

    elif regime == "CONSOLIDATION":
        log.info(f"[{pair}] ⏸️  CONSOLIDATION — closing runner")
        manager.close_trade(pair, reason="consolidation", close_price=tp_price)
        retry_pending_margin()

    elif regime == "REVERSAL":
        log.info(f"[{pair}] 🔄 REVERSAL — closing runner")
        manager.close_trade(pair, reason="reversal", close_price=tp_price)
        retry_pending_margin()
        if confidence >= 0.70:
            opposite = "long" if direction == "short" else "short"
            log.info(f"[{pair}] 💡 High-conf reversal — flagging {opposite.upper()}")

    else:  # MIXED
        log.info(f"[{pair}] ❓ MIXED — closing conservatively")
        manager.close_trade(pair, reason="mixed", close_price=tp_price)
        retry_pending_margin()


# ─── Whipsaw Scoring ──────────────────────────────────────────────────────────

def _whipsaw_score(pair: str, raw: dict, history: list, sl_streak: int) -> dict:
    """Score 0–100. ≥45 = block. 25–44 = reduce size. <25 = allow."""
    score   = 0
    reasons = []

    recent_sl = sum(1 for t in history if t.result == "sl_hit")
    if   recent_sl >= 3: score += 30; reasons.append(f"{recent_sl} recent SL hits")
    elif recent_sl >= 2: score += 20; reasons.append(f"{recent_sl} recent SL hits")
    elif recent_sl >= 1: score += 10; reasons.append(f"{recent_sl} recent SL hit")

    adx = _safe_float(raw.get("F4 ADX", 0.25))   # normalized 0-1
    if   adx < 0.15: score += 25; reasons.append(f"ADX very weak ({adx:.2f})")
    elif adx < 0.20: score += 15; reasons.append(f"ADX weak ({adx:.2f})")
    elif adx < 0.25: score += 5;  reasons.append(f"ADX moderate ({adx:.2f})")

    atr_ratio = _safe_float(raw.get("ATR Ratio", 1.0))
    if   atr_ratio < 0.70: score += 15; reasons.append(f"ATR contracting ({atr_ratio:.2f})")
    elif atr_ratio < 0.85: score += 8;  reasons.append(f"ATR slight contract ({atr_ratio:.2f})")

    pred = abs(_safe_float(raw.get("Prediction", 0)))
    if   pred <= 2: score += 10; reasons.append(f"Weak prediction ({pred:.0f}/8)")
    elif pred <= 4: score += 5;  reasons.append(f"Moderate prediction ({pred:.0f}/8)")

    vol_ratio = _safe_float(raw.get("Vol Ratio", 1.0))
    if   vol_ratio < 0.75: score += 20; reasons.append(f"Low volume ({vol_ratio:.2f}x avg)")
    elif vol_ratio < 1.0:  score += 10; reasons.append(f"Below avg volume ({vol_ratio:.2f}x)")

    upper_wick = _safe_float(raw.get("Upper Wick Ratio", 0))
    lower_wick = _safe_float(raw.get("Lower Wick Ratio", 0))
    if max(upper_wick, lower_wick) > 0.60:
        score += 10; reasons.append(f"Wick rejection ({max(upper_wick, lower_wick):.2f})")

    action = "block" if score >= 45 else "reduce" if score >= 25 else "allow"
    return {"score": score, "reasons": reasons, "action": action}


# ─── Revenge Trade Filter ─────────────────────────────────────────────────────

def _is_revenge_trade(pair: str, direction: str, history: list) -> bool:
    if not history:
        return False
    last = history[-1]
    if last.result != "sl_hit" or last.direction == direction:
        return False
    if last.bars_since_close <= 2:
        log.warning(f"[{pair}] Revenge trade — last {last.direction} hit SL "
                    f"{last.bars_since_close} bars ago, new={direction}")
        return True
    return False


# ─── Entry Rule Check ─────────────────────────────────────────────────────────

def _passes_entry_rules(raw: dict, rules: dict) -> bool:
    pred      = abs(_safe_float(raw.get("Prediction", 0)))
    adx       = _safe_float(raw.get("F4 ADX", 0))
    atr_ratio = _safe_float(raw.get("ATR Ratio", 1.0))

    failed = []
    if pred      < rules["min_prediction"]: failed.append(f"pred={pred:.0f}<{rules['min_prediction']}")
    if adx       < rules["min_adx"]:        failed.append(f"adx={adx:.1f}<{rules['min_adx']}")
    if atr_ratio < rules["min_atr_ratio"]:  failed.append(f"atr_ratio={atr_ratio:.2f}<{rules['min_atr_ratio']}")

    if failed:
        log.info(f"Entry rules failed: {failed}")
        return False
    return True


# ─── Feature Extraction ───────────────────────────────────────────────────────

def extract_features(raw: dict) -> dict:
    return {
        "f1_rsi14":         _safe_float(raw.get("F1 RSI14")),
        "f2_wt":            _safe_float(raw.get("F2 WT")),
        "f3_cci":           _safe_float(raw.get("F3 CCI")),
        "f4_adx":           _safe_float(raw.get("F4 ADX")),
        "f5_rsi9":          _safe_float(raw.get("F5 RSI9")),
        "ema_distance":     _safe_float(raw.get("EMA Distance")),
        "pivot_slope_high": _safe_float(raw.get("Pivot Slope H")),
        "pivot_slope_low":  _safe_float(raw.get("Pivot Slope L")),
        "upper_wick_ratio": _safe_float(raw.get("Upper Wick Ratio")),
        "lower_wick_ratio": _safe_float(raw.get("Lower Wick Ratio")),
        "tp_progress":      _safe_float(raw.get("TP Progress")),
    }


def _infer_kernel_dir(features: dict, direction: str) -> bool:
    rsi      = features.get("f1_rsi14", 50) or 50
    ema_dist = features.get("ema_distance", 0) or 0
    return (rsi < 50 and ema_dist < 0) if direction == "short" \
           else (rsi > 50 and ema_dist > 0)


def _safe_float(val) -> float:
    try:
        f = float(str(val).replace("−", "-"))  # normalize Unicode minus
        return 0.0 if f != f else f
    except (TypeError, ValueError):
        return 0.0


def _safe_int(val) -> int:
    try:
        f = float(val)
        return 0 if f != f else int(f)
    except (TypeError, ValueError):
        return 0
