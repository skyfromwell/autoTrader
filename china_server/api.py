#!/usr/bin/env python3
"""
FastAPI wrapper around the miniQMT broker.
Run on the remote Windows machine:
    uvicorn api:app --host 0.0.0.0 --port 8888
"""
from __future__ import annotations
import logging
import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Path
from pydantic import BaseModel, Field

load_dotenv()
import broker

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

app     = FastAPI(title="autoTrader China Bridge", version="1.0")
API_KEY = os.getenv("CHINA_API_KEY", "")


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_key(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    if not API_KEY:
        raise RuntimeError("CHINA_API_KEY not set in .env")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Schemas ───────────────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    symbol:    str   = Field(..., example="600036.SH")
    direction: str   = Field(..., pattern="^(buy|sell)$")
    volume:    int   = Field(..., gt=0, multiple_of=100)
    price:     float = Field(0.0, ge=0, description="0 = market order")


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup() -> None:
    broker.connect()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/order", dependencies=[Depends(verify_key)])
def place_order(req: OrderRequest) -> dict:
    order_id = broker.place_order(req.symbol, req.direction, req.volume, req.price)
    return {"order_id": order_id, "symbol": req.symbol,
            "direction": req.direction, "volume": req.volume}


@app.delete("/order/{order_id}", dependencies=[Depends(verify_key)])
def cancel_order(order_id: int = Path(...)) -> dict:
    ok = broker.cancel_order(order_id)
    return {"order_id": order_id, "cancelled": ok}


@app.get("/positions", dependencies=[Depends(verify_key)])
def get_positions() -> list[dict]:
    return broker.get_positions()


@app.get("/account", dependencies=[Depends(verify_key)])
def get_account() -> dict:
    return broker.get_account()


@app.get("/orders", dependencies=[Depends(verify_key)])
def get_orders() -> list[dict]:
    return broker.get_orders()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
