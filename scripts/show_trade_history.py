#!/usr/bin/env python3
"""Show closed trades from output/trade_history.jsonl (written by close_trade()).

Usage:
    python3 scripts/show_trade_history.py            # last 7 days
    python3 scripts/show_trade_history.py --days 30
    python3 scripts/show_trade_history.py --losses    # losses only
"""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

LEDGER_FILE = Path(__file__).parent.parent / "output" / "trade_history.jsonl"


def load_entries():
    if not LEDGER_FILE.exists():
        return []
    entries = []
    for line in LEDGER_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def fp(v, d=5):
    return "{:.{p}f}".format(v, p=d) if v is not None else "—"


def fpct(v):
    return "{:+.2f}%".format(v) if v is not None else "—"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    ap.add_argument("--losses", action="store_true", help="show losses only")
    ap.add_argument("--pair", type=str, default=None, help="filter by pair substring")
    args = ap.parse_args()

    entries = load_entries()
    cutoff = datetime.now() - timedelta(days=args.days)
    rows = []
    for e in entries:
        try:
            ct = datetime.fromisoformat(e["close_time"])
        except (KeyError, ValueError):
            continue
        if ct < cutoff:
            continue
        if args.losses and e.get("win_loss") != "loss":
            continue
        if args.pair and args.pair.upper() not in e.get("pair", "").upper():
            continue
        rows.append((ct, e))

    rows.sort(key=lambda r: r[0])

    if not rows:
        print(f"No closed trades in the last {args.days} day(s).")
        return

    W = 118
    print(f"\nClosed trades — last {args.days} day(s)" +
          (" (losses only)" if args.losses else ""))
    print("─" * W)
    hdr = "{:<16} {:<22} {:<6} {:<10} {:<10} {:<9} {:<6} {}".format(
        "Closed", "Pair", "Dir", "Entry", "Close", "P&L %", "W/L", "Reason")
    print(hdr)
    print("─" * W)

    wins = losses = 0
    for ct, e in rows:
        wl = e.get("win_loss") or "—"
        if wl == "win":
            wins += 1
        elif wl == "loss":
            losses += 1
        print("{:<16} {:<22} {:<6} {:<10} {:<10} {:<9} {:<6} {}".format(
            ct.strftime("%Y-%m-%d %H:%M"),
            e.get("pair", "—"),
            e.get("direction", "—"),
            fp(e.get("entry")),
            fp(e.get("close_price")),
            fpct(e.get("pnl_pct")),
            wl,
            e.get("close_reason", "—"),
        ))

    print("─" * W)
    total = wins + losses
    win_rate = f"{wins/total*100:.0f}%" if total else "—"
    print(f"{len(rows)} closes  |  {wins} win / {losses} loss  (win rate {win_rate})")
    print()


if __name__ == "__main__":
    main()
