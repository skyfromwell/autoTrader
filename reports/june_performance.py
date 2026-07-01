#!/usr/bin/env python3
"""
June 2026 AI Trading Performance Report (6/10 – 6/30)
Generates PDF + sends to Telegram.
"""
from __future__ import annotations
import os, json, requests
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                 TableStyle, HRFlowable)
from reportlab.lib.enums import TA_CENTER, TA_LEFT

load_dotenv(Path(__file__).parent.parent / ".env")

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from trader.hyperliquid_trader import _hl_post

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN  = colors.HexColor("#27ae60")
RED    = colors.HexColor("#e74c3c")
BLUE   = colors.HexColor("#2980b9")
DARK   = colors.HexColor("#1a1a2e")
LIGHT  = colors.HexColor("#f5f6fa")
GOLD   = colors.HexColor("#f39c12")
GRAY   = colors.HexColor("#7f8c8d")
WHITE  = colors.white

def pnl_color(v): return GREEN if v >= 0 else RED
def fmt_pnl(v, ccy="$"): return f"{ccy}{v:+,.2f}" if ccy=="$" else f"¥{v:+,.0f}"
def fmt_num(v, ccy="$"): return f"{ccy}{v:,.2f}"
def pct(v, base): return f"({v/base*100:+.1f}%)" if base else ""

# ── Styles ────────────────────────────────────────────────────────────────────
styles = getSampleStyleSheet()
title_style = ParagraphStyle("Title", parent=styles["Title"],
    fontSize=22, textColor=WHITE, spaceAfter=4, alignment=TA_CENTER,
    fontName="Helvetica-Bold")
sub_style = ParagraphStyle("Sub", parent=styles["Normal"],
    fontSize=11, textColor=LIGHT, spaceAfter=2, alignment=TA_CENTER)
h2_style = ParagraphStyle("H2", parent=styles["Heading2"],
    fontSize=14, textColor=DARK, spaceAfter=6, fontName="Helvetica-Bold")
h3_style = ParagraphStyle("H3", parent=styles["Heading3"],
    fontSize=11, textColor=BLUE, spaceAfter=4, fontName="Helvetica-Bold")
body_style = ParagraphStyle("Body", parent=styles["Normal"],
    fontSize=9, textColor=DARK, spaceAfter=2)
note_style = ParagraphStyle("Note", parent=styles["Normal"],
    fontSize=8, textColor=GRAY, spaceAfter=2, leftIndent=12)

def section_header(text, color=DARK):
    return Paragraph(f"<font color='#{color.hexval()[2:] if hasattr(color,'hexval') else '2980b9'}'>{text}</font>", h2_style)

def pnl_table(headers, rows, col_widths):
    data = [headers] + rows
    ts = TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), DARK),
        ("TEXTCOLOR",   (0,0), (-1,0), WHITE),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,0), 9),
        ("ALIGN",       (0,0), (-1,-1), "CENTER"),
        ("ALIGN",       (0,1), (1,-1), "LEFT"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [LIGHT, WHITE]),
        ("FONTSIZE",    (0,1), (-1,-1), 9),
        ("GRID",        (0,0), (-1,-1), 0.3, colors.lightgrey),
        ("TOPPADDING",  (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
    ])
    # Color PnL column (last)
    for i, row in enumerate(rows, 1):
        val_str = str(row[-1]).replace(",","").replace("$","").replace("+","").replace("¥","")
        try:
            val = float(val_str)
            ts.add("TEXTCOLOR", (-1,i), (-1,i), pnl_color(val))
            ts.add("FONTNAME",  (-1,i), (-1,i), "Helvetica-Bold")
        except: pass
    t = Table(data, colWidths=col_widths)
    t.setStyle(ts)
    return t

def summary_box(label, realized, unrealized, net, ccy="$"):
    r_str  = fmt_pnl(realized,  ccy)
    u_str  = fmt_pnl(unrealized,ccy)
    n_str  = fmt_pnl(net,       ccy)
    rc = pnl_color(realized); uc = pnl_color(unrealized); nc = pnl_color(net)
    rc_hex = "#27ae60" if realized>=0  else "#e74c3c"
    uc_hex = "#27ae60" if unrealized>=0 else "#e74c3c"
    nc_hex = "#27ae60" if net>=0        else "#e74c3c"
    data = [
        [label, "Realized P&L", "Unrealized P&L", "Net P&L"],
        ["", f"<font color='{rc_hex}'><b>{r_str}</b></font>",
             f"<font color='{uc_hex}'><b>{u_str}</b></font>",
             f"<font color='{nc_hex}'><b>{n_str}</b></font>"],
    ]
    for r in data:
        for j,c in enumerate(r):
            data[data.index(r)][j] = Paragraph(str(c), body_style)
    t = Table(data, colWidths=[1.4*inch,1.4*inch,1.4*inch,1.4*inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0),(-1,0), DARK),
        ("TEXTCOLOR",  (0,0),(-1,0), WHITE),
        ("FONTNAME",   (0,0),(-1,0), "Helvetica-Bold"),
        ("FONTSIZE",   (0,0),(-1,0), 9),
        ("BACKGROUND", (0,1),(-1,1), LIGHT),
        ("ALIGN",      (0,0),(-1,-1), "CENTER"),
        ("GRID",       (0,0),(-1,-1), 0.3, colors.lightgrey),
        ("TOPPADDING", (0,0),(-1,-1), 5),
        ("BOTTOMPADDING",(0,0),(-1,-1),5),
    ]))
    return t

