#!/usr/bin/env python3
from __future__ import annotations
"""
4H Signal Watcher — reads Jingda plot values from TradingView via MCP CLI.

Only scans symbols whose market is currently open (weekdays, within session hours).
Sleeps until the next 4H boundary OR the next market open, whichever is sooner.
"""

import atexit
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent.parent))
from watcher.mcp_processor import process_mcp_data

TV_MCP_DIR = Path(os.getenv("TV_MCP_DIR", "/Users/shazhou/tradingview-mcp"))
TV_CLI     = TV_MCP_DIR / "src/cli/index.js"
OUTPUT_DIR = Path("output")
STATE_FILE = OUTPUT_DIR / "watcher_state.json"
PID_FILE   = OUTPUT_DIR / "watcher.pid"

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
JINGDA_STUDY = "Jingda"   # substring match against Data Window study name

# Prefixes that trade 24/7 (crypto) or 24/5 weekdays (forex) — no hours check needed
_CRYPTO_PREFIXES_WL = {"BINANCE", "BYBIT", "COINBASE", "KRAKEN", "BITMEX", "BITSTAMP", "PIONEX", "BLOFIN"}
_FOREX_PREFIXES_WL  = {"FX", "OANDA", "FXCM", "FOREXCOM", "PEPPERSTONE"}

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler()],
)


# ── Market-hours helpers ──────────────────────────────────────────────────────

def _market_is_open(market_cfg: dict) -> bool:
    """Return True if the market is currently in its session (weekdays only)."""
    tz  = ZoneInfo(market_cfg["timezone"])
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False
    oh, om = map(int, market_cfg["open_time"].split(":"))
    ch, cm = map(int, market_cfg["close_time"].split(":"))
    open_dt  = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_dt = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return open_dt <= now < close_dt


def _seconds_until_next_open(market_cfg: dict) -> int:
    """Seconds until this market's next session open (skips weekends)."""
    tz  = ZoneInfo(market_cfg["timezone"])
    now = datetime.now(tz)
    oh, om = map(int, market_cfg["open_time"].split(":"))

    candidate = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)

    # Skip to Monday if candidate falls on weekend
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)

    return max(60, int((candidate - now).total_seconds()))


def _open_markets_for(symbols: list[str]) -> set[str]:
    """Return set of tv_prefixes whose markets are currently open."""
    from config.markets import PREFIX_MAP
    now_utc = datetime.now(ZoneInfo("UTC"))
    open_prefixes = set()
    for sym in symbols:
        prefix = sym.split(":")[0] if ":" in sym else None
        if not prefix:
            continue
        if prefix in _CRYPTO_PREFIXES_WL:
            open_prefixes.add(prefix)   # crypto: always open
        elif prefix in _FOREX_PREFIXES_WL:
            if now_utc.weekday() < 5:   # forex: weekdays only
                open_prefixes.add(prefix)
        elif prefix in PREFIX_MAP:
            if _market_is_open(PREFIX_MAP[prefix]):
                open_prefixes.add(prefix)
    return open_prefixes


