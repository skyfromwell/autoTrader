#!/usr/bin/env python3
"""
autoTrader — Master Pipeline Runner

Usage:
  python run_pipeline.py                          # full pipeline (lse)
  python run_pipeline.py --market non-us          # screen all non-US markets
  python run_pipeline.py --market lse             # one specific market
  python run_pipeline.py --screener               # screener only
  python run_pipeline.py --watchlist              # update watchlist state only
  python run_pipeline.py --watch                  # run 4H watcher daemon
  python run_pipeline.py --screener --market lse  # composable flags
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="autoTrader pipeline")
    parser.add_argument(
        "--market", default="lse",
        help="Market to screen: lse, asx, tsx, hkex, nse, euronext, xetra, sse | non-us | us | all  (default: lse)",
    )
    parser.add_argument("--screener",  action="store_true", help="Run stock screener")
    parser.add_argument("--watchlist", action="store_true", help="Update TradingView watchlist state")
    parser.add_argument("--watch",     action="store_true", help="Run 4H signal watcher (daemon)")
    args = parser.parse_args()

    # Default: full pipeline (screener + watchlist, then watch)
    run_all = not any([args.screener, args.watchlist, args.watch])

    if args.screener or run_all:
        log.info("── STEP 1: Screener ──────────────────────────────────────")
        from config.markets import resolve_markets
        from screener.screener import run_screener
        for market_cfg in resolve_markets(args.market):
            run_screener(market_cfg)

    if args.watchlist or run_all:
        log.info("── STEP 2: Watchlist update ──────────────────────────────")
        from watchlist.manager import update_watchlist
        all_syms, new_syms = update_watchlist()
        log.info(f"Watchlist: {len(all_syms)} total, {len(new_syms)} new")

    if args.watch or run_all:
        log.info("── STEP 3: 4H Watcher (daemon) ───────────────────────────")
        from watcher.watcher import run_watcher
        run_watcher()


if __name__ == "__main__":
    main()
