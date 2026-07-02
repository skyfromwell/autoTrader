#!/usr/bin/env python3
"""
TV Alert Webhook Server — receives TradingView alerts and executes broker actions.

Run locally:
    python -m watcher.tv_alert_server

Execute queued China open orders (cron at 09:30 CST):
    python -m watcher.tv_alert_server --execute-queue

Expose via Cloudflare tunnel:
    cloudflared tunnel run autotrader-webhook
    → https://tv-alert.gmoainc.com

TV alert message — action (any market):
    {"pair": "BYBIT:HYPEUSDC.P", "action": "close_and_flip", "direction": "long"}
    {"pair": "BYBIT:HYPEUSDC.P", "action": "move_sl", "value": 68.0}
    {"pair": "OANDA:NZDUSD",     "action": "close"}
    {"pair": "NYSE:GS",          "action": "open_long"}

TV alert message — signal (SSE/SZSE only, queued for next-day open):
    {"pair": "{{ticker}}", "timeframe": "{{interval}}", "signal": {{Signal_Stream}}, "price": {{close}}}

Supported actions:  open_long, open_short, close, close_and_flip, move_sl, partial_close
Supported signals:  1 (long → queue buy), -1 (short → warn if long / cancel queue)
Supported TFs:      30, 60, 240, D
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
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(Path(__file__).parent.parent / ".env")

from trader.hyperliquid_trader import execute_trade as hl_execute
from trader.hyperliquid_trader import close_position as hl_close
from trader.hyperliquid_trader import partial_close_position as hl_partial_close
from trader.oanda_trader        import execute_trade as oanda_execute
from trader.oanda_trader        import close_position as oanda_close
from trader.trader              import execute_trade as alpaca_execute
from trader.china_trader        import execute_trade as china_execute
from trader.china_trader        import close_position as china_close
from watcher.position_manager   import PositionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

TV_SECRET         = os.getenv("TV_WEBHOOK_SECRET", "")
CHINA_URL         = os.getenv("CHINA_SERVER_URL", "http://100.64.0.1:8888")
CHINA_KEY         = os.getenv("CHINA_API_KEY", "")
CHINA_NOTIONAL    = int(os.getenv("CHINA_NOTIONAL_CNY", "50000"))
PENDING_FILE      = Path("output/china_pending_orders.json")
_NOTIONAL_DEFAULT = 10_000

_CRYPTO_PREFIXES   = {"BINANCE", "BYBIT", "COINBASE", "KRAKEN", "BITMEX", "PIONEX", "BLOFIN"}
_FOREX_PREFIXES    = {"FX", "OANDA", "FXCM", "FOREXCOM", "PEPPERSTONE"}
_CHINESE_PREFIXES  = {"SSE", "SZSE", "HKEX", "SHSE"}
_VALID_TIMEFRAMES  = {"30", "60", "240", "D"}

app     = FastAPI(title="TV Alert Webhook")
manager = PositionManager()


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    log.error(f"422 validation error | body={body!r} | errors={exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# ── Auth ──────────────────────────────────────────────────────────────────────

def _verify(header_secret: str | None, query_secret: str | None = None):
    if not TV_SECRET:
        log.warning("TV_WEBHOOK_SECRET not set — running unauthenticated!")
        return
    if header_secret == TV_SECRET or query_secret == TV_SECRET:
        return
    raise HTTPException(status_code=401, detail="Invalid secret")


# ── Broker routing ────────────────────────────────────────────────────────────

def _prefix(pair: str) -> str:
    return pair.split(":")[0].upper() if ":" in pair else ""


def _broker_open(pair: str, direction: str, price: float | None, notional: int) -> None:
    p = _prefix(pair)
    if p in _CRYPTO_PREFIXES:
        hl_execute(pair, direction, price=price, notional=notional)
    elif p in _FOREX_PREFIXES:
        oanda_execute(pair, direction, price=price, notional=notional)
    elif p in _CHINESE_PREFIXES:
        china_execute(pair, direction, price=price or 0, notional=notional)
    else:
        alpaca_execute(pair, direction, price=price, notional=notional)


def _broker_close(pair: str, price: float = 0) -> None:
    p = _prefix(pair)
    if p in _CRYPTO_PREFIXES:
        hl_close(pair)
    elif p in _FOREX_PREFIXES:
        oanda_close(pair)
    elif p in _CHINESE_PREFIXES:
        china_close(pair)
    else:
        log.warning(f"[{pair}] Alpaca close not wired — manual close needed")


# ── China pending order queue ─────────────────────────────────────────────────

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


# ── China miniQMT helpers ─────────────────────────────────────────────────────

def _china_get(path: str) -> dict:
    req = urllib.request.Request(f"{CHINA_URL}{path}", headers={"X-API-Key": CHINA_KEY})
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
    return code + (".SZ" if prefix == "SZSE" else ".SS")


def _buy_market(tv_pair: str, signal_price: float, notional: int = CHINA_NOTIONAL) -> dict:
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
        raise ValueError(f"0 shares at ¥{px:.2f} notional=¥{notional}")
    result = _china_post("/order", {"symbol": qmt_sym, "direction": "buy",
                                    "volume": volume, "price": 0, "order_type": "market"})
    log.info(f"[China] BUY {qmt_sym}  {volume}sh @ mkt  order_id={result.get('order_id')}  est=¥{volume*px:.0f}")
    return {"symbol": qmt_sym, "volume": volume, "order_id": result.get("order_id"),
            "exec_price_est": px, "approx_notional": round(volume * px)}


def _qmt_volume(tv_pair: str) -> int:
    try:
        pos = _china_get("/positions")
        qmt_sym = _tv_to_qmt(tv_pair)
        return next((p["volume"] for p in pos if p["symbol"] == qmt_sym), 0)
    except Exception:
        return 0


def execute_queue() -> list[dict]:
    """Place market buy orders for all pending items. Call at 09:30 CST."""
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
        try:
            res = _buy_market(pair, order["signal_price"], order.get("notional", CHINA_NOTIONAL))
        except Exception as e:
            log.error(f"[Queue] {pair} failed: {e}")
            results.append({"pair": pair, "status": "error", "error": str(e)})
            continue
        if res.get("order_id", -1) == -1:
            log.error(f"[Queue] {pair} rejected (order_id=-1)")
            results.append({"pair": pair, "status": "rejected"})
            continue
        manager.open_trade(pair=pair, direction="long", entry=res["exec_price_est"],
                           tp=None, sl=None, atr=0, size=1.0, features={})
        _dequeue(pair)
        log.info(f"[Queue] ✅ {pair}  vol={res['volume']}  order={res['order_id']}")
        results.append({"pair": pair, "status": "ok", **res})
    return results


# ── Payload ───────────────────────────────────────────────────────────────────

class AlertPayload(BaseModel):
    pair:      str
    action:    Optional[str]   = None  # open_long|open_short|close|close_and_flip|move_sl|partial_close
    signal:    Optional[int]   = None  # 1=long, -1=short  (SSE/SZSE signal alerts)
    timeframe: Optional[str]   = None  # "30"|"60"|"240"|"D"  (required with signal)
    direction: Optional[str]   = None  # for close_and_flip: new direction
    value:     Optional[float] = None  # new SL price (move_sl / partial_close)
    fraction:  float           = 2/3   # for partial_close
    price:     Optional[float] = None  # bar close price  {{close}}
    notional:  int             = _NOTIONAL_DEFAULT
    note:      Optional[str]   = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "authenticated": bool(TV_SECRET),
            "china_pending": len(_load_pending())}


@app.get("/pending")
def list_pending(x_tv_secret: Optional[str] = Header(None), secret: Optional[str] = Query(None)):
    _verify(x_tv_secret, secret)
    pending = _load_pending()
    return {"count": len(pending), "orders": list(pending.values())}


@app.delete("/pending/{pair:path}")
def remove_pending(pair: str,
                   x_tv_secret: Optional[str] = Header(None),
                   secret: Optional[str] = Query(None)):
    _verify(x_tv_secret, secret)
    pair = pair.replace("%3A", ":").upper()
    if pair not in _load_pending():
        raise HTTPException(404, f"{pair} not in pending queue")
    _dequeue(pair)
    return {"ok": True, "removed": pair}


@app.post("/execute-queue")
async def trigger_execute_queue(x_tv_secret: Optional[str] = Header(None),
                                secret: Optional[str] = Query(None)):
    _verify(x_tv_secret, secret)
    results = execute_queue()
    return {"executed": sum(1 for r in results if r["status"] == "ok"),
            "errors":   sum(1 for r in results if r["status"] == "error"),
            "results":  results}


@app.post("/alert")
async def receive_alert(
    request:     Request,
    x_tv_secret: Optional[str] = Header(None),
    secret:      Optional[str] = Query(None),
):
    _verify(x_tv_secret, secret)
    body = await request.body()
    try:
        data = json.loads(body)
    except Exception:
        log.error(f"Non-JSON body received: {body!r}")
        raise HTTPException(400, "Expected JSON body")
    log.info(f"Alert received: {data}")
    try:
        payload = AlertPayload(**data)
    except Exception as e:
        log.error(f"Payload validation failed: {e}  data={data}")
        raise HTTPException(422, str(e))

    pair     = payload.pair.upper()
    price    = payload.price
    note     = payload.note or ""
    p        = _prefix(pair)
    is_china = p in _CHINESE_PREFIXES

    # ── Signal alerts (SSE/SZSE queue flow) ───────────────────────────────────
    if payload.signal is not None:
        if not is_china:
            return {"ok": True, "skipped": True, "reason": "non_china_signal"}

        tf = str(payload.timeframe or "")
        if tf not in _VALID_TIMEFRAMES:
            raise HTTPException(400, f"timeframe '{tf}' not in {_VALID_TIMEFRAMES}")

        signal       = payload.signal
        trade        = manager.get_trade(pair)
        has_position = trade is not None and not getattr(trade, "closed", False)

        if signal == 1:
            if has_position:
                log.info(f"[TV→] {pair} ({tf}) LONG — already in position, skip")
                return {"ok": True, "skipped": True, "reason": "already_long"}
            pending = _load_pending()
            if pair in pending:
                log.info(f"[TV→] {pair} ({tf}) LONG — already queued, skip")
                return {"ok": True, "skipped": True, "reason": "already_queued",
                        "queued_at": pending[pair]["queued_at"]}
            notional = payload.notional if payload.notional != _NOTIONAL_DEFAULT else CHINA_NOTIONAL
            _queue_order(pair, price or 0, tf, notional)
            return {"ok": True, "action": "queued", "pair": pair, "timeframe": tf,
                    "signal_price": price, "notional": notional,
                    "note": "Will execute at next-day open via POST /execute-queue"}

        elif signal == -1:
            if has_position:
                log.warning(f"[TV→] ⚠️  SHORT on {pair} ({tf}) but LONG open — A-shares cannot short!")
                return {"ok": True, "warning": True, "reason": "short_signal_while_long",
                        "message": f"SHORT signal {pair} ({tf}) — cannot short A-shares. Close manually if needed."}
            pending = _load_pending()
            if pair in pending:
                _dequeue(pair)
                log.info(f"[TV→] {pair} ({tf}) SHORT — removed from queue")
                return {"ok": True, "action": "dequeued",
                        "note": "Short signal cancelled queued entry"}
            return {"ok": True, "skipped": True, "reason": "short_no_position"}

        raise HTTPException(400, f"Unexpected signal value: {signal}")

    # ── Action alerts (all markets) ────────────────────────────────────────────
    if payload.action is None:
        raise HTTPException(400, "Provide either 'action' or 'signal'")

    action = payload.action.lower().replace("-", "_")
    log.info(f"[TV→] {pair}  action={action}  price={price}  note={note}")

    if action in ("open_long", "open_short"):
        direction = "long" if action == "open_long" else "short"
        _broker_open(pair, direction, price, payload.notional)
        manager.open_trade(pair, direction,
                           entry=price or 0, tp=None, sl=None, atr=0,
                           size=1.0, features={})
        log.info(f"[TV→] ✅ Opened {direction.upper()} {pair}")
        return {"ok": True, "action": action, "pair": pair, "direction": direction}

    if action == "close":
        _broker_close(pair, price or 0)
        manager.close_trade(pair, reason="tv_alert_close", close_price=price or 0)
        log.info(f"[TV→] ✅ Closed {pair}")
        return {"ok": True, "action": action, "pair": pair}

    if action == "close_and_flip":
        new_dir = (payload.direction or "long").lower()
        if new_dir not in ("long", "short"):
            raise HTTPException(400, f"direction must be 'long' or 'short', got '{new_dir}'")
        log.info(f"[TV→] Closing {pair} then flipping → {new_dir.upper()}")
        _broker_close(pair, price or 0)
        manager.close_trade(pair, reason=f"tv_alert_flip_to_{new_dir}@{price}",
                            close_price=price or 0)
        _broker_open(pair, new_dir, price, payload.notional)
        manager.open_trade(pair, new_dir,
                           entry=price or 0, tp=None, sl=None, atr=0,
                           size=1.0, features={})
        log.info(f"[TV→] ✅ Flipped {pair} → {new_dir.upper()} at {price}")
        return {"ok": True, "action": action, "pair": pair, "new_direction": new_dir}

    if action == "partial_close":
        trade = manager.get_trade(pair)
        if not trade:
            raise HTTPException(404, f"No open position for {pair}")
        if p in _CRYPTO_PREFIXES:
            hl_partial_close(pair, payload.fraction)
        else:
            log.warning(f"[{pair}] partial_close only implemented for HL crypto")
        if payload.value is not None:
            manager.move_sl(pair, payload.value, reason=f"tv_partial_close_sl@{price}")
        log.info(f"[TV→] ✅ Partial close {pair} fraction={payload.fraction:.2f} SL→{payload.value}")
        return {"ok": True, "action": action, "pair": pair,
                "fraction": payload.fraction, "new_sl": payload.value}

    if action == "move_sl":
        if payload.value is None:
            raise HTTPException(400, "move_sl requires 'value' (new SL price)")
        trade = manager.get_trade(pair)
        if not trade:
            raise HTTPException(404, f"No open position for {pair}")
        manager.move_sl(pair, payload.value, reason=f"tv_alert_move_sl@{price}")
        if p in _CRYPTO_PREFIXES:
            if pair.upper().startswith("XYZ:"):
                from trader.xyz_trader import move_sl as xyz_move_sl
                xyz_move_sl(pair.replace("XYZ:", "xyz:"), payload.value, trade.direction)
            else:
                log.info(f"[TV→] HL SL move for {pair} — JS cancel+replace needed")
        log.info(f"[TV→] ✅ Moved SL {pair} → {payload.value}")
        return {"ok": True, "action": action, "pair": pair, "new_sl": payload.value}

    raise HTTPException(400, f"Unknown action '{action}'. "
                        "Use: open_long, open_short, close, close_and_flip, move_sl, partial_close")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--execute-queue" in sys.argv:
        results  = execute_queue()
        ok_count = sum(1 for r in results if r["status"] == "ok")
        err_count = sum(1 for r in results if r["status"] == "error")
        print(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"\n✅ {ok_count} executed  ❌ {err_count} errors")
    else:
        port = int(os.getenv("TV_WEBHOOK_PORT", "9999"))
        log.info(f"TV Alert Webhook Server starting on port {port}")
        log.info(f"Tunnel: cloudflared tunnel run autotrader-webhook")
        log.info(f"China pending queue: {PENDING_FILE}")
        uvicorn.run("watcher.tv_alert_server:app", host="0.0.0.0", port=port, reload=False)
