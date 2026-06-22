#!/usr/bin/env python3
from __future__ import annotations
"""
Morning Position Report — runs 8am PT Mon-Fri
Fetches all open positions from Alpaca, Hyperliquid, and Oanda,
generates a PDF with performance analysis, sends to Telegram.
"""

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
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
ALPACA_KEY       = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET    = os.getenv("ALPACA_SECRET_KEY", "")
OANDA_KEY        = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT    = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_BASE       = os.getenv("OANDA_BASE_URL", "https://api-fxtrade.oanda.com/v3")
HL_ADDRESS       = os.getenv("HL_WALLET_ADDRESS", "")

STATE_FILE = Path(__file__).parent.parent / "output" / "position_state.json"
OUTPUT_DIR = Path(__file__).parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")

DARK_BLUE  = colors.HexColor("#1a3a5c")
PROFIT_GRN = colors.HexColor("#d4edda")
LOSS_RED   = colors.HexColor("#f8d7da")
ALT_ROW    = colors.HexColor("#f5f5f5")


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _alpaca_request(path: str) -> dict:
    url = f"https://paper-api.alpaca.markets/v2{path}"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _oanda_request(path: str) -> dict:
    url = f"{OANDA_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {OANDA_KEY}",
        "Content-Type":  "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _hl_request(payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=data, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def fetch_alpaca_positions() -> list[dict]:
    positions = []
    try:
        raw = _alpaca_request("/positions")
        for p in raw:
            entry    = float(p["avg_entry_price"])
            current  = float(p["current_price"])
            qty      = float(p["qty"])
            side     = p["side"].upper()
            upnl     = float(p["unrealized_pl"])
            upnl_pct = float(p["unrealized_plpc"]) * 100
            age_days = None
            positions.append({
                "broker":    "Alpaca",
                "symbol":    p["symbol"],
                "side":      side,
                "size":      abs(qty),
                "entry":     entry,
                "current":   current,
                "upnl":      upnl,
                "upnl_pct":  upnl_pct,
                "tp":        None,
                "sl":        None,
                "age_days":  age_days,
                "market_val": float(p.get("market_value", 0)),
            })
    except Exception as e:
        log.warning(f"Alpaca fetch failed: {e}")

    # Enrich with TP/SL from position_state.json
    try:
        state = json.loads(STATE_FILE.read_text()).get("open_trades", {})
        for pos in positions:
            key = f"NASDAQ:{pos['symbol']}" if pos['symbol'] not in state else pos['symbol']
            # Try common prefixes
            for prefix in ["NASDAQ", "NYSE"]:
                k = f"{prefix}:{pos['symbol']}"
                if k in state:
                    t = state[k]
                    pos["tp"] = t.get("tp")
                    pos["sl"] = t.get("sl")
                    if t.get("open_time"):
                        try:
                            opened = datetime.fromisoformat(t["open_time"])
                            pos["age_days"] = (datetime.now() - opened).days
                        except Exception:
                            pass
                    break
    except Exception as e:
        log.warning(f"Could not enrich Alpaca positions from state: {e}")

    return positions


def fetch_hyperliquid_positions() -> list[dict]:
    positions = []
    try:
        d = _hl_request({"type": "clearinghouseState", "user": HL_ADDRESS})
        for ap in d.get("assetPositions", []):
            p   = ap["position"]
            sz  = float(p["szi"])
            if sz == 0:
                continue
            entry   = float(p["entryPx"])
            upnl    = float(p["unrealizedPnl"])
            lev     = p.get("leverage", {}).get("value", 1)
            margin  = abs(sz) * entry / lev if lev else 0
            upnl_pct = (upnl / margin * 100) if margin else 0
            positions.append({
                "broker":   "Hyperliquid",
                "symbol":   p["coin"],
                "side":     "SHORT" if sz < 0 else "LONG",
                "size":     abs(sz),
                "entry":    entry,
                "current":  None,  # will fill from markPx if available
                "upnl":     upnl,
                "upnl_pct": upnl_pct,
                "tp":       None,
                "sl":       None,
                "age_days": None,
                "liq":      p.get("liquidationPx", "N/A"),
            })
        # get mark prices
        meta = _hl_request({"type": "allMids"})
        for pos in positions:
            pos["current"] = float(meta.get(pos["symbol"], pos["entry"]))
    except Exception as e:
        log.warning(f"Hyperliquid fetch failed: {e}")
    return positions


