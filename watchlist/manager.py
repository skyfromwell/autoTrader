#!/usr/bin/env python3
from __future__ import annotations
"""
TradingView Watchlist Manager.

Merges screener outputs across markets, deduplicates, caps at WATCHLIST_MAX,
writes a combined TV-format import file, and maintains a state JSON so only
NEW symbols are flagged on each run.

TradingView import (manual): Watchlist panel → ⋮ → Import list → select the .txt file.
"""

import json
import glob
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

OUTPUT_DIR    = Path("output")
STATE_FILE    = OUTPUT_DIR / "watchlist_state.json"
WATCHLIST_MAX = int(os.getenv("WATCHLIST_MAX", "30"))
TV_MCP_DIR    = Path(os.getenv("TV_MCP_DIR", "/Users/shazhou/tradingview-mcp"))
TV_CLI        = TV_MCP_DIR / "src/cli/index.js"

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s %(message)s")


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"symbols": [], "updated_at": None}


def _save_state(symbols: list[str]) -> None:
    STATE_FILE.write_text(json.dumps({
        "symbols":    symbols,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))


# ── Screener result loader ────────────────────────────────────────────────────

def _latest_results(market_short: str | None = None) -> pd.DataFrame:
    """Load and merge the most recent screener CSV for each market (or one market)."""
    pattern = f"results_{market_short.lower()}_*.csv" if market_short else "results_*_*.csv"
    all_files = sorted((OUTPUT_DIR).glob(pattern))

    if not all_files:
        log.warning(f"No screener results found matching {pattern}")
        return pd.DataFrame()

    # Keep only the latest file per market key
    latest: dict[str, Path] = {}
    for f in all_files:
        # filename: results_<market>_<ts>.csv  →  market = part between first _ and second _
        parts = f.stem.split("_")
        if len(parts) >= 3:
            mkt = parts[1]
            latest[mkt] = f          # sorted → last = newest

    frames = [pd.read_csv(p) for p in latest.values()]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ── TradingView push ──────────────────────────────────────────────────────────

def _tv_get_watchlist() -> set[str]:
    """Return set of symbols currently in the TradingView watchlist."""
    try:
        result = subprocess.run(
            ["node", str(TV_CLI), "watchlist", "get"],
            cwd=str(TV_MCP_DIR), capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout)
        return {s["symbol"] for s in data.get("symbols", [])}
    except Exception as e:
        log.warning(f"Could not read TV watchlist: {e}")
        return set()


def _tv_add_symbol(symbol: str) -> bool:
    """Add one symbol to the TradingView watchlist via MCP CLI."""
    try:
        result = subprocess.run(
            ["node", str(TV_CLI), "watchlist", "add", symbol],
            cwd=str(TV_MCP_DIR), capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout)
        if data.get("success"):
            log.info(f"  ✅ Added to TV watchlist: {symbol}")
            return True
        log.warning(f"  ⚠️  TV add failed for {symbol}: {data}")
        return False
    except Exception as e:
        log.warning(f"  TV add error for {symbol}: {e}")
        return False


def push_to_tradingview(new_symbols: list[str]) -> list[str]:
    """Push new symbols to TradingView watchlist, skipping existing ones."""
    if not new_symbols:
        return []
    existing_tv = _tv_get_watchlist()
    added = []
    for sym in new_symbols:
        if sym in existing_tv:
            log.info(f"  Already in TV watchlist: {sym}")
            continue
        if _tv_add_symbol(sym):
            added.append(sym)
    return added


# ── Public entry point ────────────────────────────────────────────────────────

def update_watchlist(market_short: str | None = None) -> tuple[list[str], list[str]]:
    """
    Merge latest screener results, enforce WATCHLIST_MAX, update state.

    Returns (all_symbols, new_symbols) where new_symbols = added since last run.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    state       = _load_state()
    existing    = set(state["symbols"])

    df = _latest_results(market_short)
    if df.empty:
        log.warning("No screener data — run screener first.")
        return list(existing), []

    # Sort by RS then momentum so best stocks fill the cap first
    sort_col = next(
        (c for c in df.columns if c.startswith("rs_vs_benchmark")), None
    ) or next(
        (c for c in df.columns if c.startswith("return_")), None
    )
    if sort_col and sort_col in df.columns:
        df = df.sort_values(sort_col, ascending=False)

    screener_symbols = df["tv_symbol"].dropna().tolist()

    # Merge: keep existing that are still in screener, then add new up to cap
    retained = [s for s in state["symbols"] if s in set(screener_symbols)]
    new_pool = [s for s in screener_symbols if s not in set(retained)]
    slots    = max(0, WATCHLIST_MAX - len(retained))
    to_add   = new_pool[:slots]
    final    = retained + to_add

    new_symbols = [s for s in final if s not in existing]

    _save_state(final)

    # Write TV import file
    ts      = datetime.now().strftime("%Y%m%d_%H%M")
    tv_path = OUTPUT_DIR / f"tv_import_{ts}.txt"
    tv_path.write_text("\n".join(final) + "\n")

    log.info(f"Watchlist: {len(final)} symbols ({len(new_symbols)} new, cap={WATCHLIST_MAX})")
    log.info(f"TV import file → {tv_path}")

    if new_symbols:
        log.info(f"New symbols: {', '.join(new_symbols)}")
        log.info("Pushing new symbols to TradingView...")
        added = push_to_tradingview(new_symbols)
        log.info(f"Added to TradingView: {len(added)} symbol(s)")

    return final, new_symbols


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", default=None, help="Filter to one market (e.g. lse)")
    args = parser.parse_args()
    update_watchlist(args.market)