def _seconds_until_any_open(symbols: list[str]) -> int:
    """Seconds until the soonest market open across all watched symbols."""
    from config.markets import PREFIX_MAP
    now_utc = datetime.now(ZoneInfo("UTC"))

    # If any crypto symbol is in the list, we're always open
    for sym in symbols:
        prefix = sym.split(":")[0] if ":" in sym else None
        if prefix in _CRYPTO_PREFIXES_WL:
            return 60

    # Forex open on weekdays — find next Monday if weekend
    for sym in symbols:
        prefix = sym.split(":")[0] if ":" in sym else None
        if prefix in _FOREX_PREFIXES_WL and now_utc.weekday() >= 5:
            days_to_monday = (7 - now_utc.weekday()) % 7 or 7
            next_open = (now_utc + timedelta(days=days_to_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            return max(60, int((next_open - now_utc).total_seconds()))

    seen_prefixes: set[str] = set()
    min_secs = 24 * 3600
    for sym in symbols:
        prefix = sym.split(":")[0] if ":" in sym else None
        if prefix and prefix in PREFIX_MAP and prefix not in seen_prefixes:
            seen_prefixes.add(prefix)
            secs = _seconds_until_next_open(PREFIX_MAP[prefix])
            min_secs = min(min_secs, secs)
    return min_secs


# ── Per-market 4H period alignment ───────────────────────────────────────────
# Crypto:  UTC 4H boundaries (00,04,08,12,16,20 UTC)
# Forex:   Pacific time 4H boundaries (user confirmed correct)
# Stocks:  ET minus 30 min so session-open bars (9:30, 1:30 PM ET) land on
#          ET-hour boundaries (9:00, 13:00) and map cleanly to period IDs

_ET_TZ = ZoneInfo("America/New_York")


def _utc_now() -> datetime:
    return datetime.now(ZoneInfo("UTC"))


def _pacific_now() -> datetime:
    return datetime.now(PACIFIC_TZ)


def _period_id(symbol: str = "") -> str:
    """Dedup key: one scan per 4H bar per symbol, aligned to each market's bar close."""
    prefix = symbol.split(":")[0].upper() if ":" in symbol else ""
    if prefix in _CRYPTO_PREFIXES_WL:
        dt = _utc_now()
        return f"UTC-{dt.date()}-{dt.hour // 4}"
    elif prefix in _FOREX_PREFIXES_WL:
        dt = _pacific_now()
        return f"PT-{dt.date()}-{dt.hour // 4}"
    else:
        # Stocks: shift ET back 30 min so 9:30→9:00, 1:30PM→1:00PM, etc.
        dt = datetime.now(_ET_TZ) - timedelta(minutes=30)
        return f"ET30-{dt.date()}-{dt.hour // 4}"


def _seconds_until_next_4h() -> int:
    # Sleep on UTC boundaries — crypto is always open and drives the scan cadence
    now = _utc_now()
    seconds_into_block = (now.hour % 4) * 3600 + now.minute * 60 + now.second
    return (4 * 3600 - seconds_into_block) + 60


# ── TradingView MCP CLI helpers ───────────────────────────────────────────────

def _tv_cli(*args, timeout: int = 30) -> dict:
    result = subprocess.run(
        [os.environ.get("NODE_BIN", "node"), str(TV_CLI), *args],
        cwd=str(TV_MCP_DIR),
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"CLI error: {' '.join(args)}")
    return json.loads(result.stdout)


def _read_indicator_data(tv_symbol: str, retries: int = 3) -> dict | None:
    """
    Read all Jingda plot values for tv_symbol from TradingView Data Window.
    Returns flat dict suitable for process_mcp_data(), or None on failure.
    """
    _tv_cli("symbol", tv_symbol)
    for attempt in range(retries):
        time.sleep(6 + attempt * 4)
        try:
            tf_result = _tv_cli("timeframe", "240")
            if not tf_result.get("chart_ready"):
                log.debug(f"{tv_symbol}: chart not ready (attempt {attempt+1})")
                continue

            val_result = _tv_cli("values")
            studies = val_result.get("studies", [])
            if not studies:
                log.debug(f"{tv_symbol}: no studies (attempt {attempt+1})")
                continue

            jingda = next(
                (s for s in studies if JINGDA_STUDY.lower() in s["name"].lower()),
                None,
            )
            if jingda is None:
                names = [s["name"] for s in studies]
                log.debug(f"{tv_symbol}: Jingda study not found — studies: {names}")
                continue

            raw = dict(jingda["values"])

            try:
                quote = _tv_cli("quote")
                raw["close"] = quote.get("close") or quote.get("last")
            except Exception:
                pass

            raw["pair"]     = tv_symbol
            raw["bar_time"] = datetime.now().isoformat(timespec="seconds")
            return raw

        except Exception as e:
            log.warning(f"{tv_symbol}: data read error (attempt {attempt+1}): {e}")
    return None


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Watchlist loader ──────────────────────────────────────────────────────────

def _load_watchlist() -> list[str]:
    wl_file = OUTPUT_DIR / "watchlist_state.json"
    if wl_file.exists():
        data = json.loads(wl_file.read_text())
        return data.get("symbols", [])
    import glob as g
    import pandas as pd
    files = sorted(g.glob(str(OUTPUT_DIR / "results_*_*.csv")))
    if not files:
        return []
    frames = [pd.read_csv(f) for f in files]
    df = pd.concat(frames, ignore_index=True).drop_duplicates("tv_symbol")
    return df["tv_symbol"].dropna().tolist()


# ── Per-symbol check ──────────────────────────────────────────────────────────

def _check_symbol(tv_symbol: str, state: dict) -> dict:
    s = state.get(tv_symbol, {})

    raw = _read_indicator_data(tv_symbol)
    if raw is None:
        log.warning(f"{tv_symbol}: could not read indicator data — skipping")
        return s

    try:
        process_mcp_data(raw)
    except Exception as e:
        log.error(f"{tv_symbol}: mcp_processor error — {e}")

    s["last_period"] = _period_id(tv_symbol)
    return s


# ── Main loop ─────────────────────────────────────────────────────────────────

def _acquire_singleton() -> None:
    """Exit if another watcher instance is already running; otherwise write PID file."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text().strip())
            os.kill(existing_pid, 0)   # raises OSError if process doesn't exist
            log.error(
                f"Another watcher is already running (PID {existing_pid}). "
                f"Kill it first or delete {PID_FILE}. Exiting."
            )
            sys.exit(1)
        except OSError:
            log.warning(f"Stale PID file found (PID {PID_FILE.read_text().strip()} no longer running) — removing.")
            PID_FILE.unlink(missing_ok=True)

    PID_FILE.write_text(str(os.getpid()))
    atexit.register(PID_FILE.unlink, missing_ok=True)


def run_watcher():
    _acquire_singleton()
    OUTPUT_DIR.mkdir(exist_ok=True)
    log.info("=" * 60)
    log.info(f"  Stock Watcher started  |  {datetime.now():%Y-%m-%d %H:%M}  PID {os.getpid()}")
    log.info("  Scans only during market session hours")
    log.info("=" * 60)

    state = _load_state()

    while True:
        symbols = _load_watchlist()

        if not symbols:
            log.warning("Watchlist empty — run screener first.")
            time.sleep(3600)
            continue

        open_prefixes = _open_markets_for(symbols)
        active = [s for s in symbols if s.split(":")[0] in open_prefixes]

        if not active:
            secs = _seconds_until_any_open(symbols)
            mins = secs // 60
            log.info(f"All markets closed. Next open in {mins} min — sleeping.")
            time.sleep(secs)
            continue

        log.info(f"Open markets: {', '.join(sorted(open_prefixes))}")
        log.info(f"Checking {len(active)}/{len(symbols)} symbols")

        for tv_symbol in active:
            sym_state   = state.get(tv_symbol, {})
            last_period = sym_state.get("last_period")
            if last_period is not None and last_period == _period_id(tv_symbol):
                log.debug(f"{tv_symbol}: already checked this period — skipping")
                continue
            try:
                state[tv_symbol] = _check_symbol(tv_symbol, state)
                _save_state(state)
            except Exception as e:
                log.error(f"{tv_symbol}: unexpected error — {e}")

        secs = _seconds_until_next_4h()
        mins = secs // 60
        log.info(f"Next check in {mins} min  ({_pacific_now().strftime('%H:%M PT')} now)")
        time.sleep(secs)


if __name__ == "__main__":
    run_watcher()
