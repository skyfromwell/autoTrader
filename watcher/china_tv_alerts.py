#!/usr/bin/env python3
"""
China A-Share TV Alert Handler — multi-timeframe webhook for SSE + SZSE stocks.

Run:
    python -m watcher.china_tv_alerts

Expose via Cloudflare tunnel (separate from main tv_alert_server):
    cloudflared tunnel --url http://localhost:9998

TradingView alert message (signal):
    {"pair": "{{ticker}}", "timeframe": "{{interval}}", "signal": {{Signal_Stream}}, "price": {{close}}}

TradingView alert message (system action):
    {"pair": "{{ticker}}", "action": "move_sl", "value": 7.50, "price": {{close}}}

Supported timeframes: 30, 60, 240, D
Signal behaviour (SSE/SZSE only):
  signal=1   + no open position  → buy (market order, default ¥50,000 notional)
  signal=1   + position exists   → log "already long, skip"
  signal=-1  + open position     → WARNING (cannot short A-shares)
  signal=-1  + no position       → ignore
System actions (any market): move_sl, close, open_long, open_short, partial_close
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
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

VALID_TIMEFRAMES = {"30", "60", "240", "D"}
CHINA_PREFIXES   = {"SSE", "SZSE"}

app     = FastAPI(title="China TV Alert Handler")
manager = PositionManager()


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
    """Return {symbol: position_dict} from miniQMT, empty on error."""
    try:
        positions = _china_get("/positions")
        return {p["symbol"]: p for p in positions}
    except Exception as e:
        log.warning(f"Could not fetch China positions: {e}")
        return {}


def _open_china_position(tv_pair: str, price: float, notional: int = DEFAULT_NOTIONAL) -> dict:
    qmt_sym = _tv_to_qmt(tv_pair)
    if price <= 0:
        raise ValueError(f"Invalid price {price} for {tv_pair}")
    volume = int((notional // price) // 100) * 100
    if volume <= 0:
        raise ValueError(f"Calculated 0 shares for {tv_pair} at ¥{price} notional=¥{notional}")
    result = _china_post("/order", {
        "symbol":     qmt_sym,
        "direction":  "buy",
        "volume":     volume,
        "price":      0,
        "order_type": "market",
    })
    log.info(f"[China] BUY {qmt_sym}  {volume} shares @ mkt  order_id={result.get('order_id')}  notional≈¥{volume*price:.0f}")
    return {"symbol": qmt_sym, "volume": volume, "order_id": result.get("order_id"), "approx_notional": round(volume * price)}


def _close_china_position(tv_pair: str, volume: int) -> dict:
    qmt_sym = _tv_to_qmt(tv_pair)
    result = _china_post("/order", {
        "symbol":     qmt_sym,
        "direction":  "sell",
        "volume":     volume,
        "price":      0,
        "order_type": "market",
    })
    log.info(f"[China] SELL {qmt_sym}  {volume} shares @ mkt  order_id={result.get('order_id')}")
    return result


# ── Payload ───────────────────────────────────────────────────────────────────

class AlertPayload(BaseModel):
    pair:       str
    timeframe:  Optional[str]  = None   # "30" | "60" | "240" | "D"
    signal:     Optional[int]  = None   # 1=long, -1=short (from Jingda Signal Stream)
    action:     Optional[str]  = None   # move_sl | close | open_long | open_short | partial_close
    value:      Optional[float] = None  # new SL price (for move_sl)
    fraction:   float          = 2/3    # for partial_close
    price:      Optional[float] = None  # bar close price
    notional:   Optional[int]  = None   # override default notional (CNY)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "china_url": CHINA_URL, "default_notional_cny": DEFAULT_NOTIONAL}


@app.post("/alert")
async def receive_alert(
    payload:      AlertPayload,
    x_tv_secret:  Optional[str] = Header(None),
    secret:       Optional[str] = Query(None),
):
    _verify(x_tv_secret, secret)

    pair      = payload.pair
    prefix    = pair.split(":")[0].upper() if ":" in pair else ""
    price     = payload.price or 0.0
    notional  = payload.notional or DEFAULT_NOTIONAL
    is_china  = prefix in CHINA_PREFIXES

    log.info(f"[TV→] {pair}  tf={payload.timeframe}  signal={payload.signal}  action={payload.action}  price={price}")

    # ── System action alerts (any market) ─────────────────────────────────────
    if payload.action:
        action = payload.action.lower().replace("-", "_")
        return await _handle_action(pair, action, payload, price, notional, is_china)

    # ── Signal alerts ─────────────────────────────────────────────────────────
    if payload.signal is None:
        raise HTTPException(400, "Provide either 'action' or 'signal'")

    tf = str(payload.timeframe or "")
    if tf not in VALID_TIMEFRAMES:
        raise HTTPException(400, f"timeframe '{tf}' not in {VALID_TIMEFRAMES}")

    if not is_china:
        log.info(f"[TV→] {pair} signal ignored — not SSE/SZSE (use main tv_alert_server for other markets)")
        return {"ok": True, "skipped": True, "reason": "non_china_signal"}

    signal = payload.signal
    trade  = manager.get_trade(pair)
    has_position = trade is not None and not trade.closed

    if signal == 1:
        if has_position:
            log.info(f"[TV→] {pair} ({tf}) LONG signal — already long, skip")
            return {"ok": True, "skipped": True, "reason": "already_long"}

        # Open new long
        try:
            result = _open_china_position(pair, price, notional)
        except Exception as e:
            log.error(f"[TV→] {pair} open failed: {e}")
            raise HTTPException(500, str(e))

        if result.get("order_id", -1) == -1:
            log.error(f"[TV→] {pair} order rejected (order_id=-1)")
            raise HTTPException(500, "miniQMT rejected order (order_id=-1)")

        manager.open_trade(pair=pair, direction="long", entry=price,
                           tp=None, sl=None, atr=0, size=1.0, features={})
        log.info(f"[TV→] ✅ NEW LONG {pair} ({tf})  vol={result['volume']}  order={result['order_id']}")
        return {"ok": True, "action": "opened_long", "pair": pair, "timeframe": tf, **result}

    elif signal == -1:
        if has_position:
            log.warning(f"[TV→] ⚠️  SHORT signal on {pair} ({tf}) but we are LONG — A-shares cannot short! Consider closing manually.")
            return {"ok": True, "warning": True, "reason": "short_signal_while_long",
                    "message": f"SHORT signal on {pair} ({tf}) — A-shares no shorting. Close manually if needed."}
        else:
            log.info(f"[TV→] {pair} ({tf}) SHORT signal — no position, ignoring")
            return {"ok": True, "skipped": True, "reason": "short_no_position"}

    raise HTTPException(400, f"Unexpected signal value: {signal}")


async def _handle_action(pair: str, action: str, payload: AlertPayload,
                          price: float, notional: int, is_china: bool) -> dict:
    """Handle system action alerts (move_sl, close, open_long, open_short, partial_close)."""

    if action == "move_sl":
        if payload.value is None:
            raise HTTPException(400, "move_sl requires 'value'")
        trade = manager.get_trade(pair)
        if not trade:
            raise HTTPException(404, f"No open position for {pair}")
        manager.move_sl(pair, payload.value, reason=f"tv_alert_move_sl@{price}")
        log.info(f"[TV→] ✅ move_sl {pair} → {payload.value}")
        return {"ok": True, "action": action, "pair": pair, "new_sl": payload.value}

    if action == "close":
        if is_china:
            trade = manager.get_trade(pair)
            vol   = getattr(trade, "volume", 0) if trade else 0
            if not vol:
                # Try live positions
                pos_map = _qmt_positions()
                qmt_sym = _tv_to_qmt(pair)
                vol = pos_map.get(qmt_sym, {}).get("volume", 0)
            if vol:
                _close_china_position(pair, vol)
            manager.close_trade(pair, reason="tv_alert_close", close_price=price)
        else:
            from trader.oanda_trader import close_position as oanda_close
            from trader.hyperliquid_trader import close_position as hl_close
            p = pair.split(":")[0].upper()
            if p in {"FX", "OANDA", "FXCM", "FOREXCOM"}:
                oanda_close(pair)
            else:
                hl_close(pair)
            manager.close_trade(pair, reason="tv_alert_close", close_price=price)
        log.info(f"[TV→] ✅ close {pair}")
        return {"ok": True, "action": action, "pair": pair}

    if action in ("open_long", "open_short"):
        direction = "long" if action == "open_long" else "short"
        if is_china and direction == "short":
            raise HTTPException(400, "Cannot short A-shares")
        if is_china:
            result = _open_china_position(pair, price, notional)
            manager.open_trade(pair=pair, direction="long", entry=price,
                               tp=None, sl=None, atr=0, size=1.0, features={})
            return {"ok": True, "action": action, "pair": pair, **result}
        from trader.oanda_trader import execute_trade as oanda_execute
        oanda_execute(pair, direction, price=price, notional=notional)
        manager.open_trade(pair=pair, direction=direction, entry=price,
                           tp=None, sl=None, atr=0, size=1.0, features={})
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


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("CHINA_ALERT_PORT", "9998"))
    log.info(f"China TV Alert Handler starting on port {port}")
    log.info(f"Valid timeframes: {VALID_TIMEFRAMES}")
    log.info(f"Default notional: ¥{DEFAULT_NOTIONAL:,}")
    uvicorn.run("watcher.china_tv_alerts:app", host="0.0.0.0", port=port, reload=False)
