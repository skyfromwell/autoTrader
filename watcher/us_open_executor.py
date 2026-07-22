#!/usr/bin/env python3
from __future__ import annotations
"""
US Stocks Market-Open Executor — cron at 06:30 PT (09:30 ET), Mon-Fri

Reads output/us_pending_orders.json and places Alpaca market orders.
Updates position_state.json with entry prices fetched from Alpaca after fill.
"""

import os, sys, json, time, logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from trader.trader import execute_trade as alpaca_execute
from watcher.position_manager import PositionManager

PENDING_FILE = Path("output/us_pending_orders.json")

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s",
                    handlers=[logging.StreamHandler()])

# Same ATR multiples as tv_alert_server.py's forex fallback
# (_FOREX_SL_ATR_MULT/_FOREX_TP_ATR_MULT — matches jingda_ai_v2.pine's
# _autoSl=2.5/_autoTp=5.5) — a reasonable starting point for US stocks
# too, until/unless a different rule is wanted specifically for this
# strategy. This screener had no TP/SL at all before (tp=None, sl=None,
# atr=0.0 hardcoded at the call site) — Alpaca already supports bracket
# orders, execute_trade() already accepts tp/sl, this was just never
# computed and passed through.
_SL_ATR_MULT = 2.5
_TP_ATR_MULT = 5.5


def _alpaca_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(
        api_key=os.getenv("ALPACA_API_KEY"),
        secret_key=os.getenv("ALPACA_SECRET_KEY"),
        paper=True,
    )


def _us_atr(symbol: str, period: int = 14) -> float | None:
    """14-day ATR (true range average) from daily bars via yfinance."""
    try:
        import yfinance as yf
        hist = yf.download(symbol, period=f"{period + 5}d", interval="1d",
                           progress=False, auto_adjust=True)
        if hist.empty or len(hist) < period + 1:
            return None
        high, low, close = hist["High"].squeeze(), hist["Low"].squeeze(), hist["Close"].squeeze()
        trs, prev_close = [], None
        for h, l, c in zip(high, low, close):
            tr = (h - l) if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
            trs.append(tr)
            prev_close = c
        trs = trs[-period:]
        return sum(trs) / len(trs) if trs else None
    except Exception as e:
        log.warning(f"[{symbol}] ATR fetch failed: {e}")
        return None


def main():
    log.info("=" * 55)
    log.info(f"  US Open Executor  |  {datetime.now():%Y-%m-%d %H:%M ET}")
    log.info("=" * 55)

    if not PENDING_FILE.exists():
        log.info("No pending queue — nothing to do"); return

    orders = json.loads(PENDING_FILE.read_text())
    if not orders:
        log.info("Queue empty"); return

    log.info(f"Processing {len(orders)} pending orders")
    manager = PositionManager()
    client  = _alpaca_client()
    executed, failed = [], []

    for order in orders:
        tv_sym   = order["tv_symbol"]
        action   = order["action"]
        reason   = order.get("reason", "sma_signal")
        notional = order.get("notional", 10000)
        symbol   = tv_sym.split(":")[-1]

        try:
            if action in ("open_long", "open_short"):
                direction = "long" if action == "open_long" else "short"

                # Check not already in same-direction position
                try:
                    existing = client.get_open_position(symbol)
                    if existing and existing.side == direction:
                        log.info(f"[{tv_sym}] already {direction} — skipping open")
                        executed.append(order)
                        continue
                except Exception:
                    pass  # no existing position, proceed

                # ATR-based tp/sl needs a price estimate up front — Alpaca's
                # bracket orders are placed alongside the entry order itself,
                # not computed after the fill (see _place_order's entry_price
                # requirement in trader/trader.py).
                atr = _us_atr(symbol)
                tp = sl = None
                if atr:
                    try:
                        import yfinance as yf
                        last_close = float(yf.Ticker(symbol).history(period="1d")["Close"].iloc[-1])
                        if direction == "long":
                            tp, sl = last_close + _TP_ATR_MULT * atr, last_close - _SL_ATR_MULT * atr
                        else:
                            tp, sl = last_close - _TP_ATR_MULT * atr, last_close + _SL_ATR_MULT * atr
                    except Exception as e:
                        log.warning(f"[{tv_sym}] price estimate for tp/sl failed: {e}")

                ok = alpaca_execute(tv_sym, direction, notional=notional, tp=tp, sl=sl)
                if ok:
                    time.sleep(4)  # wait for fill
                    entry = 0.0
                    try:
                        pos   = client.get_open_position(symbol)
                        entry = float(pos.avg_entry_price)
                    except Exception:
                        pass
                    manager.open_trade(
                        pair=tv_sym, direction=direction, entry=entry,
                        tp=tp, sl=sl, atr=atr or 0.0, size=notional, features={},
                        bar_time=datetime.now().isoformat(timespec="seconds"),
                        opened_by="tv_alert",
                    )
                    log.info(f"[{tv_sym}] ✅ OPENED {direction.upper()}  entry={entry:.2f}  "
                             f"notional=${notional:,}  tp={tp}  sl={sl}  atr={atr}")
                    executed.append(order)
                else:
                    log.error(f"[{tv_sym}] order placement failed")
                    failed.append(order)

            elif action == "close":
                try:
                    client.close_position(symbol)
                    time.sleep(3)
                    # Get close price from last trade
                    try:
                        trades = client.get_portfolio_history(period="1D")
                        close_price = 0.0
                    except Exception:
                        close_price = 0.0
                    manager.close_trade(tv_sym, reason, close_price)
                    log.info(f"[{tv_sym}] ✅ CLOSED  reason={reason}")
                    executed.append(order)
                except Exception as e:
                    log.warning(f"[{tv_sym}] close failed: {e}")
                    failed.append(order)

        except Exception as e:
            log.error(f"[{tv_sym}] unexpected error: {e}")
            failed.append(order)

    remaining = failed  # keep only failed orders for retry
    PENDING_FILE.write_text(json.dumps(remaining, indent=2))
    log.info(f"Done: {len(executed)} executed, {len(failed)} failed, {len(remaining)} remaining in queue")


if __name__ == "__main__":
    main()
