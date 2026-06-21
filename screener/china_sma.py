#!/usr/bin/env python3
from __future__ import annotations
"""
China A-share SMA10/21 Scanner — SSE + SZSE

Scans SSE and SZSE tickers for:
  - Bullish trend:   price > SMA10 > SMA21
  - Bearish trend:   price < SMA10 < SMA21
  - Recent crossover (last 3 bars): SMA10 crossed above/below SMA21

No fundamental filters — yfinance data for A-shares is unreliable.
Pure price-action experiment.
"""

import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf
import pandas as pd

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
)
log = logging.getLogger(__name__)

MARKETS = {
    "sse":  {"suffix": ".SS", "tv_prefix": "SSE",  "name": "Shanghai SE"},
    "szse": {"suffix": ".SZ", "tv_prefix": "SZSE", "name": "Shenzhen SE"},
}

SMA_SHORT = 10
SMA_LONG  = 21
LOOKBACK  = 60   # trading days


def load_tickers(market_key: str) -> list[str]:
    path = Path("config/tickers") / f"{market_key}.txt"
    return [l.strip() for l in path.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def scan_market(market_key: str) -> list[dict]:
    cfg     = MARKETS[market_key]
    tickers = load_tickers(market_key)
    results = []

    log.info(f"\n{'='*55}")
    log.info(f"  {cfg['name']}  SMA{SMA_SHORT}/{SMA_LONG}  |  {len(tickers)} tickers")
    log.info(f"{'='*55}")

    end   = datetime.now()
    start = end - timedelta(days=int(LOOKBACK * 1.6))

    for i, raw in enumerate(tickers, 1):
        ticker = f"{raw}{cfg['suffix']}"
        try:
            hist = yf.download(ticker, start=start, end=end,
                               progress=False, auto_adjust=True)
            if hist.empty or len(hist) < SMA_LONG + 3:
                log.debug(f"{ticker}: insufficient data ({len(hist)} bars)")
                continue

            close = hist["Close"].squeeze().dropna()
            vol   = hist["Volume"].squeeze().dropna()

            sma_s = close.rolling(SMA_SHORT).mean()
            sma_l = close.rolling(SMA_LONG).mean()

            price   = float(close.iloc[-1])
            s_now   = float(sma_s.iloc[-1])
            l_now   = float(sma_l.iloc[-1])
            s_prev  = float(sma_s.iloc[-2])
            l_prev  = float(sma_l.iloc[-2])

            # Trend state
            if price > s_now > l_now:
                trend = "BULL"
            elif price < s_now < l_now:
                trend = "BEAR"
            else:
                trend = "MIX"

            # Crossover in last 3 bars
            cross = None
            for k in range(1, 4):
                s_k  = float(sma_s.iloc[-k])
                l_k  = float(sma_l.iloc[-k])
                s_k1 = float(sma_s.iloc[-(k+1)])
                l_k1 = float(sma_l.iloc[-(k+1)])
                if s_k1 <= l_k1 and s_k > l_k:
                    cross = f"GOLDEN({k}d ago)"
                    break
                if s_k1 >= l_k1 and s_k < l_k:
                    cross = f"DEATH({k}d ago)"
                    break

            # Gap between SMAs as % of price (spread)
            spread_pct = (s_now - l_now) / price * 100

            # Volume ratio (last bar vs 10-day avg)
            vol_ratio = float(vol.iloc[-1]) / float(vol.rolling(10).mean().iloc[-1]) if len(vol) >= 10 else 1.0

            tv_sym = f"{cfg['tv_prefix']}:{raw}"
            rec = {
                "tv_symbol":  tv_sym,
                "ticker":     ticker,
                "market":     cfg["name"],
                "price":      round(price, 3),
                "sma10":      round(s_now, 3),
                "sma21":      round(l_now, 3),
                "spread_pct": round(spread_pct, 2),
                "trend":      trend,
                "crossover":  cross or "",
                "vol_ratio":  round(vol_ratio, 2),
            }
            results.append(rec)

            flag = ""
            if cross:         flag = f"  *** {cross} ***"
            elif trend == "BULL": flag = "  ↑ bull"
            elif trend == "BEAR": flag = "  ↓ bear"

            log.info(f"[{i:>2}/{len(tickers)}] {ticker:12s}  "
                     f"price={price:>9.3f}  SMA10={s_now:>9.3f}  SMA21={l_now:>9.3f}  "
                     f"spread={spread_pct:>+5.1f}%  trend={trend}{flag}")

        except Exception as e:
            log.warning(f"{ticker}: {e}")

        time.sleep(0.4)

    return results


def main():
    all_results = []
    for mkey in ["sse", "szse"]:
        all_results.extend(scan_market(mkey))

    if not all_results:
        log.info("No results.")
        return

    df = pd.DataFrame(all_results)

    # Sort: crossovers first, then bulls by spread, then bears
    cross_mask = df["crossover"] != ""
    bull_mask  = df["trend"] == "BULL"
    bear_mask  = df["trend"] == "BEAR"

    df_cross = df[cross_mask].sort_values("spread_pct", ascending=False)
    df_bull  = df[bull_mask & ~cross_mask].sort_values("spread_pct", ascending=False)
    df_bear  = df[bear_mask & ~cross_mask].sort_values("spread_pct")
    df_mix   = df[~cross_mask & ~bull_mask & ~bear_mask]

    df_sorted = pd.concat([df_cross, df_bull, df_bear, df_mix], ignore_index=True)

    ts  = datetime.now().strftime("%Y%m%d_%H%M")
    out = OUTPUT_DIR / f"china_sma_{ts}.csv"
    df_sorted.to_csv(out, index=False)

    print(f"\n{'='*65}")
    print(f"  China A-share SMA{SMA_SHORT}/{SMA_LONG} Scan  —  {datetime.now():%Y-%m-%d %H:%M}")
    print(f"{'='*65}")

    if cross_mask.any():
        print(f"\n🔔  CROSSOVERS ({cross_mask.sum()})")
        for _, r in df_cross.iterrows():
            print(f"  {r['tv_symbol']:16s}  {r['crossover']:20s}  "
                  f"price={r['price']:>9.3f}  spread={r['spread_pct']:>+5.1f}%  vol={r['vol_ratio']:.1f}x")

    if bull_mask.any():
        print(f"\n📈  BULLISH trend — price > SMA10 > SMA21  ({bull_mask.sum()})")
        for _, r in df[bull_mask & ~cross_mask].sort_values("spread_pct", ascending=False).iterrows():
            print(f"  {r['tv_symbol']:16s}  spread={r['spread_pct']:>+5.1f}%  "
                  f"price={r['price']:>9.3f}  SMA10={r['sma10']:>9.3f}  SMA21={r['sma21']:>9.3f}  vol={r['vol_ratio']:.1f}x")

    if bear_mask.any():
        print(f"\n📉  BEARISH trend — price < SMA10 < SMA21  ({bear_mask.sum()})")
        for _, r in df[bear_mask & ~cross_mask].sort_values("spread_pct").iterrows():
            print(f"  {r['tv_symbol']:16s}  spread={r['spread_pct']:>+5.1f}%  "
                  f"price={r['price']:>9.3f}  SMA10={r['sma10']:>9.3f}  SMA21={r['sma21']:>9.3f}  vol={r['vol_ratio']:.1f}x")

    mix_count = (~cross_mask & ~bull_mask & ~bear_mask).sum()
    if mix_count:
        print(f"\n〰️  MIXED / choppy  ({mix_count})")

    total = len(df)
    print(f"\n  Total scanned: {total}  |  "
          f"Bull: {bull_mask.sum()}  Bear: {bear_mask.sum()}  "
          f"Cross: {cross_mask.sum()}  Mix: {mix_count}")
    print(f"  CSV → {out}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    main()
