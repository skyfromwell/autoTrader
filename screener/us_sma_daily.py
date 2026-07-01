#!/usr/bin/env python3
from __future__ import annotations
"""
US Stocks SMA10/21 Daily Screener — runs at 2 PM PT, Sun-Thu

Scans NASDAQ + NYSE for 10/21 SMA conditions, skips Mixed.
  Golden Cross / Bull  → open LONG  next-day at market open
  Death Cross  / Bear  → open SHORT next-day at market open
                         (if opposite position exists, close it first)

Margin allocation (respects Alpaca buying power):
  Priority 1 — Crosses  (Golden/Death): $15,000 each
  Priority 2 — Trends   (Bull/Bear):    $10,000 each
  Stops queuing when remaining budget exhausted.

Queue file: output/us_pending_orders.json  (fired next morning 09:30 ET)
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
NOTIONAL_CROSS   = int(os.getenv("US_CROSS_NOTIONAL",  "15000"))
NOTIONAL_TREND   = int(os.getenv("US_TREND_NOTIONAL",  "10000"))
MARGIN_RESERVE   = float(os.getenv("US_MARGIN_RESERVE", "0.30"))  # keep 30% buying power free

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


# ── Alpaca helpers ────────────────────────────────────────────────────────────

def _alpaca_headers() -> dict:
    return {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "")}

_ALPACA_BASE = "https://paper-api.alpaca.markets"


def _alpaca_buying_power() -> float:
    try:
        r = requests.get(f"{_ALPACA_BASE}/v2/account", headers=_alpaca_headers(), timeout=8)
        return float(r.json().get("buying_power", 0))
    except Exception:
        return 0.0


def _alpaca_open_sides() -> dict[str, str]:
    """Returns {symbol: 'long'|'short'} for current Alpaca positions."""
    try:
        r = requests.get(f"{_ALPACA_BASE}/v2/positions", headers=_alpaca_headers(), timeout=8)
        return {p["symbol"]: p["side"] for p in r.json()}
    except Exception:
        return {}


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
                    s_k  = float(sma_s.iloc[-k]);    l_k  = float(sma_l.iloc[-k])
                    s_k1 = float(sma_s.iloc[-(k+1)]); l_k1 = float(sma_l.iloc[-(k+1)])
                    if s_k1 <= l_k1 and s_k > l_k:
                        cross = f"Golden ({k}d ago)"; break
                    if s_k1 >= l_k1 and s_k < l_k:
                        cross = f"Death ({k}d ago)";  break

                if trend == "MIX" and not cross:
                    continue

                spread_pct = (s_now - l_now) / price * 100
                vol_ratio  = (float(vol.iloc[-1]) / float(vol.rolling(10).mean().iloc[-1])
                              if len(vol) >= 10 else 1.0)

                is_cross  = bool(cross)
                is_bull   = "Golden" in (cross or "") or trend == "BULL"
                direction = "long" if is_bull else "short"
                notional  = NOTIONAL_CROSS if is_cross else NOTIONAL_TREND
                priority  = 1 if is_cross else 2

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
                    "direction":  direction,
                    "notional":   notional,
                    "priority":   priority,
                })

                flag = f"  *** {cross} ***" if cross else ("  ↑ BULL" if trend == "BULL" else "  ↓ BEAR")
                log.info(f"[{i:>2}] {ticker:8s}  price={price:>9.2f}  "
                         f"SMA10={s_now:>9.2f}  SMA21={l_now:>9.2f}  "
                         f"spread={spread_pct:>+5.1f}%  dir={direction.upper()}{flag}")

            except Exception as e:
                log.warning(f"{ticker}: {e}")
            time.sleep(0.3)

    # Sort: crosses first (by |spread|), then trends
    results.sort(key=lambda r: (r["priority"], -abs(r["spread_pct"])))
    return results


# ── Watchlist + queue ─────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def update_watchlist_and_queue(results: list[dict]) -> dict:
    wl_data      = _load_json(WL_FILE,      {"symbols": []})
    pos_data     = _load_json(POS_FILE,     {"open_trades": {}})
    pending      = _load_json(PENDING_FILE, [])

    watchlist     = set(wl_data.get("symbols", []))
    open_trades   = pos_data.get("open_trades", {})
    pending_syms  = {o["tv_symbol"] for o in pending}
    alpaca_sides  = _alpaca_open_sides()

    buying_power  = _alpaca_buying_power()
    budget        = buying_power * (1.0 - MARGIN_RESERVE)
    allocated     = 0.0
    now_str       = datetime.now().isoformat(timespec="seconds")

    log.info(f"Alpaca buying power: ${buying_power:,.0f}  |  "
             f"Budget for new trades: ${budget:,.0f}  (reserve {MARGIN_RESERVE:.0%})")

    actions = {
        "queued_long":  [],   # (sym, reason, notional)
        "queued_short": [],
        "queued_close": [],   # existing opposite position closed before new entry
        "added_wl":     [],
        "removed_wl":   [],
        "skipped_budget": [],
        "skipped_exists": [],
    }

    for r in results:
        sym        = r["tv_symbol"]
        direction  = r["direction"]   # "long" or "short"
        notional   = r["notional"]
        reason     = r["crossover"] or r["trend"]
        ticker_sym = sym.split(":")[-1]

        # Check existing Alpaca position
        alpaca_side = alpaca_sides.get(ticker_sym)
        has_same    = alpaca_side == direction
        has_opposite= alpaca_side is not None and alpaca_side != direction

        # Add bull symbols to watchlist; remove bear symbols not in position
        if direction == "long":
            if sym not in watchlist:
                watchlist.add(sym)
                actions["added_wl"].append(sym)
        else:
            if sym in watchlist and sym not in open_trades:
                watchlist.discard(sym)
                actions["removed_wl"].append(sym)

        # Skip if already in desired direction
        if has_same:
            actions["skipped_exists"].append(sym)
            log.info(f"  ~ {sym}: already {direction} — skip")
            continue

        # Skip if already queued
        if sym in pending_syms:
            log.info(f"  ~ {sym}: already in pending queue — skip")
            continue

        # Close opposite position first (no budget cost — it frees margin)
        if has_opposite:
            pending.append({"tv_symbol": sym, "action": "close",
                            "reason": f"flip_to_{direction}", "queued_at": now_str})
            actions["queued_close"].append(sym)
            log.info(f"  ↩ {sym}: queued CLOSE existing {alpaca_side} before {direction}")

        # Check budget for the new entry
        if allocated + notional > budget:
            actions["skipped_budget"].append(sym)
            log.info(f"  ✗ {sym}: budget exhausted (${allocated:,.0f} used / ${budget:,.0f})")
            continue

        # Queue the new trade
        action_key = "open_long" if direction == "long" else "open_short"
        pending.append({"tv_symbol": sym, "action": action_key,
                        "reason": reason, "notional": notional, "queued_at": now_str})
        allocated += notional
        pending_syms.add(sym)

        if direction == "long":
            actions["queued_long"].append((sym, reason, notional))
            log.info(f"  ✅ {sym}: queued LONG  ${notional:,}  [{reason}]  "
                     f"(total allocated: ${allocated:,.0f})")
        else:
            actions["queued_short"].append((sym, reason, notional))
            log.info(f"  ✅ {sym}: queued SHORT ${notional:,}  [{reason}]  "
                     f"(total allocated: ${allocated:,.0f})")

    actions["total_allocated"] = allocated
    actions["budget"]          = budget
    actions["buying_power"]    = buying_power

    wl_data["symbols"] = sorted(watchlist)
    WL_FILE.write_text(json.dumps(wl_data, indent=2))
    PENDING_FILE.write_text(json.dumps(pending, indent=2))

    log.info(f"Queue updated  |  Long={len(actions['queued_long'])}  "
             f"Short={len(actions['queued_short'])}  "
             f"Close={len(actions['queued_close'])}  "
             f"Skipped(budget)={len(actions['skipped_budget'])}  "
             f"Total allocated=${allocated:,.0f}")
    return actions


# ── PDF ───────────────────────────────────────────────────────────────────────

DARK_BLUE   = colors.HexColor("#1a3a5c")
BULL_GREEN  = colors.HexColor("#d4edda")
BEAR_RED    = colors.HexColor("#f8d7da")
CROSS_GOLD  = colors.HexColor("#fff3cd")
ACTION_BLUE = colors.HexColor("#cce5ff")
ALT_ROW     = colors.HexColor("#f5f5f5")


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

    story.append(Paragraph("US Stocks SMA10/21 Daily Scan", title_style))
    story.append(Paragraph(
        f"NASDAQ + NYSE  ·  {datetime.now().strftime('%Y-%m-%d %H:%M PT')}  ·  "
        f"{len(results)} signals  ·  Budget ${actions.get('budget', 0):,.0f} "
        f"(buying power ${actions.get('buying_power', 0):,.0f})", sub_style))

    # ── Action Plan table ────────────────────────────────────────────────────
    df = pd.DataFrame(results) if results else pd.DataFrame()
    cross_df = df[df["crossover"] != ""].copy() if not df.empty else pd.DataFrame()
    bull_df  = df[(df["trend"] == "BULL") & (df["crossover"] == "")].copy() if not df.empty else pd.DataFrame()
    bear_df  = df[(df["trend"] == "BEAR") & (df["crossover"] == "")].copy() if not df.empty else pd.DataFrame()

    queued_long_syms  = {s for s, _, _ in actions.get("queued_long",  [])}
    queued_short_syms = {s for s, _, _ in actions.get("queued_short", [])}
    queued_close_syms = set(actions.get("queued_close", []))

    story.append(Paragraph("Action Plan — executes at next market open (09:30 ET)", h2_style))
    ap_rows = [["Priority", "Symbol", "Action", "Signal", "Notional", "Price", "Spread"]]
    ap_ts   = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), DARK_BLUE),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("ALIGN",         (4, 0), (-1, -1), "RIGHT"),
    ])

    ap_i = 1
    for label, sym_set, action_str, row_color in [
        ("★ Cross", queued_long_syms  & {r["tv_symbol"] for r in results if r.get("crossover")}, "LONG",  CROSS_GOLD),
        ("★ Cross", queued_short_syms & {r["tv_symbol"] for r in results if r.get("crossover")}, "SHORT", CROSS_GOLD),
        ("  Trend", queued_long_syms  - {r["tv_symbol"] for r in results if r.get("crossover")}, "LONG",  BULL_GREEN),
        ("  Trend", queued_short_syms - {r["tv_symbol"] for r in results if r.get("crossover")}, "SHORT", BEAR_RED),
    ]:
        for r in results:
            if r["tv_symbol"] not in sym_set: continue
            sig = r["crossover"] or r["trend"]
            ap_rows.append([label, r["tv_symbol"], action_str, sig,
                            f"${r['notional']:,}", f"{r['price']:.2f}",
                            f"{r['spread_pct']:+.1f}%"])
            ap_ts.add("BACKGROUND", (0, ap_i), (-1, ap_i), row_color)
            ap_i += 1

    if len(ap_rows) > 1:
        t = Table(ap_rows, colWidths=[48, 90, 45, 95, 60, 50, 52])
        t.setStyle(ap_ts)
        story.append(t)
        total_alloc = actions.get("total_allocated", 0)
        story.append(Paragraph(
            f"Total allocated: ${total_alloc:,.0f}  |  "
            f"Skipped (budget): {len(actions.get('skipped_budget', []))}  |  "
            f"Close first: {', '.join(actions.get('queued_close', [])) or 'none'}",
            note_style))
    else:
        story.append(Paragraph("No new orders queued.", note_style))

    story.append(Spacer(1, 6))

    # ── Signal tables ─────────────────────────────────────────────────────────
    col_w   = [95, 80, 48, 48, 48, 52, 42, 48]
    headers = ["Symbol", "Signal", "Price", "SMA10", "SMA21", "Spread", "Vol", "Action"]

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

    def _action_label(sym, direction):
        if sym in queued_long_syms:  return "→ LONG"
        if sym in queued_short_syms: return "→ SHORT"
        if sym in queued_close_syms: return "→ CLOSE"
        return "—"

    def _section(title: str, subset: pd.DataFrame, row_color, show_cross=False):
        if subset.empty: return
        story.append(Paragraph(title, h2_style))
        rows = [headers]
        ts   = _table_style_base()
        for i, (_, r) in enumerate(subset.iterrows(), 1):
            label = r["crossover"] if show_cross and r["crossover"] else r["trend"]
            act   = _action_label(r["tv_symbol"], r["direction"])
            rows.append([r["tv_symbol"], label,
                         f"{r['price']:.2f}", f"{r['sma10']:.2f}", f"{r['sma21']:.2f}",
                         f"{r['spread_pct']:+.1f}%", f"{r['vol_ratio']:.1f}x", act])
            bg = row_color if i % 2 == 0 else colors.white
            ts.add("BACKGROUND", (0, i), (-1, i), bg)
            if act not in ("—", "→ CLOSE"):
                ts.add("FONTNAME", (7, i), (7, i), "Helvetica-Bold")
        t = Table(rows, colWidths=col_w)
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 8))

    _section("Crossovers (Priority 1)",                   cross_df, CROSS_GOLD, show_cross=True)
    _section("Bullish Trend — price > SMA10 > SMA21",     bull_df,  BULL_GREEN)
    _section("Bearish Trend — price < SMA10 < SMA21",     bear_df,  BEAR_RED)

    doc.build(story)
    log.info(f"PDF → {pdf_path}")
    return pdf_path


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_telegram(pdf_path: str, actions: dict, results: list[dict]):
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN not set"); return

    lines = [f"📊 US SMA10/21 — {datetime.now().strftime('%Y-%m-%d %H:%M PT')}",
             f"Budget: ${actions.get('budget', 0):,.0f}  "
             f"(buying power ${actions.get('buying_power', 0):,.0f})",
             ""]

    if actions.get("queued_long"):
        lines.append("🟢 LONG (next open):")
        for sym, reason, notional in actions["queued_long"]:
            tag = "★" if any(r["tv_symbol"] == sym and r["crossover"] for r in results) else " "
            lines.append(f"  {tag} {sym}  ${notional:,}  [{reason}]")

    if actions.get("queued_short"):
        lines.append("🔴 SHORT (next open):")
        for sym, reason, notional in actions["queued_short"]:
            tag = "★" if any(r["tv_symbol"] == sym and r["crossover"] for r in results) else " "
            lines.append(f"  {tag} {sym}  ${notional:,}  [{reason}]")

    if actions.get("queued_close"):
        lines.append(f"🔄 Close first: {', '.join(actions['queued_close'])}")

    if actions.get("skipped_budget"):
        lines.append(f"⏭ Skipped (budget): {', '.join(actions['skipped_budget'][:5])}"
                     + ("…" if len(actions["skipped_budget"]) > 5 else ""))

    total = len(actions.get("queued_long", [])) + len(actions.get("queued_short", []))
    lines.append(f"\nTotal queued: {total}  |  Allocated: ${actions.get('total_allocated', 0):,.0f}")

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
    log.info(f"  US SMA Daily Scan  |  {datetime.now():%Y-%m-%d %H:%M PT}")
    log.info("=" * 60)

    results  = run_scan()
    actions  = update_watchlist_and_queue(results)
    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    pdf_path = str(OUTPUT_DIR / f"us_sma_{ts}.pdf")
    generate_pdf(results, actions, pdf_path)
    send_to_telegram(pdf_path, actions, results)


if __name__ == "__main__":
    main()
