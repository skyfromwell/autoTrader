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
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from watcher.regime_classifier import RegimeClassifier
from watcher.position_manager  import PositionManager
from trader.trader             import execute_trade as alpaca_execute
from trader.oanda_trader       import execute_trade as oanda_execute
from trader.hyperliquid_trader import execute_trade as hl_execute
from trader.hyperliquid_trader import close_position as hl_close, _hl_coin, _hl_post

log        = logging.getLogger(__name__)
classifier = RegimeClassifier()
manager    = PositionManager()

_NOTIONAL_BY_GRADE = {"A": 20_000, "B": 10_000}
_NOTIONAL_DEFAULT  = 10_000


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

# Exchange prefix → broker / asset-class routing
_CRYPTO_PREFIXES  = {"BINANCE", "BYBIT", "COINBASE", "KRAKEN", "BITMEX", "BITSTAMP", "PIONEX", "BLOFIN"}
_FOREX_PREFIXES   = {"FX", "OANDA", "FXCM", "FOREXCOM", "PEPPERSTONE"}
_CHINESE_PREFIXES = {"SSE", "SZSE", "HKEX", "SHSE"}   # no broker yet

def _broker_execute(pair: str, direction: str, price: float | None = None,
                    notional: int = _NOTIONAL_DEFAULT) -> None:
    """Route trade execution to the correct broker based on exchange prefix."""
    prefix = pair.split(":")[0].upper() if ":" in pair else ""
    if prefix in _CHINESE_PREFIXES:
        log.info(f"[{pair}] SIGNAL logged — no Chinese broker configured, skipping execution")
        return
    if prefix in _CRYPTO_PREFIXES:
        hl_execute(pair, direction, price=price, notional=notional)
    elif prefix in _FOREX_PREFIXES:
        oanda_execute(pair, direction, price=price, notional=notional)
    else:
        alpaca_execute(pair, direction, price=price, notional=notional)

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
    pair       = raw.get("pair", "UNKNOWN")
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
        elif action == "flip_long":
            log.info(f"[{pair}] 🔄 PRICE TRIGGER flip_long — {cond}@{level}  {note}")
            _close_broker_position(pair)
            manager.close_trade(pair, reason=f"price_trigger_flip@{level}",
                                close_price=mark_price)
            _broker_execute(pair, "long", price=mark_price, notional=notional)
            manager.open_trade(pair=pair, direction="long", entry=mark_price,
                               tp=None, sl=None, atr=trade.atr, size=1.0, features={})
            log.info(f"[{pair}] ✅ Flipped to LONG at {mark_price} notional=${notional:,}")
        elif action == "flip_short":
            log.info(f"[{pair}] 🔄 PRICE TRIGGER flip_short — {cond}@{level}  {note}")
            _close_broker_position(pair)
            manager.close_trade(pair, reason=f"price_trigger_flip@{level}",
                                close_price=mark_price)
            _broker_execute(pair, "short", price=mark_price, notional=notional)
            manager.open_trade(pair=pair, direction="short", entry=mark_price,
                               tp=None, sl=None, atr=trade.atr, size=1.0, features={})
            log.info(f"[{pair}] ✅ Flipped to SHORT at {mark_price} notional=${notional:,}")
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
    """Close any open broker position for this pair (used on signal flip)."""
    prefix = pair.split(":")[0].upper() if ":" in pair else ""
    if prefix in _CRYPTO_PREFIXES:
        hl_close(pair)
    else:
        log.warning(f"[{pair}] Flip close not yet wired for {prefix} — manual close needed")


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
    tp = _safe_float(raw.get("TP Level"))
    sl = _safe_float(raw.get("SL Level"))

    notional = _notional_for(pair, size)

    log.info(f"[{pair}] ✅ NEW {direction.upper()} | "
             f"entry={entry:.5f} tp={tp:.5f} sl={sl:.5f} "
             f"atr={atr:.5f} pred={pred:.0f} size={size} notional=${notional:,} rules={rule_level}")

    manager.open_trade(pair=pair, direction=direction, entry=entry,
                       tp=tp, sl=sl, atr=atr, size=size,
                       features=features, bar_time=raw.get("bar_time"))

    _broker_execute(pair, direction, price=entry if entry else None, notional=notional)


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
        log.info(f"[{pair}] Closing 50% at TP — runner continues")
        # Partial close: Alpaca doesn't support fractional close on stocks easily;
        # log it and let the runner exit close the full position.

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
        _broker_execute(pair, "short" if direction == "long" else "long",
                        price=sl_price)   # close by flipping direction
        manager.close_trade(pair, reason="sl_hit", close_price=sl_price)

    elif event in ("long_runner_exit", "short_runner_exit"):
        direction   = "long" if event == "long_runner_exit" else "short"
        close_price = _safe_float(raw.get("close"))
        log.info(f"[{pair}] 🔚 RUNNER EXIT {direction.upper()} at {close_price:.5f}")
        manager.close_trade(pair, reason="runner_exit", close_price=close_price)


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

    elif regime == "REVERSAL":
        log.info(f"[{pair}] 🔄 REVERSAL — closing runner")
        manager.close_trade(pair, reason="reversal", close_price=tp_price)
        if confidence >= 0.70:
            opposite = "long" if direction == "short" else "short"
            log.info(f"[{pair}] 💡 High-conf reversal — flagging {opposite.upper()}")

    else:  # MIXED
        log.info(f"[{pair}] ❓ MIXED — closing conservatively")
        manager.close_trade(pair, reason="mixed", close_price=tp_price)


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
