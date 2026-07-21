#!/usr/bin/env python3
from __future__ import annotations
"""
US Stocks SMA10/21 Scanner — NASDAQ + NYSE
Same logic as china_sma_report.py: price vs SMA10 vs SMA21, crossovers in last 3 bars.
Generates PDF and sends to Telegram.
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yfinance as yf
import pandas as pd
from dotenv import load_dotenv
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm

load_dotenv(Path(__file__).parent.parent / ".env")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "2130465973")

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

SMA_SHORT = 10
SMA_LONG  = 21
LOOKBACK  = 60

MARKETS = {
    "nasdaq": {"suffix": "", "tv_prefix": "NASDAQ", "name": "NASDAQ"},
    "nyse":   {"suffix": "", "tv_prefix": "NYSE",   "name": "NYSE"},
}


def load_tickers(market_key: str) -> list[str]:
    path = Path(__file__).parent.parent / "config" / "tickers" / f"{market_key}.txt"
    return [l.strip() for l in path.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def run_scan() -> list[dict]:
    results = []
    end   = datetime.now()
    start = end - timedelta(days=int(LOOKBACK * 1.6))

    for market_key, cfg in MARKETS.items():
        tickers = load_tickers(market_key)
        log.info(f"\n{'='*55}")
        log.info(f"  {cfg['name']}  SMA{SMA_SHORT}/{SMA_LONG}  |  {len(tickers)} tickers")
        log.info(f"{'='*55}")

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

                price  = float(close.iloc[-1])
                s_now  = float(sma_s.iloc[-1])
                l_now  = float(sma_l.iloc[-1])

                if price > s_now > l_now:
                    trend = "BULL"
                elif price < s_now < l_now:
                    trend = "BEAR"
                else:
                    trend = "MIX"

                cross = None
                for k in range(1, 4):
                    s_k  = float(sma_s.iloc[-k])
                    l_k  = float(sma_l.iloc[-k])
                    s_k1 = float(sma_s.iloc[-(k+1)])
                    l_k1 = float(sma_l.iloc[-(k+1)])
                    if s_k1 <= l_k1 and s_k > l_k:
                        cross = f"Golden ({k}d ago)"
                        break
                    if s_k1 >= l_k1 and s_k < l_k:
                        cross = f"Death ({k}d ago)"
                        break

                spread_pct = (s_now - l_now) / price * 100
                vol_ratio  = (float(vol.iloc[-1]) / float(vol.rolling(10).mean().iloc[-1])
                              if len(vol) >= 10 else 1.0)

                flag = ""
                if cross:             flag = f"  *** {cross} ***"
                elif trend == "BULL": flag = "  ↑ bull"
                elif trend == "BEAR": flag = "  ↓ bear"

                log.info(f"[{i:>2}/{len(tickers)}] {ticker:8s}  "
                         f"price={price:>9.2f}  SMA10={s_now:>9.2f}  SMA21={l_now:>9.2f}  "
                         f"spread={spread_pct:>+5.1f}%  trend={trend}{flag}")

                results.append({
                    "tv_symbol":  f"{cfg['tv_prefix']}:{raw}",
                    "market":     cfg["name"],
                    "price":      round(price, 2),
                    "sma10":      round(s_now, 2),
                    "sma21":      round(l_now, 2),
                    "spread_pct": round(spread_pct, 2),
                    "trend":      trend,
                    "crossover":  cross or "",
                    "vol_ratio":  round(vol_ratio, 2),
                })

            except Exception as e:
                log.warning(f"{ticker}: {e}")

            time.sleep(0.3)

    return results


# ── PDF ───────────────────────────────────────────────────────────────────────

DARK_BLUE  = colors.HexColor("#1a3a5c")
BULL_GREEN = colors.HexColor("#d4edda")
BEAR_RED   = colors.HexColor("#f8d7da")
CROSS_GOLD = colors.HexColor("#fff3cd")
ALT_ROW    = colors.HexColor("#f5f5f5")


def _table_style() -> TableStyle:
    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), DARK_BLUE),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 9),
        ("FONTSIZE",      (0, 1), (-1, -1), 8),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("ALIGN",         (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN",         (0, 0), (1, -1), "LEFT"),
    ])


def generate_pdf(results: list[dict], pdf_path: str) -> str:
    doc    = SimpleDocTemplate(pdf_path, pagesize=A4,
                               leftMargin=15*mm, rightMargin=15*mm,
                               topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    story  = []

    title_style = ParagraphStyle("title", parent=styles["Title"],
                                 fontSize=16, textColor=DARK_BLUE, spaceAfter=4)
    sub_style   = ParagraphStyle("sub", parent=styles["Normal"],
                                 fontSize=9, textColor=colors.grey, spaceAfter=12)
    h2_style    = ParagraphStyle("h2", parent=styles["Heading2"],
                                 fontSize=11, textColor=DARK_BLUE, spaceBefore=10, spaceAfter=6)

    story.append(Paragraph("US Stocks SMA10/21 Scan", title_style))
    story.append(Paragraph(
        f"NASDAQ + NYSE  ·  {datetime.now().strftime('%Y-%m-%d %H:%M')}  ·  "
        f"{len(results)} tickers scanned", sub_style))

    df = pd.DataFrame(results)
    cross_df = df[df["crossover"] != ""]
    bull_df  = df[(df["trend"] == "BULL") & (df["crossover"] == "")].sort_values("spread_pct", ascending=False)
    bear_df  = df[(df["trend"] == "BEAR") & (df["crossover"] == "")].sort_values("spread_pct")
    mix_df   = df[(df["trend"] == "MIX")  & (df["crossover"] == "")]

    summary_data = [
        ["Total", "Crossovers", "Bull", "Bear", "Mixed"],
        [str(len(df)), str(len(cross_df)), str(len(bull_df)), str(len(bear_df)), str(len(mix_df))],
    ]
    st = Table(summary_data, colWidths=[70, 90, 70, 70, 70])
    st.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), DARK_BLUE),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.grey),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND",    (1, 1), (1, 1), CROSS_GOLD),
        ("BACKGROUND",    (2, 1), (2, 1), BULL_GREEN),
        ("BACKGROUND",    (3, 1), (3, 1), BEAR_RED),
    ]))
    story.append(st)
    story.append(Spacer(1, 10))

    col_w   = [95, 80, 48, 48, 48, 52, 42]
    headers = ["Symbol", "Crossover / Trend", "Price", "SMA10", "SMA21", "Spread", "Vol"]

    def _section(title: str, subset: pd.DataFrame, row_color, show_cross=False):
        if subset.empty:
            return
        story.append(Paragraph(title, h2_style))
        rows = [headers]
        row_colors = []
        for i, (_, r) in enumerate(subset.iterrows(), 1):
            label = r["crossover"] if show_cross and r["crossover"] else r["trend"]
            rows.append([
                r["tv_symbol"],
                label,
                f"{r['price']:.2f}",
                f"{r['sma10']:.2f}",
                f"{r['sma21']:.2f}",
                f"{r['spread_pct']:+.1f}%",
                f"{r['vol_ratio']:.1f}x",
            ])
            bg = row_color if i % 2 == 0 else colors.white
            row_colors.append(("BACKGROUND", (0, i), (-1, i), bg))

        t = Table(rows, colWidths=col_w)
        ts = _table_style()
        for rc in row_colors:
            ts.add(*rc)
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 8))

    _section("Crossovers", cross_df, CROSS_GOLD, show_cross=True)
    _section("Bullish — price > SMA10 > SMA21", bull_df, BULL_GREEN)
    _section("Bearish — price < SMA10 < SMA21", bear_df, BEAR_RED)
    _section("Mixed / Choppy", mix_df, ALT_ROW)

    doc.build(story)
    log.info(f"PDF generated: {pdf_path}")
    return pdf_path


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_telegram(pdf_path: str):
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN not set")
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    caption = f"US Stocks SMA10/21 Scan — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    with open(pdf_path, "rb") as f:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                             files={"document": f})
    if resp.json().get("ok"):
        log.info("Sent to Telegram")
    else:
        log.error(f"Telegram error: {resp.json()}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    results  = run_scan()
    if not results:
        log.warning("No results — check ticker files")
        return
    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    pdf_path = str(OUTPUT_DIR / f"us_sma_{ts}.pdf")
    generate_pdf(results, pdf_path)
    send_to_telegram(pdf_path)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    main()
