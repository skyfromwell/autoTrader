#!/usr/bin/env python3
from __future__ import annotations
"""
US Stocks SMA10/21 Daily Screener — runs at 2 PM PT, Sun-Thu

Scans NASDAQ + NYSE for 10/21 SMA conditions, skips Mixed.
- Bull / Golden Cross  → add to TV watchlist + queue next-day Alpaca long open
- Bear / Death Cross   → if open position → queue next-day close
                         if in watchlist with no position → remove from watchlist
Generates PDF + sends to Telegram.
Queue file: output/us_pending_orders.json  (executed next morning at 09:30 ET)
"""

import os, sys, json, time, logging
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

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(Path(__file__).parent.parent / ".env")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "2130465973")
NOTIONAL         = int(os.getenv("US_SMA_NOTIONAL", "10000"))

OUTPUT_DIR   = Path("output")
PENDING_FILE = OUTPUT_DIR / "us_pending_orders.json"
WL_FILE      = OUTPUT_DIR / "watchlist_state.json"
POS_FILE     = OUTPUT_DIR / "position_state.json"
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


# ── Scan ──────────────────────────────────────────────────────────────────────

def run_scan() -> list[dict]:
    results = []
    end   = datetime.now()
    start = end - timedelta(days=int(LOOKBACK * 1.6))

    for market_key, cfg in MARKETS.items():
        tickers = load_tickers(market_key)
        log.info(f"Scanning {cfg['name']} — {len(tickers)} tickers")

        for i, raw in enumerate(tickers, 1):
            ticker = f"{raw}{cfg['suffix']}"
            try:
                hist = yf.download(ticker, start=start, end=end,
                                   progress=False, auto_adjust=True)
                if hist.empty or len(hist) < SMA_LONG + 3:
                    continue

                close = hist["Close"].squeeze().dropna()
                vol   = hist["Volume"].squeeze().dropna()
                sma_s = close.rolling(SMA_SHORT).mean()
                sma_l = close.rolling(SMA_LONG).mean()

                price = float(close.iloc[-1])
                s_now = float(sma_s.iloc[-1])
                l_now = float(sma_l.iloc[-1])

                if price > s_now > l_now:
                    trend = "BULL"
                elif price < s_now < l_now:
                    trend = "BEAR"
                else:
                    trend = "MIX"

                cross = None
                for k in range(1, 4):
                    s_k  = float(sma_s.iloc[-k]);   l_k  = float(sma_l.iloc[-k])
                    s_k1 = float(sma_s.iloc[-(k+1)]); l_k1 = float(sma_l.iloc[-(k+1)])
                    if s_k1 <= l_k1 and s_k > l_k:
                        cross = f"Golden ({k}d ago)"; break
                    if s_k1 >= l_k1 and s_k < l_k:
                        cross = f"Death ({k}d ago)";  break

                if trend == "MIX" and not cross:
                    continue  # skip Mixed entirely

                spread_pct = (s_now - l_now) / price * 100
                vol_ratio  = (float(vol.iloc[-1]) / float(vol.rolling(10).mean().iloc[-1])
                              if len(vol) >= 10 else 1.0)

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

                flag = f"  *** {cross} ***" if cross else ("  ↑ bull" if trend == "BULL" else "  ↓ bear")
                log.info(f"[{i:>2}] {ticker:8s}  price={price:>9.2f}  "
                         f"SMA10={s_now:>9.2f}  SMA21={l_now:>9.2f}  "
                         f"spread={spread_pct:>+5.1f}%  {flag}")

            except Exception as e:
                log.warning(f"{ticker}: {e}")
            time.sleep(0.3)

    return results


# ── Watchlist + queue ─────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def update_watchlist_and_queue(results: list[dict]) -> dict:
    wl_data   = _load_json(WL_FILE, {"symbols": []})
    pos_data  = _load_json(POS_FILE, {"open_trades": {}})
    pending   = _load_json(PENDING_FILE, [])

    watchlist    = set(wl_data.get("symbols", []))
    open_trades  = set(pos_data.get("open_trades", {}).keys())
    pending_syms = {o["tv_symbol"] for o in pending}
    now_str      = datetime.now().isoformat(timespec="seconds")

    actions = {"added": [], "queued_open": [], "queued_close": [], "removed": []}

    for r in results:
        sym   = r["tv_symbol"]
        is_bull_signal = r["trend"] == "BULL" or (r["crossover"] and "Golden" in r["crossover"])
        is_bear_signal = r["trend"] == "BEAR" or (r["crossover"] and "Death"  in r["crossover"])

        if is_bull_signal:
            if sym not in watchlist:
                watchlist.add(sym)
                actions["added"].append(sym)
                log.info(f"  + Added to watchlist: {sym}")
            if sym not in open_trades and sym not in pending_syms:
                pending.append({"tv_symbol": sym, "action": "open_long",
                                "reason": r["crossover"] or r["trend"],
                                "notional": NOTIONAL, "queued_at": now_str})
                actions["queued_open"].append(sym)
                log.info(f"  → Queued next-day LONG: {sym}")

        elif is_bear_signal:
            if sym in open_trades and sym not in pending_syms:
                pending.append({"tv_symbol": sym, "action": "close",
                                "reason": r["crossover"] or r["trend"],
                                "queued_at": now_str})
                actions["queued_close"].append(sym)
                log.info(f"  → Queued next-day CLOSE: {sym}")
            elif sym in watchlist and sym not in open_trades:
                watchlist.discard(sym)
                actions["removed"].append(sym)
                log.info(f"  - Removed from watchlist: {sym}")

    wl_data["symbols"] = sorted(watchlist)
    WL_FILE.write_text(json.dumps(wl_data, indent=2))
    PENDING_FILE.write_text(json.dumps(pending, indent=2))

    log.info(f"Watchlist: {len(watchlist)} symbols  |  "
             f"Added={len(actions['added'])}  Removed={len(actions['removed'])}  "
             f"QueuedOpen={len(actions['queued_open'])}  QueuedClose={len(actions['queued_close'])}")
    return actions


