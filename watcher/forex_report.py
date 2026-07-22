#!/usr/bin/env python3
from __future__ import annotations
"""
Forex Afternoon Report — runs 2:00 PM PT Mon-Fri
Pulls OANDA positions + live rates for all watched FX pairs.
All data from OANDA API only (no yfinance — no external Yahoo dependency).
"""

import json
import logging
import os
import urllib.request
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm

load_dotenv(Path(__file__).parent.parent / ".env")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "2130465973")
OANDA_KEY        = os.getenv("OANDA_API_KEY", "")
OANDA_BASE       = os.getenv("OANDA_BASE_URL", "https://api-fxtrade.oanda.com/v3")

# Four independent accounts split by signal timeframe — see
# trader/oanda_trader.py's _ACCOUNTS for the canonical definition. This
# report previously only ever queried OANDA_ACCOUNT_ID (mix), so the
# short/mid/long accounts added later never showed up here at all.
OANDA_ACCOUNTS = {
    "mix":   {"key": OANDA_KEY, "account": os.getenv("OANDA_ACCOUNT_ID", ""),       "prefix": "OANDA"},
    "short": {"key": OANDA_KEY, "account": os.getenv("OANDA_ACCOUNT_ID_SHORT", ""), "prefix": "OANDA_SHORT"},
    "mid":   {"key": OANDA_KEY, "account": os.getenv("OANDA_ACCOUNT_ID_MID", ""),   "prefix": "OANDA_MID"},
    "long":  {"key": OANDA_KEY, "account": os.getenv("OANDA_ACCOUNT_ID_LONG", ""),  "prefix": "OANDA_LONG"},
}
# Kept for fetch_rates()/fetch_sr() — pure market-data endpoints (pricing,
# candles) aren't account-specific, they just need any valid account in
# the URL path, so mix's credentials are fine to reuse there.
OANDA_ACCOUNT = OANDA_ACCOUNTS["mix"]["account"]

# Starting balances for change% tracking — set when each account was funded.
STARTING_BALANCE = {"mix": 20_000, "short": 50_000, "mid": 50_000, "long": 10_000}

OUTPUT_DIR = Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
STATE_FILE = Path(__file__).parent.parent / "output" / "position_state.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
log = logging.getLogger(__name__)

# All FX pairs we watch — shown in rate snapshot even without an open position
WATCHED_PAIRS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "NZD_USD",
]

DARK_BLUE  = colors.HexColor("#1a3a5c")
FX_TEAL    = colors.HexColor("#0d6e6e")
PROFIT_GRN = colors.HexColor("#d4edda")
LOSS_RED   = colors.HexColor("#f8d7da")
ALT_ROW    = colors.HexColor("#f5f5f5")
WARN_AMBER = colors.HexColor("#fff3cd")


# ── OANDA helpers ─────────────────────────────────────────────────────────────