def fetch_oanda_positions() -> list[dict]:
    positions = []
    try:
        raw = _oanda_request(f"/accounts/{OANDA_ACCOUNT}/openPositions")
        prices_raw = _oanda_request(f"/accounts/{OANDA_ACCOUNT}/pricing?instruments=" +
                                    ",".join(p["instrument"] for p in raw.get("positions", [])))
        price_map = {p["instrument"]: (float(p["bids"][0]["price"]) + float(p["asks"][0]["price"])) / 2
                     for p in prices_raw.get("prices", [])}

        for p in raw.get("positions", []):
            instr = p["instrument"]
            mid   = price_map.get(instr, 0)
            for side_key, sign in [("long", 1), ("short", -1)]:
                s = p.get(side_key, {})
                units = int(s.get("units", 0))
                if units == 0:
                    continue
                entry   = float(s.get("averagePrice", 0))
                upnl    = float(s.get("unrealizedPL", 0))
                upnl_pct = ((mid - entry) / entry * 100 * sign) if entry else 0
                positions.append({
                    "broker":   "Oanda",
                    "symbol":   instr.replace("_", "/"),
                    "side":     side_key.upper(),
                    "size":     abs(units),
                    "entry":    entry,
                    "current":  mid,
                    "upnl":     upnl,
                    "upnl_pct": upnl_pct,
                    "tp":       None,
                    "sl":       None,
                    "age_days": None,
                })
    except Exception as e:
        log.warning(f"Oanda fetch failed: {e}")
    return positions


# ── PDF generation ────────────────────────────────────────────────────────────

def _pnl_color(upnl: float) -> colors.Color:
    return PROFIT_GRN if upnl >= 0 else LOSS_RED