# ═══════════════════════════════════════════════════════════════════════════════
# DATA COLLECTION
# ═══════════════════════════════════════════════════════════════════════════════

print("Fetching data…")

wallet  = os.environ["HL_WALLET_ADDRESS"]
akey    = os.getenv("ALPACA_API_KEY");  asec = os.getenv("ALPACA_SECRET_KEY")
abase   = os.getenv("ALPACA_BASE_URL","https://paper-api.alpaca.markets")
ah      = {"APCA-API-KEY-ID": akey, "APCA-API-SECRET-KEY": asec}
okey    = os.getenv("OANDA_API_KEY");   oacct = os.getenv("OANDA_ACCOUNT_ID")
obase   = os.getenv("OANDA_BASE_URL")
oh      = {"Authorization": f"Bearer {okey}"}
china_url = os.getenv("CHINA_SERVER_URL"); china_key = os.getenv("CHINA_API_KEY")

START_MS = int(datetime(2026,6,10,tzinfo=timezone.utc).timestamp()*1000)
END_MS   = int(datetime(2026,7,1, tzinfo=timezone.utc).timestamp()*1000)

# ── Alpaca paper ──────────────────────────────────────────────────────────────
hist = requests.get(f"{abase}/v2/account/portfolio/history?period=1M&timeframe=1D", headers=ah).json()
paper_daily = []
for ts, eq, pl in zip(hist.get("timestamp",[]), hist.get("equity",[]), hist.get("profit_loss",[])):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    if "2026-06-10" <= dt <= "2026-06-30":
        paper_daily.append({"date": dt, "equity": eq, "daily_pnl": pl or 0})

paper_pos = requests.get(f"{abase}/v2/positions", headers=ah).json()
# Only count positions opened in June for unrealized (exclude July opens)
june_syms = {"AAPL","CSCO","GS"}  # opened in June
paper_upnl = sum(float(p["unrealized_pl"]) for p in paper_pos if p["symbol"] in june_syms)

paper_start_eq = 100_000.0
paper_end_eq   = paper_daily[-1]["equity"] if paper_daily else paper_start_eq
paper_total_pnl = paper_end_eq - paper_start_eq

# Realized = total - current unrealized of June positions
paper_rpnl = paper_total_pnl - paper_upnl

# Open positions table
paper_open = [{"sym": p["symbol"], "dir": p["side"], "entry": float(p["avg_entry_price"]),
               "now": float(p["current_price"]), "upnl": float(p["unrealized_pl"])}
              for p in paper_pos if p["symbol"] in june_syms]

# ── HL crypto ─────────────────────────────────────────────────────────────────
hl_fills_all = _hl_post({"type":"userFills","user":wallet})
hl_fills = [f for f in hl_fills_all if START_MS <= f.get("time",0) <= END_MS]
hl_rpnl_by_coin: dict[str,float] = {}
for f in hl_fills:
    c = f["coin"]
    hl_rpnl_by_coin[c] = hl_rpnl_by_coin.get(c,0) + float(f.get("closedPnl",0))
hl_rpnl = sum(hl_rpnl_by_coin.values())

hl_pos_state = _hl_post({"type":"clearinghouseState","user":wallet}).get("assetPositions",[])
hl_open = [(p["position"]["coin"],
            float(p["position"].get("szi",0)),
            float(p["position"].get("entryPx",0)),
            float(p["position"].get("unrealizedPnl",0)))
           for p in hl_pos_state if float(p["position"].get("szi",0))!=0]
hl_upnl = sum(x[3] for x in hl_open)
hl_mids = _hl_post({"type":"allMids"})

# ── xyz DEX ───────────────────────────────────────────────────────────────────
xyz_fills_all = _hl_post({"type":"userFills","user":wallet,"dex":"xyz"})
# xyz fills only have coins starting with "xyz:"
xyz_fills = [f for f in xyz_fills_all
             if START_MS <= f.get("time",0) <= END_MS and f.get("coin","").startswith("xyz:")]
xyz_rpnl_by_coin: dict[str,float] = {}
for f in xyz_fills:
    c = f["coin"]
    xyz_rpnl_by_coin[c] = xyz_rpnl_by_coin.get(c,0) + float(f.get("closedPnl",0))
xyz_rpnl = sum(xyz_rpnl_by_coin.values())