def _get(path: str, account: str = "mix") -> dict:
    creds = OANDA_ACCOUNTS[account]
    req = urllib.request.Request(
        f"{OANDA_BASE}{path}",
        headers={"Authorization": f"Bearer {creds['key']}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def fetch_positions() -> list[dict]:
    positions = []
    for label, creds in OANDA_ACCOUNTS.items():
        if not creds["account"]:
            continue
        try:
            raw = _get(f"/accounts/{creds['account']}/openPositions", account=label)
            for p in raw.get("positions", []):
                instr = p["instrument"]
                for side_key, sign in [("long", 1), ("short", -1)]:
                    units = int(p.get(side_key, {}).get("units", 0))
                    if units == 0:
                        continue
                    entry = float(p[side_key].get("averagePrice", 0))
                    upnl  = float(p[side_key].get("unrealizedPL", 0))
                    positions.append({
                        "instrument": instr,
                        "pair":       instr.replace("_", "/"),
                        "account":    label,
                        "side":       side_key.upper(),
                        "units":      abs(units),
                        "entry":      entry,
                        "upnl":       upnl,
                        "current":    0.0,
                        "tp":         None,
                        "sl":         None,
                        "age_days":   None,
                    })
        except Exception as e:
            log.warning(f"Positions fetch failed for {label}: {e}")

    # Enrich from position_state.json — each account tracks under its own
    # prefix (OANDA:/OANDA_SHORT:/OANDA_MID:/OANDA_LONG:), so look up using
    # the same account this position actually came from, not a fixed list.
    try:
        state = json.loads(STATE_FILE.read_text()).get("open_trades", {})
        for pos in positions:
            prefix = OANDA_ACCOUNTS[pos["account"]]["prefix"]
            # position_state.json keys have no slash (OANDA:NZDUSD, not
            # OANDA:NZD/USD) — pos["pair"] has one for display purposes,
            # so use the raw instrument code here instead. This lookup
            # never actually matched anything before this fix, on any
            # account, so tp/sl/age have been silently blank in every
            # report to date.
            key = f"{prefix}:{pos['instrument'].replace('_', '')}"
            if key in state:
                t = state[key]
                pos["tp"] = t.get("tp")
                pos["sl"] = t.get("sl")
                if t.get("open_time"):
                    try:
                        pos["age_days"] = (
                            datetime.now() - datetime.fromisoformat(t["open_time"])
                        ).days
                    except Exception:
                        pass
    except Exception:
        pass

    return positions


def fetch_rates(instruments: list[str]) -> dict[str, dict]:
    """Fetch current mid + today's OHLC for each instrument."""
    rates = {}
    if not instruments:
        return rates
    try:
        instr_str = ",".join(instruments)
        pricing   = _get(f"/accounts/{OANDA_ACCOUNT}/pricing?instruments={instr_str}")
        for p in pricing.get("prices", []):
            instr = p["instrument"]
            bid   = float(p["bids"][0]["price"])
            ask   = float(p["asks"][0]["price"])
            rates[instr] = {"mid": (bid + ask) / 2, "spread": ask - bid}
    except Exception as e:
        log.warning(f"Pricing fetch failed: {e}")

    # Today's OHLC from daily candle
    for instr in instruments:
        try:
            raw = _get(f"/instruments/{instr}/candles?count=2&granularity=D&price=M")
            candles = raw.get("candles", [])
            if candles:
                today = candles[-1]["mid"]
                prev  = candles[-2]["mid"] if len(candles) > 1 else today
                if instr not in rates:
                    rates[instr] = {}
                rates[instr].update({
                    "open":  float(today["o"]),
                    "high":  float(today["h"]),
                    "low":   float(today["l"]),
                    "prev_close": float(prev["c"]),
                })
        except Exception as e:
            log.warning(f"Candles fetch failed for {instr}: {e}")

    return rates


def fetch_account_nav() -> dict[str, dict]:
    """NAV + change vs. STARTING_BALANCE for each of the 4 accounts."""
    nav = {}
    for label, creds in OANDA_ACCOUNTS.items():
        if not creds["account"]:
            continue
        try:
            data = _get(f"/accounts/{creds['account']}/summary", account=label)["account"]
            current = float(data.get("NAV", 0))
            start   = STARTING_BALANCE.get(label, current)
            nav[label] = {
                "nav":         current,
                "starting":    start,
                "change":      current - start,
                "change_pct":  (current - start) / start * 100 if start else 0.0,
            }
        except Exception as e:
            log.warning(f"NAV fetch failed for {label}: {e}")
    return nav


def fetch_sr(instrument: str, lookback: int = 30) -> dict:
    """Compute swing S/R from OANDA daily candles."""
    try:
        raw     = _get(f"/instruments/{instrument}/candles?count={lookback}&granularity=D&price=M")
        candles = raw.get("candles", [])
        if len(candles) < 10:
            return {}
        highs = [float(c["mid"]["h"]) for c in candles]
        lows  = [float(c["mid"]["l"]) for c in candles]
        mid   = float(candles[-1]["mid"]["c"])
        window = 3
        swing_h = [highs[i] for i in range(window, len(highs) - window)
                   if highs[i] == max(highs[i-window:i+window+1])]
        swing_l = [lows[i]  for i in range(window, len(lows)  - window)
                   if lows[i]  == min(lows[i-window:i+window+1])]
        support    = max((l for l in swing_l if l < mid), default=None)
        resistance = min((h for h in swing_h if h > mid), default=None)
        return {"support": support, "resistance": resistance}
    except Exception as e:
        log.warning(f"S/R fetch failed for {instrument}: {e}")
        return {}


# ── PDF ───────────────────────────────────────────────────────────────────────

def _pnl_color(v: float) -> colors.Color:
    return PROFIT_GRN if v >= 0 else LOSS_RED


def _chg_color(v: float) -> colors.Color:
    return PROFIT_GRN if v > 0 else (LOSS_RED if v < 0 else ALT_ROW)


def generate_pdf(positions: list[dict], rates: dict, sr: dict, nav: dict, pdf_path: str) -> str:
    doc    = SimpleDocTemplate(pdf_path, pagesize=A4,
                               leftMargin=12*mm, rightMargin=12*mm,
                               topMargin=12*mm, bottomMargin=12*mm)
    styles = getSampleStyleSheet()
    h1   = ParagraphStyle("h1",  parent=styles["Title"],    fontSize=15, textColor=DARK_BLUE, spaceAfter=2)
    sub  = ParagraphStyle("sub", parent=styles["Normal"],   fontSize=8,  textColor=colors.grey, spaceAfter=10)
    h2   = ParagraphStyle("h2",  parent=styles["Heading2"], fontSize=10, textColor=DARK_BLUE, spaceBefore=8, spaceAfter=4)
    story = []

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M PT")
    story.append(Paragraph("Forex Afternoon Report", h1))
    story.append(Paragraph(
        f"{now_str}  ·  London Close / NY Afternoon  ·  "
        f"{len(positions)} open position{'s' if len(positions) != 1 else ''}",
        sub
    ))

    # ── Account NAV ───────────────────────────────────────────────────────────
    if nav:
        story.append(Paragraph("Account NAV", h2))
        account_labels = {"mix": "Mix", "short": "Short/1h", "mid": "Mid/4h", "long": "Long/1D"}
        nav_hdr = ["Account", "Starting", "NAV", "Change $", "Change %"]
        nav_cw  = [90, 70, 70, 70, 60]
        nav_rows = [nav_hdr]
        nav_colors = []
        for i, label in enumerate(("mix", "short", "mid", "long"), 1):
            n = nav.get(label)
            if not n:
                continue
            nav_rows.append([
                account_labels[label],
                f"${n['starting']:,.0f}",
                f"${n['nav']:,.2f}",
                f"${n['change']:+,.2f}",
                f"{n['change_pct']:+.2f}%",
            ])
            nav_colors.append(("BACKGROUND", (4, len(nav_rows) - 1), (4, len(nav_rows) - 1),
                               _pnl_color(n['change_pct'])))
        nav_t = Table(nav_rows, colWidths=nav_cw)
        nav_ts = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), DARK_BLUE),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 8),
            ("GRID",          (0, 0), (-1, -1), 0.4, colors.grey),
            ("ALIGN",         (1, 0), (-1, -1), "RIGHT"),
            ("ALIGN",         (0, 0), (0, -1), "LEFT"),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, ALT_ROW]),
        ])
        for nc in nav_colors:
            nav_ts.add(*nc)
        nav_t.setStyle(nav_ts)
        story.append(nav_t)
        story.append(Spacer(1, 10))

    # ── Open Positions ────────────────────────────────────────────────────────
    # Grouped by account — mix/short/mid/long are independent capital pools
    # (see trader/oanda_trader.py), so a flat list would blur together
    # positions that don't actually share risk or margin with each other.
    if positions:
        story.append(Paragraph("Open Positions", h2))
        total_upnl = sum(p["upnl"] for p in positions)
        account_labels = {"mix": "Mix account", "short": "Short/1h account",
                          "mid": "Mid/4h account", "long": "Long/1D account"}

        for label in ("mix", "short", "mid", "long"):
            acct_positions = [p for p in positions if p["account"] == label]
            if not acct_positions and label != "mix":
                continue  # skip empty split accounts, but always show mix

            story.append(Paragraph(account_labels[label], ParagraphStyle(
                "acct", parent=styles["Heading3"], fontSize=9,
                textColor=FX_TEAL, spaceBefore=4, spaceAfter=2)))

            hdr  = ["Pair", "Side", "Units", "Entry", "Current", "uPnL", "TP", "SL", "Age"]
            cw   = [55, 38, 50, 58, 58, 52, 54, 54, 32]
            rows = [hdr]
            row_colors = []
            acct_upnl = sum(p["upnl"] for p in acct_positions)

            for i, p in enumerate(acct_positions, 1):
                instr   = p["instrument"]
                current = rates.get(instr, {}).get("mid", p["entry"])
                p["current"] = current
                rows.append([
                    p["pair"],
                    p["side"],
                    f"{p['units']:,}",
                    f"{p['entry']:.5f}",
                    f"{current:.5f}",
                    f"${p['upnl']:+,.2f}",
                    f"{p['tp']:.5f}" if p["tp"] else "—",
                    f"{p['sl']:.5f}" if p["sl"] else "—",
                    f"{p['age_days']}d" if p["age_days"] is not None else "—",
                ])
                row_colors.append(("BACKGROUND", (5, i), (5, i), _pnl_color(p["upnl"])))

            if not acct_positions:
                story.append(Paragraph("No open positions", sub))
                continue

            t = Table(rows, colWidths=cw)
            ts = TableStyle([
                ("BACKGROUND",    (0, 0), (-1, 0), DARK_BLUE),
                ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
                ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE",      (0, 0), (-1, -1), 8),
                ("GRID",          (0, 0), (-1, -1), 0.4, colors.grey),
                ("ALIGN",         (2, 0), (-1, -1), "RIGHT"),
                ("ALIGN",         (0, 0), (1, -1), "LEFT"),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, ALT_ROW]),
            ])
            for rc in row_colors:
                ts.add(*rc)
            t.setStyle(ts)
            story.append(t)

            # Per-account subtotal row
            sub_t = Table([[f"{account_labels[label]} subtotal", f"${acct_upnl:+,.2f}"]],
                          colWidths=[200, 100])
            sub_t.setStyle(TableStyle([
                ("FONTNAME",      (0, 0), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE",      (0, 0), (-1, -1), 8),
                ("ALIGN",         (1, 0), (1, 0),   "RIGHT"),
                ("BACKGROUND",    (0, 0), (-1, -1), _pnl_color(acct_upnl)),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ]))
            story.append(Spacer(1, 2))
            story.append(sub_t)
            story.append(Spacer(1, 8))

        # Grand total across all accounts
        total_t = Table([["Total unrealized PnL (all accounts)", f"${total_upnl:+,.2f}"]],
                        colWidths=[200, 100])
        total_t.setStyle(TableStyle([
            ("FONTNAME",      (0, 0), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 9),
            ("ALIGN",         (1, 0), (1, 0),   "RIGHT"),
            ("BACKGROUND",    (0, 0), (-1, -1), _pnl_color(total_upnl)),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ]))
        story.append(Spacer(1, 2))
        story.append(total_t)
        story.append(Spacer(1, 10))

    # ── FX Rate Snapshot ──────────────────────────────────────────────────────
    story.append(Paragraph("FX Rate Snapshot", h2))
    snap_hdr = ["Pair", "Mid", "Day Open", "Day High", "Day Low", "Change", "Spread (pips)", "Support", "Resistance"]
    snap_cw  = [52, 54, 54, 54, 54, 48, 60, 52, 58]
    snap_rows = [snap_hdr]
    snap_colors = []

    for instr in WATCHED_PAIRS:
        r = rates.get(instr, {})
        mid      = r.get("mid")
        day_open = r.get("open")
        chg      = (mid - day_open) if (mid and day_open) else None
        chg_pct  = (chg / day_open * 100) if (chg is not None and day_open) else None
        spread   = r.get("spread", 0)
        # Convert spread to pips (JPY pairs: 2dp, others: 4dp)
        pip_mult = 100 if "JPY" in instr else 10000
        spread_pips = spread * pip_mult

        s = sr.get(instr, {})
        pair_label = instr.replace("_", "/")

        snap_rows.append([
            pair_label,
            f"{mid:.5f}"      if mid      else "—",
            f"{day_open:.5f}" if day_open else "—",
            f"{r.get('high', 0):.5f}" if r.get("high") else "—",
            f"{r.get('low',  0):.5f}" if r.get("low")  else "—",
            f"{chg_pct:+.3f}%" if chg_pct is not None else "—",
            f"{spread_pips:.1f}",
            f"{s['support']:.5f}"    if s.get("support")    else "—",
            f"{s['resistance']:.5f}" if s.get("resistance") else "—",
        ])
        i = len(snap_rows) - 1
        if chg_pct is not None:
            snap_colors.append(("BACKGROUND", (5, i), (5, i), _chg_color(chg_pct)))

    snap_t = Table(snap_rows, colWidths=snap_cw)
    snap_ts = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), FX_TEAL),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.grey),
        ("ALIGN",         (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN",         (0, 0), (0, -1), "LEFT"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, ALT_ROW]),
    ])
    for sc in snap_colors:
        snap_ts.add(*sc)
    snap_t.setStyle(snap_ts)
    story.append(snap_t)

    doc.build(story)
    log.info(f"PDF generated: {pdf_path}")
    return pdf_path


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_telegram(pdf_path: str, positions: list[dict], rates: dict, nav: dict):
    if not TELEGRAM_TOKEN:
        return
    total_upnl = sum(p["upnl"] for p in positions)
    sign       = "📈" if total_upnl >= 0 else "📉"

    lines = [f"{sign} Forex Afternoon Report — {datetime.now().strftime('%Y-%m-%d %H:%M PT')}"]

    if nav:
        account_labels = {"mix": "Mix", "short": "Short/1h", "mid": "Mid/4h", "long": "Long/1D"}
        lines.append("NAV:")
        for label, name in account_labels.items():
            n = nav.get(label)
            if not n:
                continue
            lines.append(f"  {name}: ${n['nav']:,.2f}  ({n['change_pct']:+.2f}%)")
        lines.append("")

    if positions:
        lines.append(f"{len(positions)} open position(s)  |  uPnL: ${total_upnl:+,.2f}")
        account_labels = {"mix": "Mix", "short": "Short/1h", "mid": "Mid/4h", "long": "Long/1D"}
        for label, name in account_labels.items():
            acct_positions = [p for p in positions if p["account"] == label]
            if not acct_positions:
                continue
            acct_upnl = sum(p["upnl"] for p in acct_positions)
            lines.append(f"  {name}: {len(acct_positions)} pos  ${acct_upnl:+,.2f}")
    else:
        lines.append("No open FX positions")

    # Rate snapshot summary
    lines.append("")
    for instr in WATCHED_PAIRS:
        r   = rates.get(instr, {})
        mid = r.get("mid")
        chg = ((mid - r["open"]) / r["open"] * 100) if (mid and r.get("open")) else None
        if mid:
            arrow = "▲" if (chg or 0) > 0 else ("▼" if (chg or 0) < 0 else "─")
            lines.append(f"{arrow} {instr.replace('_','/')}: {mid:.5f}"
                         + (f"  ({chg:+.3f}%)" if chg is not None else ""))

    with open(pdf_path, "rb") as f:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": "\n".join(lines)},
            files={"document": f},
        )
    if resp.json().get("ok"):
        log.info("✅ Forex afternoon report sent to Telegram!")
    else:
        log.error(f"Telegram error: {resp.json()}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log.info("=== Forex Afternoon Report ===")

    positions = fetch_positions()
    log.info(f"Open FX positions: {len(positions)}")

    nav = fetch_account_nav()
    for label, n in nav.items():
        log.info(f"  NAV[{label}]: ${n['nav']:,.2f}  ({n['change_pct']:+.2f}%)")

    # Collect instruments to price (watched + open positions)
    open_instrs = [p["instrument"] for p in positions]
    all_instrs  = list(dict.fromkeys(WATCHED_PAIRS + open_instrs))

    rates = fetch_rates(all_instrs)
    log.info(f"Rates fetched: {list(rates.keys())}")

    sr = {}
    for instr in all_instrs:
        sr[instr] = fetch_sr(instr)
        if sr[instr]:
            log.info(f"  {instr}: support={sr[instr].get('support')}  "
                     f"resistance={sr[instr].get('resistance')}")

    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    pdf_path = str(OUTPUT_DIR / f"forex_report_{ts}.pdf")
    generate_pdf(positions, rates, sr, nav, pdf_path)
    send_to_telegram(pdf_path, positions, rates, nav)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    main()
