#!/usr/bin/env python3
from __future__ import annotations
"""
Stock Screener — market-agnostic.
Filters: Fundamentals → MA Trend → RSI → MACD → Price Momentum → Volume → Relative Strength
Output:  output/results_<market>_<ts>.csv  +  output/watchlist_<market>_<ts>.txt
"""

import yfinance as yf
import pandas as pd
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Filter defaults (can be overridden per call) ──────────────────────────────
# Goal: quality + trend filter for watchlist building.
# Timing signals (MACD, volume surge, price momentum) belong in the signal
# generator (Jingda), not here.
DEFAULTS = {
    # Fundamentals — healthy company
    "enable_fundamentals":  True,
    "min_market_cap":       500_000_000,
    "max_pe_ratio":         40,
    "min_eps_growth":       0,          # 0 = profitable; None = skip

    # MA trend — stock in uptrend structure
    "enable_ma":            True,
    "ma_short":             50,
    "ma_long":              200,

    # RSI — not deeply oversold or already exhausted
    "enable_rsi":           True,
    "rsi_period":           14,
    "rsi_min":              30,
    "rsi_max":              70,

    # Timing filters — disabled (Jingda handles entry timing)
    "enable_macd":          False,
    "enable_price_mom":     False,
    "price_mom_period":     20,
    "price_mom_min_pct":    5.0,
    "enable_volume":        False,
    "volume_lookback":      20,
    "volume_ratio_min":     1.3,

    # Relative strength — at least keeping pace with market
    "enable_rs":            True,
    "rs_period":            60,
    "rs_min_outperform":    0,
}

OUTPUT_DIR = Path("output")


def _setup_log(market_short: str) -> logging.Logger:
    OUTPUT_DIR.mkdir(exist_ok=True)
    name = f"screener.{market_short.lower()}"
    log  = logging.getLogger(name)
    if not log.handlers:
        log.setLevel(logging.INFO)
        log.propagate = False   # prevent double-printing via root logger
        fmt = logging.Formatter("%(asctime)s  %(levelname)-8s %(message)s")
        fh  = logging.FileHandler(OUTPUT_DIR / f"screener_{market_short.lower()}.log")
        sh  = logging.StreamHandler()
        fh.setFormatter(fmt); sh.setFormatter(fmt)
        log.addHandler(fh); log.addHandler(sh)
    return log


def load_tickers(market_key: str) -> list[str]:
    path = Path("config/tickers") / f"{market_key.lower()}.txt"
    if not path.exists():
        raise FileNotFoundError(
            f"No ticker file at {path}. Create it with one ticker per line."
        )
    return [
        l.strip() for l in path.read_text().splitlines()
        if l.strip() and not l.startswith("#")
    ]


def should_run_today(market_cfg: dict, hours_before: int = 2) -> bool:
    """
    Return True if now is within the [open - hours_before, open] window on a weekday.
    Screener is designed to run ~2h before market open.
    """
    tz  = ZoneInfo(market_cfg["timezone"])
    now = datetime.now(tz)
    if now.weekday() >= 5:          # Saturday / Sunday
        return False
    hh, mm   = map(int, market_cfg["open_time"].split(":"))
    open_dt  = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    window_start = open_dt - timedelta(hours=hours_before)
    return window_start <= now < open_dt


# ── Technical helpers ─────────────────────────────────────────────────────────

def _fetch_benchmark(benchmark: str, period_days: int, log) -> float:
    try:
        end   = datetime.now()
        start = end - timedelta(days=period_days + 15)
        df    = yf.download(benchmark, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty or len(df) < 2:
            return 0.0
        closes = df["Close"].squeeze().dropna()
        return float((closes.iloc[-1] / closes.iloc[0] - 1) * 100)
    except Exception as e:
        log.warning(f"Benchmark {benchmark} failed: {e}")
        return 0.0


def _calc_rsi(series: pd.Series, period: int) -> float:
    delta    = series.diff().dropna()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, float("inf"))
    return float(100 - (100 / (1 + rs.iloc[-1])))


