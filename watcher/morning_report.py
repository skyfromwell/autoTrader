#!/usr/bin/env python3
from __future__ import annotations
"""
Morning Position Report — runs 8am PT Mon-Fri
Fetches all open positions from Alpaca, Hyperliquid, and Oanda,
adds S/R analysis, generates a PDF, sends to Telegram.
"""

import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yfinance as yf
import pandas as pd
from dotenv import load_dotenv
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Table,
                                TableStyle, Spacer, HRFlowable)
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
WARN_AMBER = colors.HexColor("#fff3cd")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _alpaca_request(path: str) -> dict:
    req = urllib.request.Request(
        f"https://paper-api.alpaca.markets/v2{path}",
        headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _oanda_request(path: str) -> dict:
    req = urllib.request.Request(
        f"{OANDA_BASE}{path}",
        headers={"Authorization": f"Bearer {OANDA_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _hl_request(payload: dict) -> dict:
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


# ── Support / Resistance analysis ─────────────────────────────────────────────

def _swing_levels(highs: pd.Series, lows: pd.Series, window: int = 5) -> tuple[list, list]:
    """Return lists of swing-high and swing-low prices using local extrema."""
    swing_highs, swing_lows = [], []
    for i in range(window, len(highs) - window):
        if highs.iloc[i] == highs.iloc[i-window:i+window+1].max():
            swing_highs.append(float(highs.iloc[i]))
        if lows.iloc[i] == lows.iloc[i-window:i+window+1].min():
            swing_lows.append(float(lows.iloc[i]))
    return swing_highs, swing_lows


def _nearest(levels: list[float], price: float, above: bool) -> float | None:
    candidates = [l for l in levels if (l > price if above else l < price)]
    if not candidates:
        return None
    return min(candidates) if above else max(candidates)


def _fetch_ohlcv_stock(symbol: str, days: int = 60) -> pd.DataFrame | None:
    try:
        df = yf.download(symbol, period=f"{days}d", progress=False, auto_adjust=True)
        return df if not df.empty else None
    except Exception:
        return None


def _fetch_ohlcv_hl(coin: str, days: int = 30) -> pd.DataFrame | None:
    try:
        end_ms   = int(datetime.utcnow().timestamp() * 1000)
        start_ms = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
        resp = requests.post("https://api.hyperliquid.xyz/info", json={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1d",
                    "startTime": start_ms, "endTime": end_ms},
        }, timeout=10)
        candles = resp.json()
        if not candles:
            return None
        df = pd.DataFrame(candles)
        df["High"] = df["h"].astype(float)
        df["Low"]  = df["l"].astype(float)
        df["Close"]= df["c"].astype(float)
        return df
    except Exception:
        return None


def _fetch_ohlcv_oanda(instrument: str, days: int = 30) -> pd.DataFrame | None:
    try:
        instr = instrument.replace("/", "_")
        raw   = _oanda_request(
            f"/instruments/{instr}/candles?count=60&granularity=D&price=M"
        )
        rows = []
        for c in raw.get("candles", []):
            m = c.get("mid", {})
            rows.append({"High": float(m["h"]), "Low": float(m["l"]), "Close": float(m["c"])})
        return pd.DataFrame(rows) if rows else None
    except Exception:
        return None


def analyse_sr(pos: dict) -> dict:
    """
    Fetch OHLCV, compute swing S/R, max excursion, TP distance, analysis note.
    Returns dict with keys: support, resistance, max_excursion, tp_dist_pct,
                             sl_dist_pct, note.
    """
    broker  = pos["broker"]
    symbol  = pos["symbol"]
    entry   = pos["entry"]
    current = pos.get("current") or entry
    side    = pos["side"]
    tp      = pos.get("tp")
    sl      = pos.get("sl")

    df = None
    if broker == "Alpaca":
        df = _fetch_ohlcv_stock(symbol)
    elif broker == "Hyperliquid":
        df = _fetch_ohlcv_hl(symbol)
    elif broker == "Oanda":
        df = _fetch_ohlcv_oanda(symbol)

    result = {"support": None, "resistance": None,
              "max_excursion": None, "tp_dist_pct": None,
              "sl_dist_pct": None, "note": ""}

    if df is not None and len(df) >= 15:
        highs, lows = df["High"].squeeze(), df["Low"].squeeze()
        swing_h, swing_l = _swing_levels(highs, lows)

        result["support"]    = _nearest(swing_l, current, above=False)
        result["resistance"] = _nearest(swing_h, current, above=True)

        # Max favorable excursion: best price reached since entry
        if side == "LONG":
            result["max_excursion"] = float(highs.iloc[-pos.get("age_days", 30):].max()) \
                if pos.get("age_days") else float(highs.tail(10).max())
        else:
            result["max_excursion"] = float(lows.iloc[-pos.get("age_days", 30):].min()) \
                if pos.get("age_days") else float(lows.tail(10).min())

    # TP / SL distances
    if tp and current:
        result["tp_dist_pct"] = (tp - current) / current * 100 * (1 if side == "LONG" else -1)
    if sl and current:
        result["sl_dist_pct"] = (current - sl) / current * 100 * (1 if side == "LONG" else -1)

    # Analysis note
    notes = []
    tp_d  = result["tp_dist_pct"]
    sl_d  = result["sl_dist_pct"]
    age   = pos.get("age_days")
    supp  = result["support"]
    res   = result["resistance"]

    if tp_d is not None:
        if abs(tp_d) < 2:
            notes.append(f"TP very close ({tp_d:+.1f}%) — consider taking profit")
        elif abs(tp_d) > 20 and age and age > 5:
            notes.append(f"TP far ({tp_d:+.1f}%) after {age}d — consider tightening")
        else:
            notes.append(f"{tp_d:+.1f}% to TP")

    if sl_d is not None and sl_d < 3:
        notes.append(f"⚠️ Only {sl_d:.1f}% from SL")

    if supp and res and current:
        sr_range = res - supp
        pos_in_range = (current - supp) / sr_range * 100 if sr_range > 0 else 50
        if side == "LONG" and pos_in_range > 75:
            notes.append(f"Near resistance ({res:.4f}) — momentum may stall")
        elif side == "SHORT" and pos_in_range < 25:
            notes.append(f"Near support ({supp:.4f}) — short may face buying pressure")

    if age:
        notes.append(f"Open {age}d")

    result["note"] = " · ".join(notes) if notes else "—"
    return result


# ── Position fetchers ─────────────────────────────────────────────────────────

def fetch_alpaca_positions() -> list[dict]:
    positions = []
    try:
        raw = _alpaca_request("/positions")
        for p in raw:
            positions.append({
                "broker":    "Alpaca",
                "symbol":    p["symbol"],
                "side":      p["side"].upper(),
                "size":      abs(float(p["qty"])),
                "entry":     float(p["avg_entry_price"]),
                "current":   float(p["current_price"]),
                "upnl":      float(p["unrealized_pl"]),
                "upnl_pct":  float(p["unrealized_plpc"]) * 100,
                "tp": None, "sl": None, "age_days": None,
            })
    except Exception as e:
        log.warning(f"Alpaca fetch failed: {e}")

    try:
        state = json.loads(STATE_FILE.read_text()).get("open_trades", {})
        for pos in positions:
            for prefix in ["NASDAQ", "NYSE"]:
                k = f"{prefix}:{pos['symbol']}"
                if k in state:
                    t = state[k]
                    pos["tp"] = t.get("tp")
                    pos["sl"] = t.get("sl")
                    if t.get("open_time"):
                        try:
                            pos["age_days"] = (datetime.now() -
                                               datetime.fromisoformat(t["open_time"])).days
                        except Exception:
                            pass
                    break
    except Exception as e:
        log.warning(f"State enrich failed: {e}")

    return positions


def fetch_hyperliquid_positions() -> list[dict]:
    positions = []
    try:
        d = _hl_request({"type": "clearinghouseState", "user": HL_ADDRESS})
        for ap in d.get("assetPositions", []):
            p  = ap["position"]
            sz = float(p["szi"])
            if sz == 0:
                continue
            entry = float(p["entryPx"])
            upnl  = float(p["unrealizedPnl"])
            lev   = p.get("leverage", {}).get("value", 1)
            margin = abs(sz) * entry / lev if lev else 0
            positions.append({
                "broker":   "Hyperliquid",
                "symbol":   p["coin"],
                "side":     "SHORT" if sz < 0 else "LONG",
                "size":     abs(sz),
                "entry":    entry,
                "current":  None,
                "upnl":     upnl,
                "upnl_pct": (upnl / margin * 100) if margin else 0,
                "tp": None, "sl": None, "age_days": None,
                "liq": p.get("liquidationPx", "N/A"),
            })
        mids = _hl_request({"type": "allMids"})
        for pos in positions:
            pos["current"] = float(mids.get(pos["symbol"], pos["entry"]))
    except Exception as e:
        log.warning(f"Hyperliquid fetch failed: {e}")
    return positions


def fetch_oanda_positions() -> list[dict]:
    positions = []
    try:
        raw = _oanda_request(f"/accounts/{OANDA_ACCOUNT}/openPositions")
        instruments = [p["instrument"] for p in raw.get("positions", [])]
        if not instruments:
            return []
        prices_raw = _oanda_request(
            f"/accounts/{OANDA_ACCOUNT}/pricing?instruments={','.join(instruments)}")
        price_map = {
            p["instrument"]: (float(p["bids"][0]["price"]) + float(p["asks"][0]["price"])) / 2
            for p in prices_raw.get("prices", [])
        }
        for p in raw.get("positions", []):
            instr = p["instrument"]
            mid   = price_map.get(instr, 0)
            for side_key, sign in [("long", 1), ("short", -1)]:
                units = int(p.get(side_key, {}).get("units", 0))
                if units == 0:
                    continue
                entry = float(p[side_key].get("averagePrice", 0))
                positions.append({
                    "broker":   "Oanda",
                    "symbol":   instr.replace("_", "/"),
                    "side":     side_key.upper(),
                    "size":     abs(units),
                    "entry":    entry,
                    "current":  mid,
                    "upnl":     float(p[side_key].get("unrealizedPL", 0)),
                    "upnl_pct": ((mid - entry) / entry * 100 * sign) if entry else 0,
                    "tp": None, "sl": None, "age_days": None,
                })
    except Exception as e:
        log.warning(f"Oanda fetch failed: {e}")
    return positions


# ── PDF generation ────────────────────────────────────────────────────────────

def _pnl_color(v: float) -> colors.Color:
    return PROFIT_GRN if v >= 0 else LOSS_RED


def generate_pdf(all_positions: list[dict], analyses: dict, pdf_path: str) -> str:
    doc    = SimpleDocTemplate(pdf_path, pagesize=A4,
                               leftMargin=12*mm, rightMargin=12*mm,
                               topMargin=12*mm, bottomMargin=12*mm)
    styles = getSampleStyleSheet()
    h1   = ParagraphStyle("h1",  parent=styles["Title"],   fontSize=15, textColor=DARK_BLUE, spaceAfter=2)
    sub  = ParagraphStyle("sub", parent=styles["Normal"],  fontSize=8,  textColor=colors.grey, spaceAfter=10)
    h2   = ParagraphStyle("h2",  parent=styles["Heading2"],fontSize=10, textColor=DARK_BLUE, spaceBefore=8, spaceAfter=4)
    note = ParagraphStyle("note",parent=styles["Normal"],  fontSize=7,  textColor=colors.HexColor("#444444"), leading=10)
    story = []

    now_pt = datetime.now().strftime("%Y-%m-%d %H:%M PT")
    story.append(Paragraph("Morning Position Report", h1))
    story.append(Paragraph(f"{now_pt}  ·  {len(all_positions)} open positions", sub))

    # ── Summary ───────────────────────────────────────────────────────────────
    total_upnl = sum(p["upnl"] for p in all_positions)
    brokers = {}
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
    story.append(Spacer(1, 10))

    # ── Per-broker sections ───────────────────────────────────────────────────
    pos_col_w = [45, 38, 48, 48, 46, 44, 44, 42]
    pos_hdr   = ["Symbol", "Side", "Entry", "Current", "uPnL $", "uPnL %", "TP", "SL"]
    sr_col_w  = [45, 52, 52, 52, 52, 90]
    sr_hdr    = ["Symbol", "Support", "Resistance", "MaxExcur", "→TP %", "Analysis"]

    def _section(broker: str, positions: list[dict]):
        story.append(Paragraph(broker, h2))

        # Position table
        rows = [pos_hdr]
        row_colors = []
        for i, p in enumerate(positions, 1):
            rows.append([
                p["symbol"],
                p["side"],
                f"{p['entry']:.4f}",
                f"{p['current']:.4f}" if p.get("current") else "—",
                f"${p['upnl']:+,.2f}",
                f"{p['upnl_pct']:+.1f}%",
                f"{p['tp']:.4f}" if p.get("tp") else "—",
                f"{p['sl']:.4f}" if p.get("sl") else "—",
            ])
            row_colors.append(("BACKGROUND", (4, i), (5, i), _pnl_color(p["upnl"])))

        t = Table(rows, colWidths=pos_col_w)
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
        story.append(Spacer(1, 4))

        # S/R analysis table
        sr_rows = [sr_hdr]
        sr_row_colors = []
        for i, p in enumerate(positions, 1):
            a = analyses.get(f"{broker}:{p['symbol']}", {})
            supp = f"{a['support']:.4f}"   if a.get("support")    else "—"
            res  = f"{a['resistance']:.4f}" if a.get("resistance") else "—"
            mx   = f"{a['max_excursion']:.4f}" if a.get("max_excursion") else "—"
            tp_d = f"{a['tp_dist_pct']:+.1f}%" if a.get("tp_dist_pct") is not None else "—"
            n    = a.get("note", "—")
            sr_rows.append([p["symbol"], supp, res, mx, tp_d, n])
            # Amber if TP dist is tight (< 2%) or note has warning
            if a.get("tp_dist_pct") is not None and abs(a["tp_dist_pct"]) < 2:
                sr_row_colors.append(("BACKGROUND", (0, i), (-1, i), WARN_AMBER))
            elif "⚠️" in n:
                sr_row_colors.append(("BACKGROUND", (0, i), (-1, i), LOSS_RED))

        sr_t = Table(sr_rows, colWidths=sr_col_w)
        sr_ts = TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#2e5984")),
            ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
            ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 7),
            ("GRID",          (0, 0), (-1, -1), 0.4, colors.grey),
            ("ALIGN",         (1, 0), (-1, -1), "RIGHT"),
            ("ALIGN",         (0, 0), (0, -1), "LEFT"),
            ("ALIGN",         (5, 0), (5, -1), "LEFT"),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, ALT_ROW]),
        ])
        for rc in sr_row_colors:
            sr_ts.add(*rc)
        sr_t.setStyle(sr_ts)
        story.append(sr_t)
        story.append(Spacer(1, 8))

    for broker in ["Alpaca", "Hyperliquid", "Oanda"]:
        subset = [p for p in all_positions if p["broker"] == broker]
        if subset:
            _section(broker, subset)

    doc.build(story)
    log.info(f"PDF generated: {pdf_path}")
    return pdf_path


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_to_telegram(pdf_path: str, total_upnl: float, count: int):
    if not TELEGRAM_TOKEN:
        return
    sign    = "📈" if total_upnl >= 0 else "📉"
    caption = (f"{sign} Morning Position Report — {datetime.now().strftime('%Y-%m-%d')}\n"
               f"{count} open positions  |  Net uPnL: ${total_upnl:+,.2f}")
    with open(pdf_path, "rb") as f:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files={"document": f},
        )
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
    all_pos    = alpaca_pos + hl_pos + oanda_pos

    log.info(f"Alpaca: {len(alpaca_pos)}  Hyperliquid: {len(hl_pos)}  Oanda: {len(oanda_pos)}")
    if not all_pos:
        log.warning("No open positions found.")
        return

    log.info("Fetching S/R analysis...")
    analyses = {}
    for pos in all_pos:
        key = f"{pos['broker']}:{pos['symbol']}"
        try:
            analyses[key] = analyse_sr(pos)
            log.info(f"  {key}: support={analyses[key].get('support')}  "
                     f"resistance={analyses[key].get('resistance')}  "
                     f"note={analyses[key].get('note')}")
        except Exception as e:
            log.warning(f"  {key}: S/R failed — {e}")
            analyses[key] = {}

    total_upnl = sum(p["upnl"] for p in all_pos)
    log.info(f"Total unrealized PnL: ${total_upnl:+,.2f}")

    ts       = datetime.now().strftime("%Y%m%d_%H%M")
    pdf_path = str(OUTPUT_DIR / f"morning_report_{ts}.pdf")
    generate_pdf(all_pos, analyses, pdf_path)
    send_to_telegram(pdf_path, total_upnl, len(all_pos))


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    main()
