#!/usr/bin/env python3
from __future__ import annotations
"""
Oanda broker integration — forex execution.
TODO: Implement using Oanda REST API v20.
"""

import logging

log = logging.getLogger(__name__)


def execute_trade(tv_symbol: str, direction: str, price: float | None = None) -> None:
    """Open or close a forex position via Oanda. TODO: implement."""
    log.info(f"[OANDA-STUB] {tv_symbol} {direction.upper()} @ {price}")
    # TODO: import oandapyV20 and submit order
    # from oandapyV20 import API
    # from oandapyV20.endpoints.orders import OrderCreate
    # client = API(access_token=os.getenv("OANDA_API_KEY"), environment="practice")
    # ...
