#coding:gbk
"""
Mailbox-driven execution shell for QMT's built-in Python (内置Python /
模型交易). Paste this whole file into QMT's Python console and run it as a
live strategy bound to the trading account — QMT provides `account` and
`accountType` as strategy globals once bound that way (set explicitly below
as a fallback / for clarity; verify against the account shown in QMT's own
strategy-binding UI before going live).

Signal generation stays external and unchanged: the existing
TradingView -> mcp_processor.py / tv_alert_server.py / china_sma_report.py
pipeline keeps running on the Mac/Mini as usual, reaching this machine over
Tailscale via mailbox_writer.py's FastAPI relay (see qmt_mailbox/README.md).
That relay writes one JSON file per trade decision into the inbox folder
below; this strategy only ever reads local files here, on a timer — it
never touches the network itself, which is the safe pattern inside QMT's
single shared strategy thread (a network call here would block that thread).

------------------------------------------------------------------
Expected inbox file format (one file per signal), e.g.
  inbox\1784700000_a1b2c3d4.json
------------------------------------------------------------------
{
  "id": "1784700000_a1b2c3d4",  # unique id, becomes the order remark
  "stock": "600000.SH",
  "side": "buy",                 # "buy" | "sell" | "close"
  "volume": 100,                 # shares; omit/0 to let this script size it
  "max_position_volume": 500,    # optional per-signal cap, else use default
  "source": "sma_gold_cross"     # free text, for your own logging
}
------------------------------------------------------------------
"""
import os
import json
import time
import glob

INBOX_DIR     = r"P:\qmt_signal_mailbox\inbox"
PROCESSED_DIR = r"P:\qmt_signal_mailbox\processed"
ERROR_DIR     = r"P:\qmt_signal_mailbox\error"
OUTBOX_DIR    = r"P:\qmt_signal_mailbox\outbox"

# Fallback account binding — QMT normally injects `account`/`accountType`
# once this strategy is bound to an account via QMT's own UI. Set explicitly
# here too so the script fails loudly (NameError) rather than silently
# routing to the wrong account if that binding didn't happen.
account     = '66801935'
accountType = 'STOCK'


class _State:
    pass


S = _State()


def init(ContextInfo):
    for d in (INBOX_DIR, PROCESSED_DIR, ERROR_DIR, OUTBOX_DIR):
        os.makedirs(d, exist_ok=True)

    S.pending_orders = {}       # signal_id -> {..., 'confirmed': bool}
    S.default_max_position = 500
    S.min_cash_buffer = 0.0

    # poll the inbox every 3s; check outstanding order status every 5s
    ContextInfo.run_time("poll_inbox", "3nSecond", "2019-10-14 13:20:00")
    ContextInfo.run_time("check_pending_orders", "5nSecond", "2019-10-14 13:20:00")


def handlebar(ContextInfo):
    # not used for signal logic in this design -- everything is timer-driven
    pass


# ── STEP 0: pick up new signal files (non-blocking, short-lived) ──────────────

def poll_inbox(ContextInfo):
    files = sorted(glob.glob(os.path.join(INBOX_DIR, "*.json")))
    if not files:
        return

    for path in files:
        fname = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                signal = json.load(f)
        except Exception as e:
            print(f"[mailbox] failed to read {fname}: {e}")
            _move_file(path, ERROR_DIR)
            continue

        try:
            process_signal(ContextInfo, signal)
        except Exception as e:
            print(f"[mailbox] error processing {fname}: {e}")
            _move_file(path, ERROR_DIR)
            continue

        # move out of inbox regardless of accept/reject so it's never
        # reprocessed; the reason is logged via print()/outbox either way
        _move_file(path, PROCESSED_DIR)


def _move_file(path, dest_dir):
    try:
        os.replace(path, os.path.join(dest_dir, os.path.basename(path)))
    except Exception as e:
        print(f"[mailbox] could not move {path} -> {dest_dir}: {e}")


def _write_status(signal_id, status, detail=""):
    """Report back to mailbox_writer.py's outbox so the external side knows
    what happened, closing the loop without it needing to poll QMT."""
    try:
        out = {"id": signal_id, "status": status, "detail": detail, "ts": time.time()}
        path = os.path.join(OUTBOX_DIR, f"{signal_id}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[mailbox] could not write status for {signal_id}: {e}")


# ── STEPS 1-3: position check -> funds check -> place order ───────────────────