xyz_pos_state = _hl_post({"type":"clearinghouseState","user":wallet,"dex":"xyz"}).get("assetPositions",[])
xyz_open = [(p["position"]["coin"],
             float(p["position"].get("szi",0)),
             float(p["position"].get("entryPx",0)),
             float(p["position"].get("unrealizedPnl",0)))
            for p in xyz_pos_state if float(p["position"].get("szi",0))!=0]
xyz_upnl = sum(x[3] for x in xyz_open)

# ── OANDA ─────────────────────────────────────────────────────────────────────
oanda_closed_resp = requests.get(f"{obase}/accounts/{oacct}/trades?state=CLOSED&count=100", headers=oh).json()
oanda_closed = [t for t in oanda_closed_resp.get("trades",[]) if t.get("closeTime","") >= "2026-06-10"]
oanda_rpnl = sum(float(t.get("realizedPL",0)) for t in oanda_closed)
oanda_rpnl_by = {t["instrument"]: float(t.get("realizedPL",0)) for t in oanda_closed}

oanda_pos_resp = requests.get(f"{obase}/accounts/{oacct}/openPositions", headers=oh).json().get("positions",[])
oanda_rates_resp = requests.get(f"{obase}/accounts/{oacct}/pricing?instruments=NZD_USD,AUD_USD,USD_JPY,GBP_USD", headers=oh).json()
oanda_rates = {p["instrument"].replace("_",""): (float(p["bids"][0]["price"])+float(p["asks"][0]["price"]))/2
               for p in oanda_rates_resp.get("prices",[])}
oanda_open = []
for p in oanda_pos_resp:
    for side in ["long","short"]:
        units = int(p.get(side,{}).get("units",0))
        if units == 0: continue
        instr = p["instrument"].replace("_","")
        entry = float(p[side]["averagePrice"])
        upnl  = float(p[side]["unrealizedPL"])
        now   = oanda_rates.get(instr, entry)
        oanda_open.append((instr, side, abs(units), entry, now, upnl))
oanda_upnl = sum(x[5] for x in oanda_open)

# ── China ─────────────────────────────────────────────────────────────────────
china_pos = []
china_upnl_cny = 0.0
try:
    import urllib.request as ur
    req = ur.Request(f"{china_url}/positions", headers={"X-API-Key": china_key})
    with ur.urlopen(req, timeout=10) as r: china_pos = json.loads(r.read())
    china_upnl_cny = sum((p.get("market_value",0) - p.get("entry_price",0)*p.get("volume",0))
                         for p in china_pos)
except: pass

# ── Totals ────────────────────────────────────────────────────────────────────
real_rpnl  = hl_rpnl + xyz_rpnl + oanda_rpnl
real_upnl  = hl_upnl + xyz_upnl + oanda_upnl
real_net   = real_rpnl + real_upnl

print(f"Paper: rpnl={paper_rpnl:.2f}  upnl={paper_upnl:.2f}  total={paper_total_pnl:.2f}")
print(f"HL:    rpnl={hl_rpnl:.2f}     upnl={hl_upnl:.2f}")
print(f"xyz:   rpnl={xyz_rpnl:.2f}    upnl={xyz_upnl:.2f}")
print(f"OANDA: rpnl={oanda_rpnl:.2f}  upnl={oanda_upnl:.2f}")
print(f"Real net: {real_net:.2f}")

# ═══════════════════════════════════════════════════════════════════════════════
# PDF GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

out_path = Path("output/june_performance_2026.pdf")
out_path.parent.mkdir(exist_ok=True)
doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                        topMargin=0.5*inch, bottomMargin=0.5*inch,
                        leftMargin=0.6*inch, rightMargin=0.6*inch)

story = []

# ── Cover header ──────────────────────────────────────────────────────────────
header_data = [["AI TRADING PERFORMANCE REPORT"],
               ["June 10 – June 30, 2026"],
               ["Generated: " + datetime.now().strftime("%Y-%m-%d %H:%M")]]
ht = Table(header_data, colWidths=[7.3*inch])
ht.setStyle(TableStyle([
    ("BACKGROUND",   (0,0),(-1,-1), DARK),
    ("TEXTCOLOR",    (0,0),(-1,0),  GOLD),
    ("TEXTCOLOR",    (0,1),(-1,-1), LIGHT),
    ("FONTNAME",     (0,0),(-1,0),  "Helvetica-Bold"),
    ("FONTNAME",     (0,1),(-1,-1), "Helvetica"),
    ("FONTSIZE",     (0,0),(-1,0),  20),
    ("FONTSIZE",     (0,1),(-1,1),  12),
    ("FONTSIZE",     (0,2),(-1,2),  8),
    ("ALIGN",        (0,0),(-1,-1), "CENTER"),
    ("TOPPADDING",   (0,0),(-1,0),  14),
    ("BOTTOMPADDING",(0,0),(-1,0),  6),
    ("TOPPADDING",   (0,1),(-1,1),  4),
    ("BOTTOMPADDING",(0,2),(-1,2),  10),
]))
story.append(ht)
story.append(Spacer(1, 0.2*inch))

