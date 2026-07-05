"""
t0_assistant.py — Main entry point. Strings all T+0 modules together.

Usage:
    # Demo with mock data (no real orders):
    python3 -m t0.t0_assistant --symbol SSE:600036 --shares 1000 --mode normal --mock

    # Live trading (miniQMT bridge):
    python3 -m t0.t0_assistant --symbol SSE:600036 --shares 1000 --mode normal

    # Write a regime change from command line (simulates what TV webhook does):
    python3 -m t0.t0_assistant --write-regime SSE:600036 --mode aggressive
"""
from __future__ import annotations
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from t0.position_ledger import PositionLedger
from t0.data_feed import MockTickFeed, MarketState, Tick
from t0.state_machine import T0StateMachine, RegimeConfig, Signal
from t0.regime_bridge import write_regime, read_regime
from t0.executor import MockExecutor, BrokerAPIExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

REGIME_POLL_INTERVAL = 30  # seconds between regime file checks


def run(symbol: str, initial_shares: int, mode: str, mock: bool,
        tick_interval: float = 1.0, start_price: float = 10.0) -> None:

    log.info("=" * 60)
    log.info(f"  T+0 Assistant  |  {symbol}  shares={initial_shares}  mode={mode}  mock={mock}")
    log.info("=" * 60)

    # Bootstrap
    ledger   = PositionLedger(symbol=symbol, old_shares=initial_shares, avg_cost=start_price)
    config   = RegimeConfig()
    config.apply_mode(mode)
    machine  = T0StateMachine(ledger=ledger, config=config)
    executor = MockExecutor() if mock else BrokerAPIExecutor()

    # Seed regime file
    write_regime(symbol, t_mode=mode)

    last_regime_check = 0.0

    def on_tick(tick: Tick, mkt: MarketState) -> None:
        nonlocal last_regime_check

        # Poll regime file every REGIME_POLL_INTERVAL seconds
        now = time.time()
        if now - last_regime_check > REGIME_POLL_INTERVAL:
            regime = read_regime(symbol)
            new_mode = regime.get("t_mode", "normal")
            if new_mode != config.t_mode:
                log.info(f"[regime] mode change: {config.t_mode} → {new_mode}")
                config.apply_mode(new_mode)
            last_regime_check = now

        signal: Signal = machine.on_tick(mkt)

        if signal.action == "sell":
            executor.sell(ledger, signal.qty, tick.price)

        elif signal.action == "buy":
            qty = ledger.buy_back_qty()
            executor.buy(ledger, qty, tick.price)

        elif signal.action == "resell":
            executor.resell(ledger, signal.qty, tick.price)

        elif signal.action != "hold":
            log.debug(f"[{symbol}] signal={signal.action}  reason={signal.reason}")

        # Status line every 10 ticks
        if int(now) % 10 == 0:
            log.info(f"  {mkt}  | {machine}")

    # Start feed
    feed = MockTickFeed(symbol=symbol, start_price=start_price,
                        tick_interval=tick_interval, volatility=0.003)
    feed.state.open_price = start_price
    feed.subscribe(on_tick)

    try:
        feed.start()
        log.info(f"Feed running — press Ctrl+C to stop")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        feed.stop()
        log.info(f"Stopped.  Final ledger: {ledger}")
        log.info(f"Completed T cycles: {machine._cycles}")


def main() -> None:
    parser = argparse.ArgumentParser(description="T+0 Same-Day Trading Assistant")
    parser.add_argument("--symbol",       default="SSE:600036", help="TV symbol, e.g. SSE:600036")
    parser.add_argument("--shares",       type=int, default=1000, help="Initial old_shares")
    parser.add_argument("--mode",         default="normal", choices=["aggressive","normal","conservative"])
    parser.add_argument("--mock",         action="store_true", help="Use MockExecutor (no real orders)")
    parser.add_argument("--start-price",  type=float, default=10.0, help="Mock starting price")
    parser.add_argument("--tick-interval",type=float, default=1.0, help="Seconds between mock ticks")

    # Utility: write regime from CLI (simulates TV webhook)
    parser.add_argument("--write-regime", metavar="SYMBOL", help="Write regime for symbol and exit")

    args = parser.parse_args()

    if args.write_regime:
        write_regime(args.write_regime, t_mode=args.mode)
        log.info(f"Regime written: {args.write_regime} → {args.mode}")
        return

    run(
        symbol=args.symbol,
        initial_shares=args.shares,
        mode=args.mode,
        mock=args.mock,
        start_price=args.start_price,
        tick_interval=args.tick_interval,
    )


if __name__ == "__main__":
    main()
