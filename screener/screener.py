#!/usr/bin/env python3
from __future__ import annotations
"""
Stock Screener V2.0 — 选股体系全景图
Two-stage process:
  Stage 1 (this file) → "What to Buy": quality + trend watchlist
  Stage 2 (Jingda AI) → "When to Buy": entry/exit timing

Steps 2-5 of the framework:
  Step 2: Core Four hard filters (revenue growth, EPS growth, rev/employee, P/S)
  Step 3: Supplementary indicators (inform score)
  Step 4: Scoring model 0-100 → min 60 (B grade) to enter watchlist
  Step 5: Tier classification A/B/C (growth stage label on output)
"""

import yfinance as yf
import pandas as pd
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Screener defaults (V2.0) ──────────────────────────────────────────────────
DEFAULTS = {
    # ── Step 2: Core Four hard filters ───────────────────────────────────────
    "enable_fundamentals":    True,
    "min_market_cap":         500_000_000,   # liquidity floor
    "max_pe_ratio":           40,
    "min_eps_growth":         0,             # break-even ok; 0 = profitable
    "min_revenue_growth":     10,            # % YoY — core indicator ①
    "min_rev_per_employee":   300_000,       # USD — core indicator ③
    "max_ps_ratio":           25,            # P/S cap — core indicator ④

    # ── MA trend ──────────────────────────────────────────────────────────────
    "enable_ma":              True,
    "ma_short":               50,
    "ma_long":                200,

    # ── RSI ───────────────────────────────────────────────────────────────────
    "enable_rsi":             True,
    "rsi_period":             14,
    "rsi_min":                30,
    "rsi_max":                70,

    # ── Relative strength vs benchmark ────────────────────────────────────────
    "enable_rs":              True,
    "rs_period":              60,
    "rs_min_outperform":      0,

    # ── Step 4: Scoring model minimum ─────────────────────────────────────────
    "min_score":              60,            # B grade or above

    # ── Disabled (Jingda handles timing) ─────────────────────────────────────
    "enable_macd":            False,
    "enable_price_mom":       False,
    "price_mom_period":       20,
    "price_mom_min_pct":      5.0,
    "enable_volume":          False,
    "volume_lookback":        20,
    "volume_ratio_min":       1.3,
}

OUTPUT_DIR = Path("output")


def _setup_log(market_short: str) -> logging.Logger:
    OUTPUT_DIR.mkdir(exist_ok=True)
    name = f"screener.{market_short.lower()}"
    log  = logging.getLogger(name)
    if not log.handlers:
        log.setLevel(logging.INFO)
        log.propagate = False
        fmt = logging.Formatter("%(asctime)s  %(levelname)-8s %(message)s")
        fh  = logging.FileHandler(OUTPUT_DIR / f"screener_{market_short.lower()}.log")
        sh  = logging.StreamHandler()
        fh.setFormatter(fmt); sh.setFormatter(fmt)
        log.addHandler(fh); log.addHandler(sh)
    return log


def load_tickers(market_key: str) -> list[str]:
    path = Path("config/tickers") / f"{market_key.lower()}.txt"
    if not path.exists():
        raise FileNotFoundError(f"No ticker file at {path}.")
    return [
        l.strip() for l in path.read_text().splitlines()
        if l.strip() and not l.startswith("#")
    ]


def should_run_today(market_cfg: dict, hours_before: int = 2) -> bool:
    tz  = ZoneInfo(market_cfg["timezone"])
    now = datetime.now(tz)
    if now.weekday() >= 5:
        return False
    hh, mm       = map(int, market_cfg["open_time"].split(":"))
    open_dt      = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
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


# ── V2.0 Scoring Model ────────────────────────────────────────────────────────