# ── PDF ───────────────────────────────────────────────────────────────────────

DARK_BLUE  = colors.HexColor("#1a3a5c")
BULL_GREEN = colors.HexColor("#d4edda")
BEAR_RED   = colors.HexColor("#f8d7da")
CROSS_GOLD = colors.HexColor("#fff3cd")
ALT_ROW    = colors.HexColor("#f5f5f5")


def generate_pdf(results: list[dict], actions: dict, pdf_path: str) -> str:
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
    note_style  = ParagraphStyle("note", parent=styles["Normal"],
                                 fontSize=8, textColor=colors.HexColor("#555555"), spaceAfter=8)

    story.append(Paragraph("US Stocks SMA10/21 Daily Scan", title_style))
    story.append(Paragraph(
        f"NASDAQ + NYSE  ·  {datetime.now().strftime('%Y-%m-%d %H:%M PT')}  ·  "
        f"{len(results)} signals (Mixed excluded)", sub_style))

    df = pd.DataFrame(results) if results else pd.DataFrame()
    cross_df = df[df["crossover"] != ""] if not df.empty else pd.DataFrame()
    bull_df  = df[(df["trend"] == "BULL") & (df["crossover"] == "")].sort_values("spread_pct", ascending=False) if not df.empty else pd.DataFrame()
    bear_df  = df[(df["trend"] == "BEAR") & (df["crossover"] == "")].sort_values("spread_pct") if not df.empty else pd.DataFrame()

    # Summary table
    q_open  = len(actions.get("queued_open", []))
    q_close = len(actions.get("queued_close", []))
    summary_data = [
        ["Crossovers", "Bull", "Bear", "Queued Open", "Queued Close"],
        [str(len(cross_df)), str(len(bull_df)), str(len(bear_df)), str(q_open), str(q_close)],
    ]
    st = Table(summary_data, colWidths=[90, 70, 70, 90, 90])
    st.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), DARK_BLUE),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.grey),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND",    (0, 1), (0, 1), CROSS_GOLD),
        ("BACKGROUND",    (1, 1), (1, 1), BULL_GREEN),
        ("BACKGROUND",    (2, 1), (2, 1), BEAR_RED),
    ]))
    story.append(st)
    story.append(Spacer(1, 10))

    if actions.get("queued_open") or actions.get("queued_close"):
        lines = []
        if actions.get("queued_open"):
            lines.append(f"Next-day OPEN (long): {', '.join(actions['queued_open'])}")
        if actions.get("queued_close"):
            lines.append(f"Next-day CLOSE: {', '.join(actions['queued_close'])}")
        if actions.get("added"):
            lines.append(f"Added to watchlist: {', '.join(actions['added'])}")
        story.append(Paragraph("  |  ".join(lines), note_style))

    col_w   = [100, 85, 45, 45, 45, 50, 40]
    headers = ["Symbol", "Signal", "Price", "SMA10", "SMA21", "Spread", "Vol"]

    def _table_style_base() -> TableStyle:
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

    def _section(title: str, subset: pd.DataFrame, row_color, show_cross=False):
        if subset.empty:
            return
        story.append(Paragraph(title, h2_style))
        rows = [headers]
        ts   = _table_style_base()
        for i, (_, r) in enumerate(subset.iterrows(), 1):
            label = r["crossover"] if show_cross and r["crossover"] else r["trend"]
            rows.append([r["tv_symbol"], label,
                         f"{r['price']:.2f}", f"{r['sma10']:.2f}", f"{r['sma21']:.2f}",
                         f"{r['spread_pct']:+.1f}%", f"{r['vol_ratio']:.1f}x"])
            bg = row_color if i % 2 == 0 else colors.white
            ts.add("BACKGROUND", (0, i), (-1, i), bg)
        t = Table(rows, colWidths=col_w)
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 8))

    _section("Crossovers", cross_df, CROSS_GOLD, show_cross=True)
    _section("Bullish — price > SMA10 > SMA21", bull_df, BULL_GREEN)
    _section("Bearish — price < SMA10 < SMA21", bear_df, BEAR_RED)

    doc.build(story)
    log.info(f"PDF → {pdf_path}")
    return pdf_path


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_telegram(pdf_path: str, actions: dict):
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN not set"); return
    lines   = [f"US SMA10/21 Scan — {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    if actions.get("queued_open"):
        lines.append(f"Queued open: {', '.join(actions['queued_open'])}")
    if actions.get("queued_close"):
        lines.append(f"Queued close: {', '.join(actions['queued_close'])}")
    caption = "\n".join(lines)
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    with open(pdf_path, "rb") as f:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                             files={"document": f})
    if resp.json().get("ok"):
        log.info("Sent to Telegram")
    else:
        log.error(f"Telegram error: {resp.json()}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info(f"  US SMA Daily Scan  |  {datetime.now():%Y-%m-%d %H:%M PT}")
    log.info("=" * 60)

    results = run_scan()
    actions = update_watchlist_and_queue(results)

    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    pdf_path = str(OUTPUT_DIR / f"us_sma_{ts}.pdf")
    generate_pdf(results, actions, pdf_path)
    send_to_telegram(pdf_path, actions)


if __name__ == "__main__":
    main()