def process_signal(ContextInfo, signal):
    signal_id = signal.get("id")
    stock     = signal.get("stock")
    side      = signal.get("side", "").lower()

    if not signal_id or not stock or side not in ("buy", "sell", "close"):
        print(f"[mailbox] malformed signal, skipping: {signal}")
        if signal_id:
            _write_status(signal_id, "rejected", "malformed signal")
        return False

    if signal_id in S.pending_orders:
        print(f"[mailbox] duplicate signal id {signal_id}, skipping")
        return False

    # ---------- STEP 1: check positions ----------
    positions = get_trade_detail_data(account, accountType, 'position')
    held = {p.m_strInstrumentID + '.' + p.m_strExchangeID: p for p in positions}
    current_volume = held[stock].m_nVolume if stock in held else 0
    can_use_volume = held[stock].m_nCanUseVolume if stock in held else 0

    max_position     = signal.get("max_position_volume", S.default_max_position)
    requested_volume = int(signal.get("volume") or 0)

    if side == "buy":
        if current_volume >= max_position:
            msg = f"already at/over max position ({current_volume}/{max_position})"
            print(f"[mailbox] {signal_id} rejected: {msg}")
            _write_status(signal_id, "rejected", msg)
            return False
        volume = requested_volume or (max_position - current_volume)
        op_type = 23  # STOCK_BUY
    else:  # sell / close
        if can_use_volume <= 0:
            msg = "no available shares to sell"
            print(f"[mailbox] {signal_id} rejected: {msg}")
            _write_status(signal_id, "rejected", msg)
            return False
        volume = requested_volume or can_use_volume
        volume = min(volume, can_use_volume)
        op_type = 24  # STOCK_SELL

    # ---------- STEP 2: check funds (buys only) ----------
    if side == "buy":
        asset_list = get_trade_detail_data(account, accountType, 'account')
        if not asset_list:
            msg = "could not query account asset info"
            print(f"[mailbox] {signal_id} rejected: {msg}")
            _write_status(signal_id, "rejected", msg)
            return False
        available_cash = float(asset_list[0].m_dAvailable)

        full_tick = ContextInfo.get_full_tick([stock])
        last_price = full_tick.get(stock, {}).get('lastPrice', 0)
        if last_price <= 0:
            msg = "no valid last price"
            print(f"[mailbox] {signal_id} rejected: {msg}")
            _write_status(signal_id, "rejected", msg)
            return False

        estimated_cost = last_price * volume * 1.001
        if available_cash - S.min_cash_buffer < estimated_cost:
            msg = f"insufficient funds: available={available_cash}, need~={estimated_cost}"
            print(f"[mailbox] {signal_id} rejected: {msg}")
            _write_status(signal_id, "rejected", msg)
            return False

    if volume <= 0:
        msg = "resolved volume is zero, nothing to do"
        print(f"[mailbox] {signal_id} rejected: {msg}")
        _write_status(signal_id, "rejected", msg)
        return False

    # ---------- STEP 3: place the order ----------
    # prType 5 = latest price, quickTrade=2 = fire immediately
    passorder(
        op_type, 1101, account, stock,
        5, -1, volume,
        signal.get("source", "mailbox_signal"), 2, signal_id,
        ContextInfo
    )

    S.pending_orders[signal_id] = {
        'stock': stock, 'side': side, 'volume': volume,
        'submit_time': time.time(), 'confirmed': False,
    }
    print(f"[mailbox] submitted {signal_id}: {side} {volume} {stock}")
    _write_status(signal_id, "submitted", f"{side} {volume} {stock}")
    return True


# ── STEP 4: delayed confirmation ───────────────────────────────────────────────

def check_pending_orders(ContextInfo):
    if not S.pending_orders:
        return

    orders = get_trade_detail_data(account, accountType, 'order')
    trades = get_trade_detail_data(account, accountType, 'deal')

    order_by_remark = {o.m_strRemark: o for o in orders if getattr(o, 'm_strRemark', '')}
    trade_remarks    = {t.m_strRemark for t in trades if getattr(t, 'm_strRemark', '')}

    for sid, info in list(S.pending_orders.items()):
        if info['confirmed']:
            continue

        if sid in trade_remarks:
            print(f"[mailbox] CONFIRMED FILLED: {sid} ({info['stock']}, {info['volume']} shares)")
            info['confirmed'] = True
            _write_status(sid, "filled", f"{info['side']} {info['volume']} {info['stock']}")
            continue

        order = order_by_remark.get(sid)
        elapsed = time.time() - info['submit_time']
        if order is not None:
            print(f"[mailbox] {sid} order status = {order.m_nOrderStatus} (not yet filled, {elapsed:.0f}s)")
        else:
            print(f"[mailbox] {sid} not found in order cache yet (elapsed {elapsed:.0f}s)")

        if elapsed > 60:
            print(f"[mailbox] WARNING: {sid} still unconfirmed after 60s -- check manually")
            _write_status(sid, "stale", f"unconfirmed after {elapsed:.0f}s")