def _score_stock(info: dict) -> tuple[float, str, str]:
    """
    Compute V2.0 score (0-100), letter grade (A/B/C/D), and tier (A/B/C).
    Dimensions: Growth 30% | Efficiency 20% | Profitability 20% |
                Valuation 15% | Financial Health 10% | Management 5%
    """
    def _pct(v) -> float:
        return float(v) * 100 if v is not None else 0.0

    def _val(v, default=0.0) -> float:
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    rev_growth  = _pct(info.get("revenueGrowth"))
    eps_growth  = _pct(info.get("earningsGrowth"))
    roe         = _pct(info.get("returnOnEquity"))
    gross_m     = _pct(info.get("grossMargins"))
    net_m       = _pct(info.get("profitMargins"))
    total_rev   = _val(info.get("totalRevenue"))
    employees   = _val(info.get("fullTimeEmployees"), 1)
    rev_per_emp = total_rev / employees if employees > 0 else 0
    fcf         = _val(info.get("freeCashflow"))
    de_ratio    = _val(info.get("debtToEquity"))
    ps          = _val(info.get("priceToSalesTrailingTwelveMonths"))
    peg         = _val(info.get("pegRatio"))
    fwd_pe      = _val(info.get("forwardPE"))
    mkt_cap     = _val(info.get("marketCap"))

    # ── Growth (30 pts) ───────────────────────────────────────────────────────
    def _growth_pts(g: float, max_pts: int) -> float:
        if g > 50: return max_pts
        if g > 30: return max_pts * 0.80
        if g > 20: return max_pts * 0.60
        if g > 10: return max_pts * 0.40
        if g >= 0: return max_pts * 0.20
        return 0
    g_score = _growth_pts(rev_growth, 15) + _growth_pts(eps_growth, 15)

    # ── Efficiency (20 pts) ───────────────────────────────────────────────────
    if   rev_per_emp > 1_000_000: rpe_pts = 10
    elif rev_per_emp >   500_000: rpe_pts = 8
    elif rev_per_emp >   300_000: rpe_pts = 6
    elif rev_per_emp >   150_000: rpe_pts = 3
    else:                         rpe_pts = 1

    if   roe > 30: roe_pts = 10
    elif roe > 20: roe_pts = 8
    elif roe > 15: roe_pts = 6
    elif roe > 10: roe_pts = 4
    elif roe >  0: roe_pts = 2
    else:          roe_pts = 0
    e_score = rpe_pts + roe_pts

    # ── Profitability (20 pts) ────────────────────────────────────────────────
    if   gross_m > 60: gm_pts = 7
    elif gross_m > 40: gm_pts = 5
    elif gross_m > 20: gm_pts = 3
    else:              gm_pts = 1

    if   net_m > 20: nm_pts = 7
    elif net_m > 10: nm_pts = 5
    elif net_m >  5: nm_pts = 3
    elif net_m >  0: nm_pts = 1
    else:            nm_pts = 0

    fcf_pts = 6 if fcf > 0 else 0
    p_score = gm_pts + nm_pts + fcf_pts

    # ── Valuation (15 pts) ────────────────────────────────────────────────────
    if   ps < 2:  ps_pts = 7
    elif ps < 10: ps_pts = 5
    elif ps < 25: ps_pts = 3
    else:         ps_pts = 0

    if peg > 0:
        if   peg < 1: peg_pts = 8
        elif peg < 2: peg_pts = 6
        else:         peg_pts = 2
    elif fwd_pe > 0:
        if   fwd_pe < 20: peg_pts = 5
        elif fwd_pe < 30: peg_pts = 3
        else:             peg_pts = 1
    else:
        peg_pts = 2  # unknown
    v_score = ps_pts + peg_pts

    # ── Financial Health (10 pts) ─────────────────────────────────────────────
    if   de_ratio < 50:  de_pts = 5   # yfinance returns D/E as percent (e.g. 50 = 0.5x)
    elif de_ratio < 100: de_pts = 3
    elif de_ratio < 200: de_pts = 1
    else:                de_pts = 0

    cash_pts = 5 if fcf > 0 else 2
    fh_score = de_pts + cash_pts

    # ── Management (5 pts) ────────────────────────────────────────────────────
    # Cannot be automated — assign neutral 3/5
    mgmt_score = 3

    total = g_score + e_score + p_score + v_score + fh_score + mgmt_score

    if   total >= 80: grade = "A"
    elif total >= 60: grade = "B"
    elif total >= 40: grade = "C"
    else:             grade = "D"

    # ── Tier classification (Step 5) ─────────────────────────────────────────
    # A = established leaders (large cap, steady growth)
    # B = growth accelerators (scaling, rising stars)
    # C = potential seeds (early stage, hyper-growth)
    if rev_growth > 50 or mkt_cap < 2_000_000_000:
        tier = "C"
    elif mkt_cap > 20_000_000_000 and rev_growth < 30:
        tier = "A"
    else:
        tier = "B"

    return round(total, 1), grade, tier


# ── Per-ticker screening ──────────────────────────────────────────────────────