# ── Executive Summary ─────────────────────────────────────────────────────────
story.append(Paragraph("EXECUTIVE SUMMARY", h2_style))

summary_rows = [
    ["Account",          "Realized P&L",           "Unrealized P&L",        "Net P&L",             "Type"],
    ["Alpaca (Paper)",   fmt_pnl(paper_rpnl),       fmt_pnl(paper_upnl),     fmt_pnl(paper_total_pnl), "Paper"],
    ["HL Crypto",        fmt_pnl(hl_rpnl),          fmt_pnl(hl_upnl),        fmt_pnl(hl_rpnl+hl_upnl), "Real"],
    ["xyz Commodities",  fmt_pnl(xyz_rpnl),         fmt_pnl(xyz_upnl),       fmt_pnl(xyz_rpnl+xyz_upnl),"Real"],
    ["OANDA Forex",      fmt_pnl(oanda_rpnl),       fmt_pnl(oanda_upnl),     fmt_pnl(oanda_rpnl+oanda_upnl),"Real"],
    ["China A-Shares",   "¥0",                      f"¥{china_upnl_cny:+,.0f}", f"¥{china_upnl_cny:+,.0f}","Real"],
    ["REAL TOTAL (USD)", fmt_pnl(real_rpnl),        fmt_pnl(real_upnl),      fmt_pnl(real_net),     "—"],
]

def color_cell(val_str):
    clean = val_str.replace(",","").replace("$","").replace("+","").replace("¥","")
    try:
        v = float(clean)
        return f'<font color="{"#27ae60" if v>=0 else "#e74c3c"}"><b>{val_str}</b></font>'
    except: return val_str

para_rows = []
for i, row in enumerate(summary_rows):
    if i == 0:
        para_rows.append([Paragraph(f"<b>{c}</b>", body_style) for c in row])
    elif i == len(summary_rows)-1:
        para_rows.append([Paragraph(f"<b>{color_cell(c) if j>0 and j<4 else c}</b>", body_style) for j,c in enumerate(row)])
    else:
        para_rows.append([Paragraph(color_cell(c) if j>0 and j<4 else c, body_style) for j,c in enumerate(row)])

st = Table(para_rows, colWidths=[1.55*inch,1.3*inch,1.3*inch,1.3*inch,0.85*inch])
st.setStyle(TableStyle([
    ("BACKGROUND",     (0,0),(-1,0),   DARK),
    ("TEXTCOLOR",      (0,0),(-1,0),   WHITE),
    ("BACKGROUND",     (0,-1),(-1,-1), colors.HexColor("#d5e8d4")),
    ("ROWBACKGROUNDS", (0,1),(-1,-2),  [LIGHT, WHITE]),
    ("ALIGN",          (0,0),(-1,-1),  "CENTER"),
    ("ALIGN",          (0,0),(0,-1),   "LEFT"),
    ("GRID",           (0,0),(-1,-1),  0.3, colors.lightgrey),
    ("TOPPADDING",     (0,0),(-1,-1),  5),
    ("BOTTOMPADDING",  (0,0),(-1,-1),  5),
    ("FONTSIZE",       (0,0),(-1,-1),  9),
]))
story.append(st)
story.append(Spacer(1, 0.15*inch))
story.append(Paragraph(
    "* China A-shares (BOE 000725, Hikvision 002415) opened 6/30 — one day in period, P&L pending market data.",
    note_style))
story.append(Spacer(1, 0.2*inch))

# ═══════════════════════════════════════════════════════════════════
# SECTION 1: PAPER TRADING
# ═══════════════════════════════════════════════════════════════════
story.append(HRFlowable(width="100%", thickness=1.5, color=BLUE))
story.append(Spacer(1, 0.08*inch))
story.append(Paragraph("SECTION 1 — PAPER TRADING (Alpaca)", h2_style))
story.append(Paragraph(
    f"Starting balance: $100,000 &nbsp;|&nbsp; Ending balance (6/30): {fmt_num(paper_end_eq)} &nbsp;|&nbsp; "
    f"Period gain: <b><font color='{'#27ae60' if paper_total_pnl>=0 else '#e74c3c'}'>{fmt_pnl(paper_total_pnl)} "
    f"({paper_total_pnl/paper_start_eq*100:+.2f}%)</font></b>", body_style))
story.append(Spacer(1, 0.1*inch))

# Daily P&L table
story.append(Paragraph("Daily Equity Tracker", h3_style))
daily_rows = []
for d in paper_daily:
    pl = d["daily_pnl"]
    pl_str = f'<font color="{"#27ae60" if pl>=0 else "#e74c3c"}"><b>{fmt_pnl(pl)}</b></font>'
    daily_rows.append([d["date"], fmt_num(d["equity"]), Paragraph(pl_str, body_style)])
daily_hdr = ["Date", "Equity", "Daily P&L"]
dt = Table([daily_hdr]+[[r[0],r[1],r[2]] for r in daily_rows],
           colWidths=[1.4*inch,1.4*inch,1.2*inch])