def generate_pdf(all_positions: list[dict], pdf_path: str) -> str:
    doc    = SimpleDocTemplate(pdf_path, pagesize=A4,
                               leftMargin=12*mm, rightMargin=12*mm,
                               topMargin=12*mm, bottomMargin=12*mm)
    styles = getSampleStyleSheet()
    h1     = ParagraphStyle("h1", parent=styles["Title"],   fontSize=15, textColor=DARK_BLUE, spaceAfter=2)
    sub    = ParagraphStyle("sub", parent=styles["Normal"], fontSize=8,  textColor=colors.grey, spaceAfter=10)
    h2     = ParagraphStyle("h2", parent=styles["Heading2"],fontSize=10, textColor=DARK_BLUE, spaceBefore=10, spaceAfter=5)
    story  = []

    now_pt = datetime.now().strftime("%Y-%m-%d %H:%M PT")
    story.append(Paragraph("Morning Position Report", h1))
    story.append(Paragraph(f"{now_pt}  ·  {len(all_positions)} open positions", sub))

    # ── Summary totals ────────────────────────────────────────────────────────
    total_upnl = sum(p["upnl"] for p in all_positions)
    brokers    = {}
    for p in all_positions:
        brokers.setdefault(p["broker"], {"count": 0, "upnl": 0.0})
        brokers[p["broker"]]["count"] += 1
        brokers[p["broker"]]["upnl"]  += p["upnl"]

    summary_rows = [["Broker", "Positions", "Unrealized PnL"]]
    for broker, v in brokers.items():
        summary_rows.append([broker, str(v["count"]), f"${v['upnl']:+,.2f}"])
    summary_rows.append(["TOTAL", str(len(all_positions)), f"${total_upnl:+,.2f}"])

    st = Table(summary_rows, colWidths=[100, 80, 110])
    st.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), DARK_BLUE),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 9),
        ("ALIGN",         (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN",         (0, 0), (0, -1), "LEFT"),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.grey),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("FONTNAME",      (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND",    (0, -1), (-1, -1), _pnl_color(total_upnl)),
    ]))
    story.append(st)
    story.append(Spacer(1, 8))

    # ── Per-broker position tables ─────────────────────────────────────────────
    col_w = [55, 42, 52, 52, 52, 52, 52, 45, 38]
    headers = ["Symbol", "Side", "Entry", "Current", "uPnL $", "uPnL %", "TP", "SL", "Age"]

    def _broker_section(broker: str, positions: list[dict]):
        story.append(Paragraph(f"{broker}", h2))
        rows = [headers]
        row_styles = []
        for i, p in enumerate(positions, 1):
            tp_str  = f"{p['tp']:.4f}" if p.get("tp") else "—"
            sl_str  = f"{p['sl']:.4f}" if p.get("sl") else "—"
            age_str = f"{p['age_days']}d" if p.get("age_days") is not None else "—"
            cur_str = f"{p['current']:.4f}" if p.get("current") else "—"
            rows.append([
                p["symbol"],
                p["side"],
                f"{p['entry']:.4f}",
                cur_str,
                f"${p['upnl']:+,.2f}",
                f"{p['upnl_pct']:+.1f}%",
                tp_str,
                sl_str,
                age_str,
            ])
            bg = _pnl_color(p["upnl"]) if i % 2 == 0 else (PROFIT_GRN if p["upnl"] >= 0 else colors.HexColor("#fce8ea"))
            row_styles.append(("BACKGROUND", (4, i), (5, i), _pnl_color(p["upnl"])))

        t = Table(rows, colWidths=col_w)
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
        for rs in row_styles:
            ts.add(*rs)
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 6))

    for broker in ["Alpaca", "Hyperliquid", "Oanda"]:
        subset = [p for p in all_positions if p["broker"] == broker]
        if subset:
            _broker_section(broker, subset)

    doc.build(story)
    log.info(f"PDF generated: {pdf_path}")
    return pdf_path


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_telegram(pdf_path: str, total_upnl: float, count: int):
    if not TELEGRAM_TOKEN:
        log.warning("TELEGRAM_TOKEN not set")
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    sign    = "📈" if total_upnl >= 0 else "📉"
    caption = (f"{sign} Morning Position Report — {datetime.now().strftime('%Y-%m-%d')}\n"
               f"{count} open positions  |  Net uPnL: ${total_upnl:+,.2f}")
    with open(pdf_path, "rb") as f:
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                             files={"document": f})
    if resp.json().get("ok"):
        log.info("✅ Morning report sent to Telegram!")
    else:
        log.error(f"Telegram error: {resp.json()}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log.info("=== Morning Position Report ===")

    alpaca_pos = fetch_alpaca_positions()
    hl_pos     = fetch_hyperliquid_positions()
    oanda_pos  = fetch_oanda_positions()

    all_pos = alpaca_pos + hl_pos + oanda_pos
    log.info(f"Alpaca: {len(alpaca_pos)}  Hyperliquid: {len(hl_pos)}  Oanda: {len(oanda_pos)}")

    if not all_pos:
        log.warning("No open positions found.")
        return

    total_upnl = sum(p["upnl"] for p in all_pos)
    log.info(f"Total unrealized PnL: ${total_upnl:+,.2f}")

    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    pdf_path = str(OUTPUT_DIR / f"morning_report_{ts}.pdf")
    generate_pdf(all_pos, pdf_path)
    send_to_telegram(pdf_path, total_upnl, len(all_pos))


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    main()