def _calc_macd(series: pd.Series) -> tuple[float, float]:
    ema12  = series.ewm(span=12, adjust=False).mean()
    ema26  = series.ewm(span=26, adjust=False).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return float(macd.iloc[-1]), float(signal.iloc[-1])


# ── Per-ticker screening ──────────────────────────────────────────────────────

def _screen_one(raw: str, market_cfg: dict, cfg: dict, bench_ret: float, log) -> dict | None:
    suffix     = market_cfg["suffix"]
    full       = f"{raw}{suffix}"
    tv_symbol  = f"{market_cfg['tv_prefix']}:{raw}"
    out        = {"symbol": raw, "ticker": full, "tv_symbol": tv_symbol,
                  "market": market_cfg["short"]}

    try:
        info     = yf.Ticker(full).info
        # Convert trading-day requirement to calendar days (~1.45× ratio, +30 holiday buffer)
        trading_days_needed = max(cfg["ma_long"], cfg["rs_period"]) + 20
        lookback = int(trading_days_needed * 1.45) + 30
        hist     = yf.download(full,
                               start=datetime.now() - timedelta(days=lookback),
                               end=datetime.now(),
                               progress=False, auto_adjust=True)

        if hist.empty or len(hist) < cfg["ma_long"] + 5:
            log.debug(f"{full}: insufficient history ({len(hist)} bars)")
            return None

        close = hist["Close"].squeeze()
        vol   = hist["Volume"].squeeze()
        price = float(close.iloc[-1])

        # Fundamentals
        if cfg["enable_fundamentals"]:
            mkt_cap = info.get("marketCap") or 0
            if mkt_cap < cfg["min_market_cap"]:
                log.debug(f"{full}: market cap {mkt_cap:,.0f} below min"); return None
            pe = info.get("trailingPE") or info.get("forwardPE")
            if cfg["max_pe_ratio"] and pe and pe > cfg["max_pe_ratio"]:
                log.debug(f"{full}: P/E {pe:.1f} > max"); return None
            eps_g = info.get("earningsGrowth")
            if cfg["min_eps_growth"] is not None and eps_g is not None:
                if eps_g * 100 < cfg["min_eps_growth"]:
                    log.debug(f"{full}: EPS growth below min"); return None
            out.update({
                "name":           info.get("longName", raw),
                "sector":         info.get("sector", ""),
                "market_cap_M":   round(mkt_cap / 1_000_000, 1),
                "pe_ratio":       round(pe, 2) if pe else None,
                "eps_growth_pct": round(eps_g * 100, 1) if eps_g is not None else None,
            })

        # MA trend: price > MA50 > MA200
        if cfg["enable_ma"]:
            ma_s = float(close.rolling(cfg["ma_short"]).mean().iloc[-1])
            ma_l = float(close.rolling(cfg["ma_long"]).mean().iloc[-1])
            if not (price > ma_s > ma_l):
                log.debug(f"{full}: MA not bullish"); return None
            out["ma_bullish"] = True

        # RSI
        if cfg["enable_rsi"]:
            rsi = _calc_rsi(close, cfg["rsi_period"])
            if not (cfg["rsi_min"] <= rsi <= cfg["rsi_max"]):
                log.debug(f"{full}: RSI {rsi:.1f} outside range"); return None
            out["rsi"] = round(rsi, 1)

        # MACD
        if cfg["enable_macd"]:
            macd_v, sig_v = _calc_macd(close)
            if macd_v < sig_v:
                log.debug(f"{full}: MACD bearish"); return None
            out["macd"] = round(macd_v, 4)
            out["macd_signal"] = round(sig_v, 4)

        # Price momentum
        if cfg["enable_price_mom"]:
            if len(close) <= cfg["price_mom_period"]:
                return None
            chg = (price / float(close.iloc[-(cfg["price_mom_period"] + 1)]) - 1) * 100
            if chg < cfg["price_mom_min_pct"]:
                log.debug(f"{full}: {cfg['price_mom_period']}d return {chg:.1f}% below min"); return None
            out[f"return_{cfg['price_mom_period']}d_pct"] = round(chg, 1)

        # Volume surge
        if cfg["enable_volume"]:
            if len(vol) < cfg["volume_lookback"] + 1:
                return None
            avg_vol = float(vol.iloc[-(cfg["volume_lookback"] + 1):-1].mean())
            ratio   = float(vol.iloc[-1]) / avg_vol if avg_vol else 0
            if ratio < cfg["volume_ratio_min"]:
                log.debug(f"{full}: volume ratio {ratio:.2f}× below min"); return None
            out["volume_ratio"] = round(ratio, 2)

        # Relative strength vs benchmark
        if cfg["enable_rs"]:
            if len(close) <= cfg["rs_period"]:
                return None
            stock_ret = (price / float(close.iloc[-(cfg["rs_period"] + 1)]) - 1) * 100
            rs_diff   = stock_ret - bench_ret
            if rs_diff < cfg["rs_min_outperform"]:
                log.debug(f"{full}: RS diff {rs_diff:.1f}% below min"); return None
            out[f"rs_vs_benchmark_{cfg['rs_period']}d"] = round(rs_diff, 1)

        out["price"] = round(price, 4)
        log.info(f"✅  {full} passed all filters")
        return out

    except Exception as e:
        log.error(f"{full}: {e}")
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def run_screener(market_cfg: dict, filter_overrides: dict | None = None) -> pd.DataFrame:
    """
    Screen all tickers for one market. Returns DataFrame of passing stocks.
    Saves output/results_<market>_<ts>.csv and output/watchlist_<market>_<ts>.txt.
    """
    cfg     = {**DEFAULTS, **(filter_overrides or {})}
    log     = _setup_log(market_cfg["short"])
    tickers = load_tickers(market_cfg["short"])

    log.info("=" * 60)
    log.info(f"  {market_cfg['name']}  |  {datetime.now():%Y-%m-%d %H:%M}")
    log.info("=" * 60)

    bench_ret = (
        _fetch_benchmark(market_cfg["benchmark"], cfg["rs_period"], log)
        if cfg["enable_rs"] else 0.0
    )
    log.info(f"Benchmark {market_cfg['benchmark']} ({cfg['rs_period']}d): {bench_ret:+.1f}%")
    log.info(f"Universe: {len(tickers)} tickers\n")

    passed = []
    for i, raw in enumerate(tickers, 1):
        log.info(f"[{i:>3}/{len(tickers)}] {raw}{market_cfg['suffix']}")
        result = _screen_one(raw, market_cfg, cfg, bench_ret, log)
        if result:
            passed.append(result)
        time.sleep(0.6)

    if not passed:
        log.info("⚠️  No tickers passed all filters.")
        return pd.DataFrame()

    df = pd.DataFrame(passed)

    # Sort by RS outperformance, fallback to price momentum
    sort_col = next(
        (c for c in df.columns if c.startswith("rs_vs_benchmark")), None
    ) or next(
        (c for c in df.columns if c.startswith("return_")), None
    )
    if sort_col:
        df = df.sort_values(sort_col, ascending=False)

    ts      = datetime.now().strftime("%Y%m%d_%H%M")
    mkt_key = market_cfg["short"].lower()
    csv_path = OUTPUT_DIR / f"results_{mkt_key}_{ts}.csv"
    tv_path  = OUTPUT_DIR / f"watchlist_{mkt_key}_{ts}.txt"

    df.to_csv(csv_path, index=False)
    tv_path.write_text("\n".join(r["tv_symbol"] for r in passed) + "\n")

    log.info(f"\n{'─'*60}")
    log.info(f"  {len(passed)}/{len(tickers)} passed")
    log.info(f"  CSV       → {csv_path}")
    log.info(f"  Watchlist → {tv_path}")
    log.info(f"{'─'*60}\n")

    return df


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import argparse
    from config.markets import resolve_markets

    parser = argparse.ArgumentParser(description="Stock screener")
    parser.add_argument("--market", default="lse",
                        help="Market key or non-us/us/all (default: lse)")
    args = parser.parse_args()

    for m in resolve_markets(args.market):
        run_screener(m, m.get("filter_overrides"))
