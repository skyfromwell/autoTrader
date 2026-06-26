#!/usr/bin/env python3
"""
TV Alert Webhook Server — receives TradingView price alerts and executes broker actions.

Run locally:
    python -m watcher.tv_alert_server

Expose via Cloudflare tunnel:
    cloudflared tunnel --url http://localhost:9999

Set in TradingView alert → Webhook URL:
    https://<your-id>.trycloudflare.com/alert

TV alert message (JSON):
    {"pair": "BYBIT:HYPEUSDC.P", "action": "close_and_flip", "direction": "long"}
    {"pair": "BYBIT:HYPEUSDC.P", "action": "move_sl", "value": 68.0}
    {"pair": "OANDA:NZDUSD",     "action": "close"}
    {"pair": "NYSE:GS",          "action": "open_long"}

Supported actions: open_long, open_short, close, close_and_flip, move_sl
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(Path(__file__).parent.parent / ".env")

from trader.hyperliquid_trader import execute_trade as hl_execute
from trader.hyperliquid_trader import close_position as hl_close
from trader.oanda_trader        import execute_trade as oanda_execute
from trader.oanda_trader        import close_position as oanda_close
from trader.trader              import execute_trade as alpaca_execute
from trader.china_trader        import execute_trade as china_execute
from trader.china_trader        import close_position as china_close
from watcher.position_manager   import PositionManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

TV_SECRET        = os.getenv("TV_WEBHOOK_SECRET", "")
_NOTIONAL_DEFAULT = 10_000

_CRYPTO_PREFIXES  = {"BINANCE", "BYBIT", "COINBASE", "KRAKEN", "BITMEX", "PIONEX", "BLOFIN"}
_FOREX_PREFIXES   = {"FX", "OANDA", "FXCM", "FOREXCOM", "PEPPERSTONE"}
_CHINESE_PREFIXES = {"SSE", "SZSE", "HKEX", "SHSE"}

app     = FastAPI(title="TV Alert Webhook")
manager = PositionManager()


# ── Auth ──────────────────────────────────────────────────────────────────────

def _verify(secret: str | None, query_secret: str | None = None):
    if not TV_SECRET:
        log.warning("TV_WEBHOOK_SECRET not set — running unauthenticated!")
        return
    if secret == TV_SECRET or query_secret == TV_SECRET:
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


def _broker_close(pair: str) -> None:
    p = _prefix(pair)
    if p in _CRYPTO_PREFIXES:
        hl_close(pair)
    elif p in _FOREX_PREFIXES:
        oanda_close(pair)
    elif p in _CHINESE_PREFIXES:
        china_close(pair)
    else:
        log.warning(f"[{pair}] Alpaca close not wired — manual close needed")


# ── Payload ───────────────────────────────────────────────────────────────────

class AlertPayload(BaseModel):
    pair:      str
    action:    str                    # open_long | open_short | close | close_and_flip | move_sl
    direction: Optional[str] = None  # for close_and_flip: the new direction after close
    value:     Optional[float] = None  # for move_sl: new SL price
    price:     Optional[float] = None  # current price (use {{close}} in TV)
    notional:  int = _NOTIONAL_DEFAULT
    note:      Optional[str] = None  # free-text from TV alert for logging


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "authenticated": bool(TV_SECRET)}


@app.post("/alert")
async def receive_alert(
    payload: AlertPayload,
    x_tv_secret: Optional[str] = Header(None),
    secret: Optional[str] = None,
):
    _verify(x_tv_secret, secret)

    pair   = payload.pair
    action = payload.action.lower().replace("-", "_")
    price  = payload.price
    note   = payload.note or ""

    log.info(f"[TV→] {pair}  action={action}  price={price}  note={note}")

    # ── open_long / open_short ─────────────────────────────────────────────
    if action in ("open_long", "open_short"):
        direction = "long" if action == "open_long" else "short"
        _broker_open(pair, direction, price, payload.notional)
        manager.open_trade(pair, direction,
                           entry=price or 0, tp=None, sl=None, atr=0,
                           size=1.0, features={})
        log.info(f"[TV→] ✅ Opened {direction.upper()} {pair}")
        return {"ok": True, "action": action, "pair": pair, "direction": direction}

    # ── close ──────────────────────────────────────────────────────────────
    if action == "close":
        _broker_close(pair)
        manager.close_trade(pair, price=price or 0, reason="tv_alert_close")
        log.info(f"[TV→] ✅ Closed {pair}")
        return {"ok": True, "action": action, "pair": pair}

    # ── close_and_flip ─────────────────────────────────────────────────────
    if action == "close_and_flip":
        new_dir = (payload.direction or "long").lower()
        if new_dir not in ("long", "short"):
            raise HTTPException(400, f"direction must be 'long' or 'short', got '{new_dir}'")

        log.info(f"[TV→] Closing {pair} then flipping → {new_dir.upper()}")
        _broker_close(pair)
        manager.close_trade(pair, price=price or 0,
                            reason=f"tv_alert_flip_to_{new_dir}@{price}")

        _broker_open(pair, new_dir, price, payload.notional)
        manager.open_trade(pair, new_dir,
                           entry=price or 0, tp=None, sl=None, atr=0,
                           size=1.0, features={})
        log.info(f"[TV→] ✅ Flipped {pair} → {new_dir.upper()} at {price}")
        return {"ok": True, "action": action, "pair": pair, "new_direction": new_dir}

    # ── move_sl ────────────────────────────────────────────────────────────
    if action == "move_sl":
        if payload.value is None:
            raise HTTPException(400, "move_sl requires 'value' (new SL price)")
        new_sl = payload.value
        trade  = manager.get_trade(pair)
        if not trade:
            raise HTTPException(404, f"No open position for {pair}")

        manager.move_sl(pair, new_sl, reason=f"tv_alert_move_sl@{price}")

        # Also update broker-side SL order
        p = _prefix(pair)
        if p in _CRYPTO_PREFIXES:
            from trader.xyz_trader import move_sl as xyz_move_sl
            if pair.upper().startswith("XYZ:"):
                xyz_move_sl(pair.replace("XYZ:", "xyz:"), new_sl, trade.direction)
            else:
                log.info(f"[TV→] HL SL move for {pair} — JS cancel+replace needed")
        log.info(f"[TV→] ✅ Moved SL {pair} → {new_sl}")
        return {"ok": True, "action": action, "pair": pair, "new_sl": new_sl}

    raise HTTPException(400, f"Unknown action '{action}'. "
                        "Use: open_long, open_short, close, close_and_flip, move_sl")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("TV_WEBHOOK_PORT", "9999"))
    log.info(f"TV Alert Webhook Server starting on port {port}")
    log.info("Expose with: cloudflared tunnel --url http://localhost:{port}")
    uvicorn.run("watcher.tv_alert_server:app", host="0.0.0.0", port=port, reload=False)
