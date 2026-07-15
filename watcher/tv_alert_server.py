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
import re
import sys
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
from trader.oanda_trader        import _request as oanda_request, _ACCOUNT as _OANDA_ACCOUNT, _oanda_instrument
from trader.trader              import execute_trade as alpaca_execute
from trader.china_trader        import execute_trade as china_execute
from trader.china_trader        import close_position as china_close
from watcher.position_manager   import PositionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

TV_SECRET         = os.getenv("TV_WEBHOOK_SECRET", "")
CHINA_NOTIONAL    = int(os.getenv("CHINA_NOTIONAL_CNY", "50000"))
CHINA_PENDING_DIR = Path("output/china_pending")
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


def _broker_open(pair: str, direction: str, price: float | None, notional: int,
                 tp: float | None = None, sl: float | None = None) -> None:
    p = _prefix(pair)
    if p in _CRYPTO_PREFIXES:
        hl_execute(pair, direction, price=price, notional=notional, tp=tp, sl=sl)
    elif p in _FOREX_PREFIXES:
        oanda_execute(pair, direction, price=price, notional=notional, tp=tp, sl=sl)
    elif p in _CHINESE_PREFIXES:
        china_execute(pair, direction, price=price or 0, notional=notional)
    else:
        alpaca_execute(pair, direction, price=price, notional=notional, tp=tp, sl=sl)


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


def _oanda_live_position(pair: str) -> str | None:
    """Return 'long'|'short' if pair has a live position on Oanda, else None."""
    if not pair.upper().startswith("OANDA:"):
        return None
    try:
        instrument = _oanda_instrument(pair)
        r = oanda_request("GET", f"/accounts/{_OANDA_ACCOUNT}/positions/{instrument}")
        pos = r.get("position", {})
        long_units  = float(pos.get("long",  {}).get("units", 0))
        short_units = float(pos.get("short", {}).get("units", 0))
        if long_units > 0:
            return "long"
        if short_units < 0:
            return "short"
        return None
    except Exception as e:
        log.warning(f"[{pair}] Oanda position check failed: {e}")
        return None


def _hl_live_position(pair: str) -> str | None:
    """Return 'long'|'short' if pair has a live position on Hyperliquid, else None."""
    p = _prefix(pair)
    if p not in _CRYPTO_PREFIXES:
        return None
    try:
        import requests as _req
        wallet = os.environ.get("HL_WALLET_ADDRESS", "")
        if not wallet:
            return None
        coin = pair.split(":")[-1].replace("USDC.P", "").replace("USDT.P", "")
        r = _req.post("https://api.hyperliquid.xyz/info",
                      json={"type": "clearinghouseState", "user": wallet}, timeout=8)
        for ap in r.json().get("assetPositions", []):
            pos = ap.get("position", {})
            if pos.get("coin") == coin:
                szi = float(pos.get("szi", 0))
                if szi > 0: return "long"
                if szi < 0: return "short"
        return None
    except Exception as e:
        log.warning(f"[{pair}] HL position check failed: {e}")
        return None


def _broker_live_position(pair: str) -> str | None:
    """Return 'long'|'short' if pair has a live position on the broker, else None."""
    p = _prefix(pair)
    if p in _FOREX_PREFIXES:
        return _oanda_live_position(pair)
    if p in _CRYPTO_PREFIXES:
        return _hl_live_position(pair)
    return None


# ── China pending order queue (Syncthing file-based) ─────────────────────────
# Each pending order is a separate JSON file: output/china_pending/SZSE_000725.json
# Syncthing replicates the folder to Windows in real time.
# china_executor.py on Windows watches the folder, executes, then deletes the file.
# Deletion propagates back via Syncthing — no HTTP polling needed.

def _pair_filename(pair: str) -> str:
    """SZSE:000725 → SZSE_000725.json"""
    return pair.replace(":", "_") + ".json"


def _filename_to_pair(stem: str) -> str:
    """SZSE_000725 → SZSE:000725  (replace first _ with :)"""
    return stem.replace("_", ":", 1)


def _load_pending() -> dict:
    CHINA_PENDING_DIR.mkdir(parents=True, exist_ok=True)
    result = {}
    for f in sorted(CHINA_PENDING_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            pair = data.get("pair") or _filename_to_pair(f.stem)
            result[pair] = data
        except Exception:
            pass
    return result


def _queue_order(pair: str, price: float, timeframe: str, notional: int) -> None:
    CHINA_PENDING_DIR.mkdir(parents=True, exist_ok=True)
    order = {
        "pair":         pair,
        "notional":     notional,
        "signal_price": price,
        "timeframe":    timeframe,
        "queued_at":    datetime.now().isoformat(timespec="seconds"),
    }
    fpath = CHINA_PENDING_DIR / _pair_filename(pair)
    fpath.write_text(json.dumps(order, indent=2))
    pending_count = len(list(CHINA_PENDING_DIR.glob("*.json")))
    log.info(f"[Queue] ➕ {pair} ({timeframe})  price={price}  notional=¥{notional:,}  "
             f"→ {fpath.name}  ({pending_count} total)")


def _dequeue(pair: str) -> None:
    fpath = CHINA_PENDING_DIR / _pair_filename(pair)
    fpath.unlink(missing_ok=True)


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
    tp:        Optional[float] = None  # take-profit price from Jingda signal
    sl:        Optional[float] = None  # stop-loss price from Jingda signal
    atr:       Optional[float] = None  # ATR at signal bar
    notional:  int             = _NOTIONAL_DEFAULT
    note:      Optional[str]   = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "authenticated": bool(TV_SECRET),
            "china_pending": len(list(CHINA_PENDING_DIR.glob("*.json")))
            if CHINA_PENDING_DIR.exists() else 0}


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


