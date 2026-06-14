#!/usr/bin/env python3
from __future__ import annotations
"""
Hyperliquid broker integration — crypto execution.
TODO: Implement using Hyperliquid Python SDK.
"""

import logging

log = logging.getLogger(__name__)


def execute_trade(tv_symbol: str, direction: str, price: float | None = None) -> None:
    """Open or close a crypto position via Hyperliquid. TODO: implement."""
    log.info(f"[HYPERLIQUID-STUB] {tv_symbol} {direction.upper()} @ {price}")
    # TODO: import hyperliquid SDK and submit order
    # from hyperliquid.exchange import Exchange
    # ...