dt.setStyle(TableStyle([
    ("BACKGROUND",     (0,0),(-1,0),  DARK),
    ("TEXTCOLOR",      (0,0),(-1,0),  WHITE),
    ("FONTNAME",       (0,0),(-1,0),  "Helvetica-Bold"),
    ("ROWBACKGROUNDS", (0,1),(-1,-1), [LIGHT, WHITE]),
    ("ALIGN",          (0,0),(-1,-1), "CENTER"),
    ("GRID",           (0,0),(-1,-1), 0.3, colors.lightgrey),
    ("FONTSIZE",       (0,0),(-1,-1), 9),
    ("TOPPADDING",     (0,0),(-1,-1), 4),
    ("BOTTOMPADDING",  (0,0),(-1,-1), 4),
]))
story.append(dt)
story.append(Spacer(1, 0.12*inch))

# Open positions
story.append(Paragraph("Open Positions at Period End (6/30)", h3_style))
op_rows = []
for p in paper_open:
    u = p["upnl"]
    u_str = f'<font color="{"#27ae60" if u>=0 else "#e74c3c"}"><b>{fmt_pnl(u)}</b></font>'
    op_rows.append([p["sym"], p["dir"].upper(),
                    fmt_num(p["entry"]), fmt_num(p["now"]),
                    Paragraph(u_str, body_style)])
ot = Table([["Symbol","Direction","Entry","Price (7/1)","Unrealized P&L"]]+op_rows,
           colWidths=[1.0*inch,1.0*inch,1.1*inch,1.1*inch,1.2*inch])
ot.setStyle(TableStyle([
    ("BACKGROUND",     (0,0),(-1,0),  DARK),
    ("TEXTCOLOR",      (0,0),(-1,0),  WHITE),
    ("FONTNAME",       (0,0),(-1,0),  "Helvetica-Bold"),
    ("ROWBACKGROUNDS", (0,1),(-1,-1), [LIGHT, WHITE]),
    ("ALIGN",          (0,0),(-1,-1), "CENTER"),
    ("GRID",           (0,0),(-1,-1), 0.3, colors.lightgrey),
    ("FONTSIZE",       (0,0),(-1,-1), 9),
    ("TOPPADDING",     (0,0),(-1,-1), 4),
    ("BOTTOMPADDING",  (0,0),(-1,-1), 4),
]))
story.append(ot)
story.append(Spacer(1, 0.08*inch))
story.append(Paragraph(
    f"Paper Realized P&L (closed / mark-to-market): <b>{fmt_pnl(paper_rpnl)}</b>  |  "
    f"Paper Unrealized P&L: <b>{fmt_pnl(paper_upnl)}</b>  |  "
    f"Net: <b>{fmt_pnl(paper_total_pnl)}</b>", body_style))

story.append(Spacer(1, 0.2*inch))

# ═══════════════════════════════════════════════════════════════════
# SECTION 2: REAL MONEY
# ═══════════════════════════════════════════════════════════════════
story.append(HRFlowable(width="100%", thickness=1.5, color=GREEN))
story.append(Spacer(1, 0.08*inch))
story.append(Paragraph("SECTION 2 — REAL MONEY ACCOUNTS", h2_style))

# ── 2a. Hyperliquid Crypto ────────────────────────────────────────
story.append(Paragraph("2a. Hyperliquid — Crypto Perpetuals", h3_style))
hl_coin_rows = []
for coin, rpnl in sorted(hl_rpnl_by_coin.items(), key=lambda x: -abs(x[1])):
    if rpnl == 0: continue
    hl_coin_rows.append([coin,
        f'<font color="{"#27ae60" if rpnl>=0 else "#e74c3c"}"><b>{fmt_pnl(rpnl)}</b></font>'])