def _parse_plain_text(text: str) -> dict | None:
    """
    Parse TradingView plain-text alert body into an AlertPayload dict.

    Supported formats:
      Action:  "BYBIT:HYPEUSDC.P LDC Open Short | HYPEUSDC.P@68.3 | (240)"
      Signal:  "SSE:600030 Jingda 1 | 600030@29.50 | (D)"
               "SSE:600030 Jingda -1 | 600030@29.50 | (D)"
    """
    text = text.strip()

    # pair — first token containing a colon
    pair_m = re.match(r'^([A-Z]+:[A-Z0-9.]+)', text)
    if not pair_m:
        return None
    pair = pair_m.group(1)

    # price — after @
    price = None
    price_m = re.search(r'@([\d.]+)', text)
    if price_m:
        price = float(price_m.group(1))

    # timeframe — inside ( )
    timeframe = None
    tf_m = re.search(r'\((\d+|[DWM])\)', text)
    if tf_m:
        timeframe = tf_m.group(1)

    # signal integer (e.g. "Jingda 1" or "Jingda -1")
    sig_m = re.search(r'\b(-?1)\b', text)

    # action keywords
    text_l = text.lower()
    if "open long" in text_l:
        return {"pair": pair, "action": "open_long", "price": price}
    if "open short" in text_l:
        return {"pair": pair, "action": "open_short", "price": price}
    if "close and flip" in text_l or "close_and_flip" in text_l:
        direction = "short" if "short" in text_l else "long"
        return {"pair": pair, "action": "close_and_flip", "direction": direction, "price": price}
    if "close" in text_l:
        return {"pair": pair, "action": "close", "price": price}

    # signal flow (A-share queue / Jingda signal number)
    if sig_m:
        return {"pair": pair, "signal": int(sig_m.group(1)),
                "price": price, "timeframe": timeframe}

    return None


@app.post("/alert")
async def receive_alert(
    request:     Request,
    x_tv_secret: Optional[str] = Header(None),
    secret:      Optional[str] = Query(None),
):
    _verify(x_tv_secret, secret)
    body = await request.body()

    # Try JSON first, fall back to plain-text parser
    data = None
    try:
        data = json.loads(body)
    except Exception:
        pass

    if data is None:
        text = body.decode(errors="replace").strip()
        data = _parse_plain_text(text)
        if data is None:
            log.error(f"Unparseable alert body: {body!r}")
            raise HTTPException(400, f"Could not parse alert body: {text[:120]!r}")
        log.info(f"Plain-text alert parsed: {data}")
    else:
        log.info(f"JSON alert received: {data}")
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
        tp  = payload.tp
        sl  = payload.sl
        atr = payload.atr or 0

        # ── Smart flip logic ──────────────────────────────────────────────────
        existing = manager.get_trade(pair)
        if existing and not existing.closed and existing.direction != direction:
            # Opposing position exists in state — check if still live on broker
            live_dir = _broker_live_position(pair)
            if live_dir is not None:
                # Still live → close it first, then open new (flip)
                log.info(f"[TV→] 🔄 Flip {pair}: {existing.direction}→{direction}  "
                         f"broker confirms live, closing first")
                _broker_close(pair, price or 0)
                manager.close_trade(pair,
                                    reason=f"tv_alert_flip_to_{direction}@{price}",
                                    close_price=price or 0)
            else:
                # Not on broker — stopped out before flip signal arrived
                log.info(f"[TV→] ⚡ {pair} already stopped out before flip signal — "
                         f"closing state only, opening {direction}")
                manager.close_trade(pair,
                                    reason=f"stopped_out_before_flip@{price}",
                                    close_price=price or 0)
        # ─────────────────────────────────────────────────────────────────────

        _broker_open(pair, direction, price, payload.notional, tp=tp, sl=sl)
        manager.open_trade(pair, direction,
                           entry=price or 0, tp=tp, sl=sl, atr=atr,
                           size=payload.notional, features={})
        log.info(f"[TV→] ✅ Opened {direction.upper()} {pair}  tp={tp}  sl={sl}  atr={atr}")
        return {"ok": True, "action": action, "pair": pair, "direction": direction,
                "tp": tp, "sl": sl}

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
                           size=payload.notional, features={})
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
        log.info(f"China pending dir: {CHINA_PENDING_DIR}")
        uvicorn.run("watcher.tv_alert_server:app", host="0.0.0.0", port=port, reload=False)
