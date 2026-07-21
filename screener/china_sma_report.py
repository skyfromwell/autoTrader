#!/usr/bin/env python3
from __future__ import annotations
"""
China A-Share SMA10/21 Scanner — PDF Report + Telegram Delivery

Scans SSE + SZSE, queues actions for next market open (09:30 CST):
  Golden Cross / Bull  → queue LONG via miniQMT bridge  (priority: cross > trend)
  Death Cross  / Bear  → note only (no short-selling on A-shares)

Queue: output/china_pending/{PAIR}.json (one file per pair)
Execution: Syncthing replicates the folder to the Windows QMT bridge, where
china_server/china_executor.py polls it continuously and fires each order —
there is no separate execute-queue step to trigger from this side.
"""

import os
import sys
import json
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

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(Path(__file__).parent.parent / ".env")

from watcher.china_queue import load_pending as _load_pending, queue_order as _queue_order

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "2130465973")
CHINA_NOTIONAL   = int(os.getenv("CHINA_NOTIONAL_CNY", "50000"))

OUTPUT_DIR  = Path("output")
POS_FILE    = OUTPUT_DIR / "position_state.json"
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

SMA_SHORT = 10
SMA_LONG  = 21
LOOKBACK  = 60

MARKETS = {
    "sse":  {"suffix": ".SS", "tv_prefix": "SSE",  "name": "Shanghai SE"},
    "szse": {"suffix": ".SZ", "tv_prefix": "SZSE", "name": "Shenzhen SE"},
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

        for raw in tickers:
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
                    s_k  = float(sma_s.iloc[-k]);    l_k  = float(sma_l.iloc[-k])
                    s_k1 = float(sma_s.iloc[-(k+1)]); l_k1 = float(sma_l.iloc[-(k+1)])
                    if s_k1 <= l_k1 and s_k > l_k:
                        cross = f"Golden ({k}d ago)"; break
                    if s_k1 >= l_k1 and s_k < l_k:
                        cross = f"Death ({k}d ago)";  break

                spread_pct = (s_now - l_now) / price * 100
                vol_ratio  = (float(vol.iloc[-1]) / float(vol.rolling(10).mean().iloc[-1])
                              if len(vol) >= 10 else 1.0)

                is_cross      = bool(cross)
                is_gold_cross = "Golden" in (cross or "")   # the only thing that queues a trade
                is_bullish    = is_gold_cross or trend == "BULL"   # descriptive only (PDF/report)
                is_bearish    = "Death"  in (cross or "") or trend == "BEAR"

                results.append({
                    "tv_symbol":     f"{cfg['tv_prefix']}:{raw}",
                    "market":        cfg["name"],
                    "price":         round(price, 3),
                    "sma10":         round(s_now, 3),
                    "sma21":         round(l_now, 3),
                    "spread_pct":    round(spread_pct, 2),
                    "trend":         trend,
                    "crossover":     cross or "",
                    "vol_ratio":     round(vol_ratio, 2),
                    "is_cross":      is_cross,
                    "is_gold_cross": is_gold_cross,
                    "is_bullish": is_bullish,
                    "is_bearish": is_bearish,
                    "priority":   1 if is_cross else 2,
                })
            except Exception as e:
                log.debug(f"{ticker}: {e}")
            time.sleep(0.4)

    # crosses first, then by |spread|
    results.sort(key=lambda r: (r["priority"], -abs(r["spread_pct"])))
    return results


def update_china_queue(results: list[dict]) -> dict:
    """Queue Golden-Cross symbols for next China open. Plain BULL trend (no
    crossover) does NOT queue — only the discrete crossover event does,
    otherwise every day a stock stays in an uptrend re-flags as "buy".
    Bears noted only (no shorting A-shares)."""
    pending    = _load_pending()
    pos_data   = json.loads(POS_FILE.read_text()) if POS_FILE.exists() else {"open_trades": {}}
    open_pairs = set(pos_data.get("open_trades", {}).keys())

    actions = {"queued_long": [], "already_queued": [], "already_long": [],
               "bear_noted": [], "skipped": []}

    for r in results:
        sym = r["tv_symbol"]

        if r["is_gold_cross"]:
            if sym in open_pairs:
                actions["already_long"].append(sym)
                log.info(f"  ~ {sym}: already long — skip")
            elif sym in pending:
                actions["already_queued"].append(sym)
                log.info(f"  ~ {sym}: already in queue — skip")
            else:
                vol_est = int((CHINA_NOTIONAL // r["price"]) // 100) * 100
                sent = _queue_order(sym, r["price"], "D", CHINA_NOTIONAL,
                                    type_="sma_gold_cross", reason=r["crossover"])
                if sent is None:
                    actions["skipped"].append(sym)
                    log.warning(f"  ❌ {sym}: mailbox submit failed — will retry next scan")
                    continue
                pending[sym] = {"pair": sym}  # just needs to mark "now pending" for this loop
                actions["queued_long"].append((sym, r["crossover"] or r["trend"],
                                              r["price"], vol_est))
                log.info(f"  ✅ {sym}: queued LONG  "
                         f"price≈¥{r['price']:.2f}  est_vol={vol_est}sh  "
                         f"[{r['crossover'] or r['trend']}]")

        elif r["is_bearish"]:
            actions["bear_noted"].append(sym)
            log.info(f"  ⚠ {sym}: BEAR signal — noted (no short on A-shares)")

    log.info(f"China queue: +{len(actions['queued_long'])} new  "
             f"({len(pending)} total pending)  "
             f"Bear-noted={len(actions['bear_noted'])}  "
             f"MailboxFailed={len(actions['skipped'])}")
    return actions


# ── PDF ───────────────────────────────────────────────────────────────────────

DARK_BLUE  = colors.HexColor("#1a3a5c")
BULL_GREEN = colors.HexColor("#d4edda")
BEAR_RED   = colors.HexColor("#f8d7da")
CROSS_GOLD = colors.HexColor("#fff3cd")
ALT_ROW    = colors.HexColor("#f5f5f5")
WARN_ORANGE= colors.HexColor("#fde8cc")


def _table_style(header_color=DARK_BLUE) -> TableStyle:
    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), header_color),
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


def generate_pdf(results: list[dict], actions: dict, pdf_path: str) -> str:
    doc    = SimpleDocTemplate(pdf_path, pagesize=A4,
                               leftMargin=15*mm, rightMargin=15*mm,
                               topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    story  = []

    title_style = ParagraphStyle("title", parent=styles["Title"],
                                 fontSize=16, textColor=DARK_BLUE, spaceAfter=4)
    sub_style   = ParagraphStyle("sub",   parent=styles["Normal"],
                                 fontSize=9,  textColor=colors.grey, spaceAfter=10)
    h2_style    = ParagraphStyle("h2",    parent=styles["Heading2"],
                                 fontSize=11, textColor=DARK_BLUE, spaceBefore=10, spaceAfter=4)
    note_style  = ParagraphStyle("note",  parent=styles["Normal"],
                                 fontSize=8,  textColor=colors.HexColor("#555555"), spaceAfter=6)

    story.append(Paragraph("China A-Share SMA10/21 Scan", title_style))
    story.append(Paragraph(
        f"SSE + SZSE  ·  {datetime.now().strftime('%Y-%m-%d %H:%M')}  ·  "
        f"{len(results)} tickers  ·  Executes at 09:30 CST (next open)", sub_style))

    df = pd.DataFrame(results) if results else pd.DataFrame()
    if df.empty:
        story.append(Paragraph("No results.", note_style))
        doc.build(story)
        return pdf_path

    cross_df = df[df["crossover"] != ""]
    bull_df  = df[(df["trend"] == "BULL") & (df["crossover"] == "")].sort_values("spread_pct", ascending=False)
    bear_df  = df[(df["trend"] == "BEAR") & (df["crossover"] == "")].sort_values("spread_pct")
    mix_df   = df[(df["trend"] == "MIX")  & (df["crossover"] == "")]

    # ── Summary bar ──────────────────────────────────────────────────────────
    n_cross = len(cross_df); n_bull = len(bull_df); n_bear = len(bear_df); n_mix = len(mix_df)
    summary_data = [
        ["Total", "Crossovers", "Bull", "Bear", "Mixed"],
        [str(len(df)), str(n_cross), str(n_bull), str(n_bear), str(n_mix)],
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

    # ── Action Plan ───────────────────────────────────────────────────────────
    story.append(Paragraph("Action Plan — executes at 09:30 CST next market open", h2_style))

    queued_syms = {sym for sym, _, _, _ in actions.get("queued_long", [])}
    ap_rows = [["Priority", "Symbol", "Signal", "Price (¥)", "Est. Shares", "Notional (¥)"]]
    ap_ts   = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), DARK_BLUE),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("ALIGN",         (3, 0), (-1, -1), "RIGHT"),
    ])

    if actions.get("queued_long"):
        for i, (sym, reason, price, vol_est) in enumerate(actions["queued_long"], 1):
            is_cross = any(r["tv_symbol"] == sym and r["crossover"] for r in results)
            priority = "★ Cross" if is_cross else "  Trend"
            row_bg   = CROSS_GOLD if is_cross else BULL_GREEN
            ap_rows.append([priority, sym, reason,
                            f"¥{price:.2f}", f"{vol_est:,} sh",
                            f"¥{vol_est * price:,.0f}"])
            ap_ts.add("BACKGROUND", (0, i), (-1, i), row_bg)

        if actions.get("already_queued"):
            ap_rows.append(["—", f"Already queued: {', '.join(actions['already_queued'][:4])}"
                            + ("…" if len(actions['already_queued']) > 4 else ""),
                            "", "", "", ""])
            ap_ts.add("TEXTCOLOR", (0, len(ap_rows)-1), (-1, len(ap_rows)-1), colors.grey)

        t = Table(ap_rows, colWidths=[52, 95, 88, 58, 62, 68])
        t.setStyle(ap_ts)
        story.append(t)

        if actions.get("bear_noted"):
            story.append(Paragraph(
                f"Bear/Death signals (no action — A-shares long only): "
                f"{', '.join(actions['bear_noted'][:8])}"
                + ("…" if len(actions['bear_noted']) > 8 else ""),
                note_style))
    else:
        story.append(Paragraph("No new orders to queue.", note_style))
        if actions.get("already_queued"):
            story.append(Paragraph(
                f"Already in queue: {', '.join(actions['already_queued'])}", note_style))
        if actions.get("already_long"):
            story.append(Paragraph(
                f"Already long: {', '.join(actions['already_long'])}", note_style))

    story.append(Spacer(1, 6))

    # ── Signal tables ─────────────────────────────────────────────────────────
    col_w   = [95, 72, 52, 52, 52, 52, 42, 50]
    headers = ["Symbol", "Signal", "Price", "SMA10", "SMA21", "Spread", "Vol", "Action"]

    def _section(title: str, subset: pd.DataFrame, row_color, show_cross=False):
        if subset.empty:
            return
        story.append(Paragraph(title, h2_style))
        rows = [headers]
        ts   = _table_style()
        for i, (_, r) in enumerate(subset.iterrows(), 1):
            label  = r["crossover"] if show_cross and r["crossover"] else r["trend"]
            sym    = r["tv_symbol"]
            if sym in queued_syms:
                act = "→ BUY"
            elif r["is_bearish"]:
                act = "⚠ BEAR"
            else:
                act = "—"
            rows.append([sym, label,
                         f"{r['price']:.3f}", f"{r['sma10']:.3f}", f"{r['sma21']:.3f}",
                         f"{r['spread_pct']:+.1f}%", f"{r['vol_ratio']:.1f}x", act])
            bg = row_color if i % 2 == 0 else colors.white
            ts.add("BACKGROUND", (0, i), (-1, i), bg)
            if act == "→ BUY":
                ts.add("FONTNAME", (7, i), (7, i), "Helvetica-Bold")
                ts.add("TEXTCOLOR", (7, i), (7, i), colors.HexColor("#155724"))
            elif act == "⚠ BEAR":
                ts.add("TEXTCOLOR", (7, i), (7, i), colors.HexColor("#721c24"))
        t = Table(rows, colWidths=col_w)
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 8))

    _section("Crossovers (Priority 1)",           cross_df, CROSS_GOLD, show_cross=True)
    _section("Bullish — price > SMA10 > SMA21",   bull_df,  BULL_GREEN)
    _section("Bearish — price < SMA10 < SMA21",   bear_df,  BEAR_RED)
    _section("Mixed / Choppy",                    mix_df,   ALT_ROW)

    doc.build(story)
    log.info(f"PDF → {pdf_path}")
    return pdf_path


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_telegram(pdf_path: str, actions: dict):
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN not set"); return

    lines = [f"📊 China A-Share SMA10/21 — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"Executes at 09:30 CST next open  ·  Notional ¥{CHINA_NOTIONAL:,}/trade", ""]

    if actions.get("queued_long"):
        lines.append("🟢 BUY at next open:")
        for sym, reason, price, vol_est in actions["queued_long"]:
            tag = "★" if "Golden" in reason else " "
            lines.append(f"  {tag} {sym}  ¥{price:.2f}  ~{vol_est}sh  [{reason}]")

    if actions.get("already_queued"):
        lines.append(f"Already queued: {', '.join(actions['already_queued'])}")

    if actions.get("bear_noted"):
        lines.append(f"⚠ Bear (no action): {', '.join(actions['bear_noted'][:5])}"
                     + ("…" if len(actions["bear_noted"]) > 5 else ""))

    if not actions.get("queued_long") and not actions.get("already_queued"):
        lines.append("No new orders queued.")

    caption = "\n".join(lines)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
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
    log.info(f"  China A-Share SMA Scan  |  {datetime.now():%Y-%m-%d %H:%M}")
    log.info("=" * 60)

    results  = run_scan()
    actions  = update_china_queue(results)
    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    pdf_path = str(OUTPUT_DIR / f"china_sma_{ts}.pdf")
    generate_pdf(results, actions, pdf_path)
    send_to_telegram(pdf_path, actions)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))
    main()
