#!/usr/bin/env python3
"""
China A-Share TV Alert Handler — multi-timeframe webhook for SSE + SZSE stocks.

Signal flow:
  1. TradingView alert fires  →  POST /alert  →  queued in pending_orders.json
  2. Next morning at 09:30 CST →  POST /execute-queue  (or python -m watcher.china_tv_alerts --execute-queue)
                                  →  market buy orders placed via miniQMT

Run server:
    python -m watcher.china_tv_alerts

Execute queued orders (cron at 09:30 CST):
    python -m watcher.china_tv_alerts --execute-queue

Expose via Cloudflare tunnel (separate from main tv_alert_server):
    cloudflared tunnel --url http://localhost:9998

TradingView alert message (signal):
    {"pair": "{{ticker}}", "timeframe": "{{interval}}", "signal": {{Signal_Stream}}, "price": {{close}}}

TradingView alert message (system action):
    {"pair": "{{ticker}}", "action": "move_sl", "value": 7.50, "price": {{close}}}

Supported timeframes: 30, 60, 240, D
Signal behaviour (SSE/SZSE only):
  signal=1   + no position + not queued  → add to pending queue for next-day open
  signal=1   + already queued/in position → skip
  signal=-1  + open position             → WARNING (cannot short A-shares)
  signal=-1  + queued                    → remove from queue
  signal=-1  + no position               → ignore
System actions (any market): move_sl, close, open_long, open_short, partial_close
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(Path(__file__).parent.parent / ".env")

from watcher.position_manager import PositionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

TV_SECRET        = os.getenv("TV_WEBHOOK_SECRET", "")
CHINA_URL        = os.getenv("CHINA_SERVER_URL", "http://100.64.0.1:8888")
CHINA_KEY        = os.getenv("CHINA_API_KEY", "")
DEFAULT_NOTIONAL = int(os.getenv("CHINA_NOTIONAL_CNY", "50000"))
PENDING_FILE     = Path("output/china_pending_orders.json")

VALID_TIMEFRAMES = {"30", "60", "240", "D"}
CHINA_PREFIXES   = {"SSE", "SZSE"}

app     = FastAPI(title="China TV Alert Handler")
manager = PositionManager()


# ── Pending order queue ───────────────────────────────────────────────────────

def _load_pending() -> dict:
    if PENDING_FILE.exists():
        return json.loads(PENDING_FILE.read_text())
    return {}


def _save_pending(pending: dict) -> None:
    PENDING_FILE.parent.mkdir(exist_ok=True)
    PENDING_FILE.write_text(json.dumps(pending, indent=2))


def _queue_order(pair: str, price: float, timeframe: str, notional: int) -> None:
    pending = _load_pending()
    pending[pair] = {
        "pair":         pair,
        "notional":     notional,
        "signal_price": price,
        "timeframe":    timeframe,
        "queued_at":    datetime.now().isoformat(timespec="seconds"),
    }
    _save_pending(pending)
    log.info(f"[Queue] ➕ {pair} ({timeframe})  price={price}  notional=¥{notional:,}  "
             f"→ pending ({len(pending)} total)")


def _dequeue(pair: str) -> None:
    pending = _load_pending()
    if pair in pending:
        pending.pop(pair)
        _save_pending(pending)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _verify(header_secret: str | None, query_secret: str | None):
    if not TV_SECRET:
        log.warning("TV_WEBHOOK_SECRET not set — unauthenticated")
        return
    if header_secret == TV_SECRET or query_secret == TV_SECRET:
        return
    raise HTTPException(status_code=401, detail="Invalid secret")


# ── China broker helpers ──────────────────────────────────────────────────────

def _china_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{CHINA_URL}{path}",
        headers={"X-API-Key": CHINA_KEY},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _china_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{CHINA_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": CHINA_KEY},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _tv_to_qmt(tv_pair: str) -> str:
    """SZSE:000725 → 000725.SZ,  SSE:600028 → 600028.SS"""
    prefix, code = tv_pair.split(":", 1)
    suffix = ".SZ" if prefix == "SZSE" else ".SS"
    return code + suffix


def _qmt_positions() -> dict[str, dict]:
    try:
        return {p["symbol"]: p for p in _china_get("/positions")}
    except Exception as e:
        log.warning(f"Could not fetch China positions: {e}")
        return {}


def _buy_market(tv_pair: str, signal_price: float, notional: int = DEFAULT_NOTIONAL) -> dict:
    """Place a market buy order. Fetches live price for accurate lot sizing."""
    qmt_sym = _tv_to_qmt(tv_pair)

    try:
        quote = _china_get(f"/quote?symbol={qmt_sym}")
        px    = float(quote.get("price") or quote.get("last") or signal_price)
    except Exception:
        px = signal_price

    if px <= 0:
        raise ValueError(f"Invalid price {px} for {tv_pair}")
    volume = int((notional // px) // 100) * 100
    if volume <= 0:
        raise ValueError(f"Calculated 0 shares for {tv_pair} at ¥{px:.2f} notional=¥{notional}")

    result = _china_post("/order", {
        "symbol":     qmt_sym,
        "direction":  "buy",
        "volume":     volume,
        "price":      0,
        "order_type": "market",
    })
    log.info(f"[China] BUY {qmt_sym}  {volume} shares @ mkt  "
             f"order_id={result.get('order_id')}  est=¥{volume*px:.0f}")
    return {"symbol": qmt_sym, "volume": volume, "order_id": result.get("order_id"),
            "exec_price_est": px, "approx_notional": round(volume * px)}


def _sell_market(tv_pair: str, volume: int) -> dict:
    qmt_sym = _tv_to_qmt(tv_pair)
    result  = _china_post("/order", {
        "symbol":     qmt_sym,
        "direction":  "sell",
        "volume":     volume,
        "price":      0,
        "order_type": "market",
    })
    log.info(f"[China] SELL {qmt_sym}  {volume} shares @ mkt  order_id={result.get('order_id')}")
    return result


# ── Execute all queued orders ─────────────────────────────────────────────────

def execute_queue() -> list[dict]:
    """
    Place market buy orders for all pending items.
    Skips any where a position is already open.
    Called at next-day market open (09:30 CST).
    """
    pending = _load_pending()
    if not pending:
        log.info("[Queue] Nothing pending.")
        return []

    log.info(f"[Queue] Executing {len(pending)} pending orders")
    results = []

    for pair, order in list(pending.items()):
        trade = manager.get_trade(pair)
        if trade and not getattr(trade, "closed", False):
            log.info(f"[Queue] {pair} — already in position, skip")
            _dequeue(pair)
            results.append({"pair": pair, "status": "skipped", "reason": "already_long"})
            continue

        notional = order.get("notional", DEFAULT_NOTIONAL)
        try:
            res = _buy_market(pair, order["signal_price"], notional)
        except Exception as e:
            log.error(f"[Queue] {pair} order failed: {e}")
            results.append({"pair": pair, "status": "error", "error": str(e)})
            continue

        if res.get("order_id", -1) == -1:
            log.error(f"[Queue] {pair} rejected (order_id=-1)")
            results.append({"pair": pair, "status": "rejected"})
            continue

        manager.open_trade(pair=pair, direction="long",
                           entry=res["exec_price_est"],
                           tp=None, sl=None, atr=0, size=notional, features={},
                           opened_by="tv_alert")
        _dequeue(pair)
        log.info(f"[Queue] ✅ {pair}  vol={res['volume']}  order={res['order_id']}")
        results.append({"pair": pair, "status": "ok", **res})

    return results


# ── Payload ───────────────────────────────────────────────────────────────────

class AlertPayload(BaseModel):
    pair:      str
    timeframe: Optional[str]   = None
    signal:    Optional[int]   = None   # 1=long, -1=short
    action:    Optional[str]   = None   # move_sl | close | open_long | open_short | partial_close
    value:     Optional[float] = None   # new SL price (for move_sl)
    fraction:  float           = 2/3    # for partial_close
    price:     Optional[float] = None   # bar close price
    notional:  Optional[int]   = None   # override default notional (CNY)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status":               "ok",
        "china_url":            CHINA_URL,
        "default_notional_cny": DEFAULT_NOTIONAL,
        "pending_count":        len(_load_pending()),
    }


@app.get("/pending")
def list_pending(x_tv_secret: Optional[str] = Header(None), secret: Optional[str] = Query(None)):
    _verify(x_tv_secret, secret)
    pending = _load_pending()
    return {"count": len(pending), "orders": list(pending.values())}


@app.delete("/pending/{pair:path}")
def remove_pending(pair: str, x_tv_secret: Optional[str] = Header(None), secret: Optional[str] = Query(None)):
    _verify(x_tv_secret, secret)
    pair = pair.replace("%3A", ":").upper()
    pending = _load_pending()
    if pair not in pending:
        raise HTTPException(404, f"{pair} not in pending queue")
    _dequeue(pair)
    return {"ok": True, "removed": pair}


@app.post("/execute-queue")
async def trigger_execute_queue(x_tv_secret: Optional[str] = Header(None), secret: Optional[str] = Query(None)):
    _verify(x_tv_secret, secret)
    results   = execute_queue()
    ok_count  = sum(1 for r in results if r["status"] == "ok")
    err_count = sum(1 for r in results if r["status"] == "error")
    return {"executed": ok_count, "errors": err_count, "results": results}


@app.post("/alert")
async def receive_alert(
    payload:     AlertPayload,
    x_tv_secret: Optional[str] = Header(None),
    secret:      Optional[str] = Query(None),
):
    _verify(x_tv_secret, secret)

    pair     = payload.pair.upper()
    prefix   = pair.split(":")[0] if ":" in pair else ""
    price    = payload.price or 0.0
    notional = payload.notional or DEFAULT_NOTIONAL
    is_china = prefix in CHINA_PREFIXES

    log.info(f"[TV→] {pair}  tf={payload.timeframe}  signal={payload.signal}  "
             f"action={payload.action}  price={price}")

    # ── System action alerts ───────────────────────────────────────────────────
    if payload.action:
        return await _handle_action(pair, payload.action.lower().replace("-", "_"),
                                    payload, price, notional, is_china)

    # ── Signal alerts ──────────────────────────────────────────────────────────
    if payload.signal is None:
        raise HTTPException(400, "Provide either 'action' or 'signal'")

    tf = str(payload.timeframe or "")
    if tf not in VALID_TIMEFRAMES:
        raise HTTPException(400, f"timeframe '{tf}' not in {VALID_TIMEFRAMES}")

    if not is_china:
        return {"ok": True, "skipped": True, "reason": "non_china_signal"}

    signal       = payload.signal
    trade        = manager.get_trade(pair)
    has_position = trade is not None and not getattr(trade, "closed", False)

    if signal == 1:
        if has_position:
            log.info(f"[TV→] {pair} ({tf}) LONG — already in position, skip")
            return {"ok": True, "skipped": True, "reason": "already_long"}

        pending = _load_pending()
        if pair in pending:
            log.info(f"[TV→] {pair} ({tf}) LONG — already queued ({pending[pair]['queued_at']}), skip")
            return {"ok": True, "skipped": True, "reason": "already_queued",
                    "queued_at": pending[pair]["queued_at"]}

        _queue_order(pair, price, tf, notional)
        return {"ok": True, "action": "queued", "pair": pair, "timeframe": tf,
                "signal_price": price, "notional": notional,
                "note": "Will execute at next-day open via POST /execute-queue"}

    elif signal == -1:
        if has_position:
            log.warning(f"[TV→] ⚠️  SHORT on {pair} ({tf}) but LONG open — A-shares cannot short!")
            return {"ok": True, "warning": True, "reason": "short_signal_while_long",
                    "message": f"SHORT signal {pair} ({tf}) — A-shares no shorting. Close manually if needed."}

        pending = _load_pending()
        if pair in pending:
            _dequeue(pair)
            log.info(f"[TV→] {pair} ({tf}) SHORT — removed from queue")
            return {"ok": True, "action": "dequeued",
                    "note": "Short signal cancelled queued entry"}

        log.info(f"[TV→] {pair} ({tf}) SHORT — no position/queue, ignore")
        return {"ok": True, "skipped": True, "reason": "short_no_position"}

    raise HTTPException(400, f"Unexpected signal value: {signal}")


async def _handle_action(pair: str, action: str, payload: AlertPayload,
                          price: float, notional: int, is_china: bool) -> dict:
    if action == "move_sl":
        if payload.value is None:
            raise HTTPException(400, "move_sl requires 'value'")
        trade = manager.get_trade(pair)
        if not trade:
            raise HTTPException(404, f"No open position for {pair}")
        manager.move_sl(pair, payload.value, reason=f"tv_alert_move_sl@{price}")
        return {"ok": True, "action": action, "pair": pair, "new_sl": payload.value}

    if action == "close":
        if is_china:
            trade = manager.get_trade(pair)
            vol   = getattr(trade, "volume", 0) if trade else 0
            if not vol:
                qmt_sym = _tv_to_qmt(pair)
                vol = _qmt_positions().get(qmt_sym, {}).get("volume", 0)
            if vol:
                _sell_market(pair, vol)
        else:
            prefix = pair.split(":")[0]
            if prefix in {"FX", "OANDA", "FXCM", "FOREXCOM", "PEPPERSTONE"}:
                from trader.oanda_trader import close_position as oanda_close
                oanda_close(pair)
            else:
                from trader.hyperliquid_trader import close_position as hl_close
                hl_close(pair)
        manager.close_trade(pair, reason="tv_alert_close", close_price=price)
        log.info(f"[TV→] ✅ close {pair}")
        return {"ok": True, "action": action, "pair": pair}

    if action in ("open_long", "open_short"):
        direction = "long" if action == "open_long" else "short"
        if is_china and direction == "short":
            raise HTTPException(400, "Cannot short A-shares")
        if is_china:
            res = _buy_market(pair, price, notional)
            manager.open_trade(pair=pair, direction="long", entry=res["exec_price_est"],
                               tp=None, sl=None, atr=0, size=notional, features={},
                               opened_by="tv_alert")
            return {"ok": True, "action": action, "pair": pair, **res}
        from trader.oanda_trader import execute_trade as oanda_execute
        oanda_execute(pair, direction, price=price, notional=notional)
        manager.open_trade(pair=pair, direction=direction, entry=price,
                           tp=None, sl=None, atr=0, size=notional, features={},
                           opened_by="tv_alert")
        return {"ok": True, "action": action, "pair": pair, "direction": direction}

    if action == "partial_close":
        trade = manager.get_trade(pair)
        if not trade:
            raise HTTPException(404, f"No open position for {pair}")
        from trader.hyperliquid_trader import partial_close_position as hl_partial_close
        hl_partial_close(pair, payload.fraction)
        if payload.value is not None:
            manager.move_sl(pair, payload.value, reason=f"tv_partial_close_sl@{price}")
        return {"ok": True, "action": action, "pair": pair, "fraction": payload.fraction}

    raise HTTPException(400, f"Unknown action '{action}'")


# ── CLI / main ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--execute-queue" in sys.argv:
        results   = execute_queue()
        ok_count  = sum(1 for r in results if r["status"] == "ok")
        err_count = sum(1 for r in results if r["status"] == "error")
        print(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"\n✅ {ok_count} executed  ❌ {err_count} errors")
    else:
        port = int(os.getenv("CHINA_ALERT_PORT", "9998"))
        log.info(f"China TV Alert Handler starting on port {port}")
        log.info(f"Valid timeframes: {VALID_TIMEFRAMES}")
        log.info(f"Default notional: ¥{DEFAULT_NOTIONAL:,}  (signals queued → execute at open)")
        uvicorn.run("watcher.china_tv_alerts:app", host="0.0.0.0", port=port, reload=False)