def _screen_one(raw: str, market_cfg: dict, cfg: dict, bench_ret: float, log) -> dict | None:
    suffix    = market_cfg["suffix"]
    full      = f"{raw}{suffix}"
    tv_symbol = f"{market_cfg['tv_prefix']}:{raw}"
    out       = {"symbol": raw, "ticker": full, "tv_symbol": tv_symbol,
                 "market": market_cfg["short"]}

    try:
        info = yf.Ticker(full).info

        trading_days_needed = max(cfg["ma_long"], cfg["rs_period"]) + 20
        lookback = int(trading_days_needed * 1.45) + 30
        hist = yf.download(full,
                           start=datetime.now() - timedelta(days=lookback),
                           end=datetime.now(),
                           progress=False, auto_adjust=True)

        if hist.empty or len(hist) < cfg["ma_long"] + 5:
            log.debug(f"{full}: insufficient history ({len(hist)} bars)")
            return None

        close = hist["Close"].squeeze()
        vol   = hist["Volume"].squeeze()
        price = float(close.iloc[-1])

        # ── Step 2: Core Four hard filters ───────────────────────────────────
        if cfg["enable_fundamentals"]:
            mkt_cap = info.get("marketCap") or 0
            if mkt_cap < cfg["min_market_cap"]:
                log.debug(f"{full}: market cap {mkt_cap:,.0f} below min"); return None

            pe = info.get("trailingPE") or info.get("forwardPE")
            if cfg["max_pe_ratio"] and pe and pe > cfg["max_pe_ratio"]:
                log.debug(f"{full}: P/E {pe:.1f} > {cfg['max_pe_ratio']}"); return None

            eps_g = info.get("earningsGrowth")
            if cfg["min_eps_growth"] is not None and eps_g is not None:
                if eps_g * 100 < cfg["min_eps_growth"]:
                    log.debug(f"{full}: EPS growth {eps_g*100:.1f}% below min"); return None

            # ① Revenue growth
            rev_g = info.get("revenueGrowth")
            if rev_g is not None and rev_g * 100 < cfg["min_revenue_growth"]:
                log.debug(f"{full}: revenue growth {rev_g*100:.1f}% < {cfg['min_revenue_growth']}%")
                return None

            # ③ Revenue per employee
            total_rev = info.get("totalRevenue") or 0
            employees = info.get("fullTimeEmployees") or 0
            rev_per_emp = total_rev / employees if employees > 0 else 0
            if employees > 0 and rev_per_emp < cfg["min_rev_per_employee"]:
                log.debug(f"{full}: rev/employee ${rev_per_emp:,.0f} < ${cfg['min_rev_per_employee']:,.0f}")
                return None

            # ④ P/S ratio
            ps = info.get("priceToSalesTrailingTwelveMonths")
            if ps and ps > cfg["max_ps_ratio"]:
                log.debug(f"{full}: P/S {ps:.1f} > {cfg['max_ps_ratio']}"); return None

            out.update({
                "name":              info.get("longName", raw),
                "sector":            info.get("sector", ""),
                "market_cap_M":      round(mkt_cap / 1_000_000, 1),
                "pe_ratio":          round(pe, 2) if pe else None,
                "eps_growth_pct":    round(eps_g * 100, 1) if eps_g is not None else None,
                "rev_growth_pct":    round(rev_g * 100, 1) if rev_g is not None else None,
                "rev_per_emp_K":     round(rev_per_emp / 1_000, 1) if rev_per_emp else None,
                "ps_ratio":          round(ps, 2) if ps else None,
                "gross_margin_pct":  round((info.get("grossMargins") or 0) * 100, 1),
                "net_margin_pct":    round((info.get("profitMargins") or 0) * 100, 1),
                "roe_pct":           round((info.get("returnOnEquity") or 0) * 100, 1),
                "fcf_M":             round((info.get("freeCashflow") or 0) / 1_000_000, 1),
                "de_ratio":          round((info.get("debtToEquity") or 0) / 100, 2),
                "peg_ratio":         info.get("pegRatio"),
                "fwd_pe":            round(info.get("forwardPE"), 2) if info.get("forwardPE") else None,
            })

        # ── MA trend: price > MA50 > MA200 ───────────────────────────────────
        if cfg["enable_ma"]:
            ma_s = float(close.rolling(cfg["ma_short"]).mean().iloc[-1])
            ma_l = float(close.rolling(cfg["ma_long"]).mean().iloc[-1])
            if not (price > ma_s > ma_l):
                log.debug(f"{full}: MA not bullish (price={price:.2f} MA50={ma_s:.2f} MA200={ma_l:.2f})")
                return None
            out["ma_bullish"] = True

        # ── RSI ───────────────────────────────────────────────────────────────
        if cfg["enable_rsi"]:
            rsi = _calc_rsi(close, cfg["rsi_period"])
            if not (cfg["rsi_min"] <= rsi <= cfg["rsi_max"]):
                log.debug(f"{full}: RSI {rsi:.1f} outside {cfg['rsi_min']}-{cfg['rsi_max']}")
                return None
            out["rsi"] = round(rsi, 1)

        # ── Relative strength vs benchmark ────────────────────────────────────
        if cfg["enable_rs"]:
            if len(close) <= cfg["rs_period"]:
                return None
            stock_ret = (price / float(close.iloc[-(cfg["rs_period"] + 1)]) - 1) * 100
            rs_diff   = stock_ret - bench_ret
            if rs_diff < cfg["rs_min_outperform"]:
                log.debug(f"{full}: RS diff {rs_diff:.1f}% below {cfg['rs_min_outperform']}%")
                return None
            out[f"rs_vs_benchmark_{cfg['rs_period']}d"] = round(rs_diff, 1)

        # ── Step 4: Scoring model ─────────────────────────────────────────────
        score, grade, tier = _score_stock(info)
        if score < cfg["min_score"]:
            log.debug(f"{full}: score {score} < {cfg['min_score']} ({grade} grade)")
            return None

        out["score"]  = score
        out["grade"]  = grade
        out["tier"]   = tier
        out["price"]  = round(price, 4)

        log.info(f"✅  {full}  score={score}  grade={grade}  tier={tier}  "
                 f"rev_g={out.get('rev_growth_pct','?')}%  "
                 f"ps={out.get('ps_ratio','?')}")
        return out

    except Exception as e:
        log.error(f"{full}: {e}")
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def run_screener(market_cfg: dict, filter_overrides: dict | None = None) -> pd.DataFrame:
    cfg     = {**DEFAULTS, **(filter_overrides or {})}
    log     = _setup_log(market_cfg["short"])
    tickers = load_tickers(market_cfg["short"])

    log.info("=" * 60)
    log.info(f"  {market_cfg['name']}  V2.0  |  {datetime.now():%Y-%m-%d %H:%M}")
    log.info("=" * 60)

    bench_ret = (
        _fetch_benchmark(market_cfg["benchmark"], cfg["rs_period"], log)
        if cfg["enable_rs"] else 0.0
    )
    log.info(f"Benchmark {market_cfg['benchmark']} ({cfg['rs_period']}d): {bench_ret:+.1f}%")
    log.info(f"Universe: {len(tickers)} tickers")
    log.info(f"Filters: rev_growth≥{cfg['min_revenue_growth']}%  "
             f"rev/emp≥${cfg['min_rev_per_employee']//1000}K  "
             f"P/S≤{cfg['max_ps_ratio']}  score≥{cfg['min_score']}\n")

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

    df = pd.DataFrame(passed).sort_values("score", ascending=False)

    ts      = datetime.now().strftime("%Y%m%d_%H%M")
    mkt_key = market_cfg["short"].lower()
    csv_path = OUTPUT_DIR / f"results_{mkt_key}_{ts}.csv"
    tv_path  = OUTPUT_DIR / f"watchlist_{mkt_key}_{ts}.txt"

    df.to_csv(csv_path, index=False)
    tv_path.write_text("\n".join(r["tv_symbol"] for r in passed) + "\n")

    log.info(f"\n{'─'*60}")
    log.info(f"  {len(passed)}/{len(tickers)} passed  |  "
             f"Grades: " + "  ".join(
                 f"{g}={sum(1 for r in passed if r['grade']==g)}"
                 for g in ['A','B','C','D'] if any(r['grade']==g for r in passed)
             ))
    for r in passed:
        log.info(f"  [{r['grade']}/{r['tier']}] {r['ticker']:12s}  "
                 f"score={r['score']:5.1f}  "
                 f"rev_g={str(r.get('rev_growth_pct','?')):>6}%  "
                 f"P/S={str(r.get('ps_ratio','?')):>5}  "
                 f"{r.get('name','')}")
    log.info(f"  CSV       → {csv_path}")
    log.info(f"  Watchlist → {tv_path}")
    log.info(f"{'─'*60}\n")

    return df


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import argparse
    from config.markets import resolve_markets

    parser = argparse.ArgumentParser(description="Stock screener V2.0")
    parser.add_argument("--market", default="lse",
                        help="Market key or non-us/us/all (default: lse)")
    args = parser.parse_args()

    for m in resolve_markets(args.market):
        run_screener(m, m.get("filter_overrides"))