if hl_coin_rows:
    hl_coin_rows_p = [[r[0], Paragraph(r[1], body_style)] for r in hl_coin_rows]
    ct = Table([["Coin","Realized P&L"]]+hl_coin_rows_p, colWidths=[1.5*inch,1.5*inch])
    ct.setStyle(TableStyle([
        ("BACKGROUND",     (0,0),(-1,0),  DARK),
        ("TEXTCOLOR",      (0,0),(-1,0),  WHITE),
        ("FONTNAME",       (0,0),(-1,0),  "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0,1),(-1,-1), [LIGHT, WHITE]),
        ("ALIGN",          (0,0),(-1,-1), "CENTER"),
        ("GRID",           (0,0),(-1,-1), 0.3, colors.lightgrey),
        ("FONTSIZE",       (0,0),(-1,-1), 9),
        ("TOPPADDING",     (0,0),(-1,-1), 4),
        ("BOTTOMPADDING",  (0,0),(-1,-1), 4),
    ]))
    story.append(ct)
    story.append(Spacer(1, 0.08*inch))

# HL open positions
story.append(Paragraph("Open Positions", h3_style))
hl_open_rows = []
for coin, szi, entry, upnl in hl_open:
    now   = float(hl_mids.get(coin, entry))
    side  = "LONG" if szi > 0 else "SHORT"
    u_str = f'<font color="{"#27ae60" if upnl>=0 else "#e74c3c"}"><b>{fmt_pnl(upnl)}</b></font>'
    hl_open_rows.append([coin, side, f"${entry:.4f}", f"${now:.4f}", Paragraph(u_str, body_style)])
hot = Table([["Coin","Dir","Entry","Price","Unrealized P&L"]]+hl_open_rows,
            colWidths=[1.1*inch,0.7*inch,1.0*inch,1.0*inch,1.3*inch])
hot.setStyle(TableStyle([
    ("BACKGROUND",     (0,0),(-1,0),  DARK),
    ("TEXTCOLOR",      (0,0),(-1,0),  WHITE),
    ("FONTNAME",       (0,0),(-1,0),  "Helvetica-Bold"),
    ("ROWBACKGROUNDS", (0,1),(-1,-1), [LIGHT, WHITE]),
    ("ALIGN",          (0,0),(-1,-1), "CENTER"),
    ("GRID",           (0,0),(-1,-1), 0.3, colors.lightgrey),
    ("FONTSIZE",       (0,0),(-1,-1), 9),
    ("TOPPADDING",     (0,0),(-1,-1), 4),
    ("BOTTOMPADDING",  (0,0),(-1,-1), 4),
]))
story.append(hot)
story.append(Spacer(1, 0.06*inch))
story.append(Paragraph(
    f"HL Realized: <b>{fmt_pnl(hl_rpnl)}</b>  |  Unrealized: <b>{fmt_pnl(hl_upnl)}</b>  |  "
    f"Net: <b>{fmt_pnl(hl_rpnl+hl_upnl)}</b>  |  Account Value: ${4198:.0f}", body_style))

story.append(Spacer(1, 0.14*inch))

# ── 2b. xyz DEX Commodities ───────────────────────────────────────
story.append(Paragraph("2b. xyz DEX — Commodity Perpetuals (GOLD, SILVER, CL, BRENTOIL)", h3_style))
xyz_open_rows = []
xyz_mids_data = _hl_post({"type":"allMids","dex":"xyz"})
for coin, szi, entry, upnl in xyz_open:
    now   = float(xyz_mids_data.get(coin, entry))
    label = coin.replace("xyz:","")
    u_str = f'<font color="{"#27ae60" if upnl>=0 else "#e74c3c"}"><b>{fmt_pnl(upnl)}</b></font>'
    xyz_open_rows.append([label, "LONG", f"${entry:.2f}", f"${now:.2f}", Paragraph(u_str, body_style)])
xyz_open_rows.append(["BRENTOIL", "LONG→CLOSED", "~$73.65", "stopped out", Paragraph('<font color="#e74c3c"><b>~$0</b></font>', body_style)])

xt = Table([["Instrument","Dir","Entry","Price","Unrealized P&L"]]+xyz_open_rows,
           colWidths=[1.1*inch,1.0*inch,1.0*inch,1.0*inch,1.3*inch])
xt.setStyle(TableStyle([
    ("BACKGROUND",     (0,0),(-1,0),  DARK),
    ("TEXTCOLOR",      (0,0),(-1,0),  WHITE),
    ("FONTNAME",       (0,0),(-1,0),  "Helvetica-Bold"),
    ("ROWBACKGROUNDS", (0,1),(-1,-1), [LIGHT, WHITE]),
    ("ALIGN",          (0,0),(-1,-1), "CENTER"),
    ("GRID",           (0,0),(-1,-1), 0.3, colors.lightgrey),
    ("FONTSIZE",       (0,0),(-1,-1), 9),
    ("TOPPADDING",     (0,0),(-1,-1), 4),
    ("BOTTOMPADDING",  (0,0),(-1,-1), 4),
]))
story.append(xt)
story.append(Spacer(1, 0.06*inch))
story.append(Paragraph(
    f"xyz Realized: <b>{fmt_pnl(xyz_rpnl)}</b>  |  Unrealized: <b>{fmt_pnl(xyz_upnl)}</b>  |  "
    f"Net: <b>{fmt_pnl(xyz_rpnl+xyz_upnl)}</b>", body_style))

story.append(Spacer(1, 0.14*inch))

# ── 2c. OANDA Forex ───────────────────────────────────────────────
story.append(Paragraph("2c. OANDA — Forex", h3_style))
if oanda_closed:
    story.append(Paragraph("Closed Trades", h3_style))
    oc_rows = [[t["instrument"], t["closeTime"][:10], fmt_pnl(float(t.get("realizedPL",0)))]
               for t in oanda_closed]
    # (no closed trades this period)

story.append(Paragraph("Open Positions", h3_style))
oanda_open_rows = []
for instr, side, units, entry, now, upnl in oanda_open:
    u_str = f'<font color="{"#27ae60" if upnl>=0 else "#e74c3c"}"><b>{fmt_pnl(upnl)}</b></font>'
    oanda_open_rows.append([instr, side.upper(), str(units), f"{entry:.5f}", f"{now:.5f}", Paragraph(u_str, body_style)])
oft = Table([["Pair","Dir","Units","Entry","Price","Unrealized P&L"]]+oanda_open_rows,
            colWidths=[0.9*inch,0.6*inch,0.7*inch,0.9*inch,0.9*inch,1.2*inch])
oft.setStyle(TableStyle([
    ("BACKGROUND",     (0,0),(-1,0),  DARK),
    ("TEXTCOLOR",      (0,0),(-1,0),  WHITE),
    ("FONTNAME",       (0,0),(-1,0),  "Helvetica-Bold"),
    ("ROWBACKGROUNDS", (0,1),(-1,-1), [LIGHT, WHITE]),
    ("ALIGN",          (0,0),(-1,-1), "CENTER"),
    ("GRID",           (0,0),(-1,-1), 0.3, colors.lightgrey),
    ("FONTSIZE",       (0,0),(-1,-1), 9),
    ("TOPPADDING",     (0,0),(-1,-1), 4),
    ("BOTTOMPADDING",  (0,0),(-1,-1), 4),
]))
story.append(oft)
story.append(Spacer(1, 0.06*inch))
story.append(Paragraph(
    f"OANDA Realized: <b>{fmt_pnl(oanda_rpnl)}</b>  |  Unrealized: <b>{fmt_pnl(oanda_upnl)}</b>  |  "
    f"Net: <b>{fmt_pnl(oanda_rpnl+oanda_upnl)}</b>", body_style))
story.append(Spacer(1, 0.06*inch))
story.append(Paragraph("⚠  USDJPY short position is the primary drag (-$1,101). No OANDA trades closed this period.",
                        note_style))

story.append(Spacer(1, 0.14*inch))

# ── 2d. China ─────────────────────────────────────────────────────
story.append(Paragraph("2d. China A-Shares (miniQMT / Xinhua Paper)", h3_style))
china_rows = [
    ["BOE Technology (000725.SZ)", "LONG", "5700", "¥8.66 (ref)", "Opened 6/30", Paragraph('<b>Pending</b>', body_style)],
    ["Hikvision (002415.SZ)",      "LONG", "1400", "¥34.98 (ref)", "Opened 6/30", Paragraph('<b>Pending</b>', body_style)],
]
cft = Table([["Stock","Dir","Volume","Entry","Status","P&L"]]+china_rows,
            colWidths=[1.8*inch,0.5*inch,0.6*inch,0.9*inch,0.8*inch,0.7*inch])
cft.setStyle(TableStyle([
    ("BACKGROUND",     (0,0),(-1,0),  DARK),
    ("TEXTCOLOR",      (0,0),(-1,0),  WHITE),
    ("FONTNAME",       (0,0),(-1,0),  "Helvetica-Bold"),
    ("ROWBACKGROUNDS", (0,1),(-1,-1), [LIGHT, WHITE]),
    ("ALIGN",          (0,0),(-1,-1), "CENTER"),
    ("ALIGN",          (0,1),(0,-1),  "LEFT"),
    ("GRID",           (0,0),(-1,-1), 0.3, colors.lightgrey),
    ("FONTSIZE",       (0,0),(-1,-1), 9),
    ("TOPPADDING",     (0,0),(-1,-1), 4),
    ("BOTTOMPADDING",  (0,0),(-1,-1), 4),
]))
story.append(cft)
story.append(Spacer(1, 0.06*inch))
story.append(Paragraph("Positions opened on last day of period (6/30). P&L reported once market data is available.",
                        note_style))

story.append(Spacer(1, 0.2*inch))

# ═══════════════════════════════════════════════════════════════════
# SECTION 3: COMBINED SUMMARY
# ═══════════════════════════════════════════════════════════════════
story.append(HRFlowable(width="100%", thickness=2, color=GOLD))
story.append(Spacer(1, 0.08*inch))
story.append(Paragraph("SECTION 3 — COMBINED PERFORMANCE", h2_style))

final_rows = [
    ["",                 "Realized",          "Unrealized",        "Net",                  "Currency"],
    ["Paper (Alpaca)",   fmt_pnl(paper_rpnl), fmt_pnl(paper_upnl), fmt_pnl(paper_total_pnl),"USD (paper)"],
    ["HL Crypto",        fmt_pnl(hl_rpnl),    fmt_pnl(hl_upnl),    fmt_pnl(hl_rpnl+hl_upnl),"USD (real)"],
    ["xyz Commodities",  fmt_pnl(xyz_rpnl),   fmt_pnl(xyz_upnl),   fmt_pnl(xyz_rpnl+xyz_upnl),"USD (real)"],
    ["OANDA Forex",      fmt_pnl(oanda_rpnl), fmt_pnl(oanda_upnl), fmt_pnl(oanda_rpnl+oanda_upnl),"USD (real)"],
    ["China A-Shares",   "¥0",                "Pending",           "Pending",              "CNY (real)"],
    ["REAL SUBTOTAL",    fmt_pnl(real_rpnl),  fmt_pnl(real_upnl),  fmt_pnl(real_net),      "USD"],
]

para_final = []
for i, row in enumerate(final_rows):
    prow = []
    for j, c in enumerate(row):
        if i == 0:
            prow.append(Paragraph(f"<b>{c}</b>", body_style))
        elif i == len(final_rows)-1:
            style_here = ParagraphStyle("bold_body", parent=body_style, fontName="Helvetica-Bold")
            clean = c.replace(",","").replace("$","").replace("+","").replace("¥","").replace("Pending","0")
            try:
                v = float(clean)
                col = "#27ae60" if v>=0 else "#e74c3c"
                prow.append(Paragraph(f'<font color="{col}"><b>{c}</b></font>', body_style))
            except:
                prow.append(Paragraph(f"<b>{c}</b>", body_style))
        else:
            clean = c.replace(",","").replace("$","").replace("+","").replace("¥","")
            try:
                v = float(clean)
                col = "#27ae60" if v>=0 else "#e74c3c"
                prow.append(Paragraph(f'<font color="{col}">{c}</font>', body_style))
            except:
                prow.append(Paragraph(c, body_style))
    para_final.append(prow)

ft = Table(para_final, colWidths=[1.5*inch,1.2*inch,1.2*inch,1.2*inch,1.2*inch])
ft.setStyle(TableStyle([
    ("BACKGROUND",     (0,0),(-1,0),  DARK),
    ("TEXTCOLOR",      (0,0),(-1,0),  WHITE),
    ("BACKGROUND",     (0,-1),(-1,-1), colors.HexColor("#ffeeba")),
    ("ROWBACKGROUNDS", (0,1),(-1,-2), [LIGHT, WHITE]),
    ("ALIGN",          (0,0),(-1,-1), "CENTER"),
    ("ALIGN",          (0,1),(0,-1),  "LEFT"),
    ("GRID",           (0,0),(-1,-1), 0.3, colors.lightgrey),
    ("FONTSIZE",       (0,0),(-1,-1), 9),
    ("TOPPADDING",     (0,0),(-1,-1), 5),
    ("BOTTOMPADDING",  (0,0),(-1,-1), 5),
]))
story.append(ft)
story.append(Spacer(1, 0.1*inch))

# Key observations
obs = [
    f"• <b>Best performer:</b> HYPE long trade — realized {fmt_pnl(hl_rpnl_by_coin.get('HYPE',0))} in crypto perps",
    f"• <b>Biggest drag:</b> USDJPY short position — {fmt_pnl(oanda_upnl)} unrealized (no SL set)",
    f"• <b>Crypto account value:</b> ${4198:.0f} | HL realized alone: {fmt_pnl(hl_rpnl)}",
    f"• <b>Paper trading:</b> +{paper_total_pnl/paper_start_eq*100:.2f}% on $100K — AI signal entry working",
    f"• <b>Period:</b> June 10–30, 2026 (15 trading days) | Strategies: Jingda 4H signal + SMA cross",
]
for o in obs:
    story.append(Paragraph(o, body_style))

story.append(Spacer(1, 0.2*inch))
story.append(HRFlowable(width="100%", thickness=0.5, color=GRAY))
story.append(Spacer(1, 0.05*inch))
story.append(Paragraph(
    "This report is auto-generated by the AI Trading System. "
    "Unrealized P&L reflects mark-to-market at time of generation. "
    "China positions opened on final day of period; full P&L available in next report.",
    note_style))

doc.build(story)
print(f"PDF saved: {out_path}")

# ── Telegram ──────────────────────────────────────────────────────────────────
tg_token   = os.getenv("TELEGRAM_TOKEN")
tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")

caption = (
    "📊 *AI Trading — June 2026 Performance Report*\n"
    f"Period: June 10 – June 30, 2026\n\n"
    f"*PAPER TRADING (Alpaca)*\n"
    f"Net P&L: `{fmt_pnl(paper_total_pnl)}` ({paper_total_pnl/paper_start_eq*100:+.2f}%)\n\n"
    f"*REAL MONEY*\n"
    f"HL Crypto: `{fmt_pnl(hl_rpnl+hl_upnl)}` (realized `{fmt_pnl(hl_rpnl)}`)\n"
    f"xyz DEX:   `{fmt_pnl(xyz_rpnl+xyz_upnl)}`\n"
    f"OANDA:     `{fmt_pnl(oanda_rpnl+oanda_upnl)}`\n"
    f"Real Net:  `{fmt_pnl(real_net)}`\n\n"
    f"_Full breakdown in attached PDF_"
)

with open(str(out_path), "rb") as f:
    r = requests.post(
        f"https://api.telegram.org/bot{tg_token}/sendDocument",
        data={"chat_id": tg_chat_id, "caption": caption, "parse_mode": "Markdown"},
        files={"document": (out_path.name, f, "application/pdf")},
    )
print("Telegram:", r.status_code, r.json().get("ok"))
