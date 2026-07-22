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

TP/SL: QMT has no broker-side conditional-order support for A-shares, so
this script IS the TP/SL enforcement — it stores tp/sl per stock (from the
originating buy signal) in POSITIONS_FILE (survives a strategy restart,
unlike in-memory state) and polls live price against it every
CHECK_TPSL_INTERVAL seconds, firing its own sell when crossed. A-share T+1
settlement means a same-day sell can never actually fill regardless; the
check naturally waits for can_use_volume > 0 rather than needing any
special-cased T+1 logic of its own.

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
  "source": "sma_gold_cross",    # free text, for your own logging
  "tp": 39.49,                   # optional — take-profit price (buy only)
  "sl": 32.87                    # optional — stop-loss price (buy only)
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
POSITIONS_FILE = r"P:\qmt_signal_mailbox\positions.json"

# Fallback account binding — QMT normally injects `account`/`accountType`
# once this strategy is bound to an account via QMT's own UI. Set explicitly
# here too so the script fails loudly (NameError) rather than silently
# routing to the wrong account if that binding didn't happen.
account     = '66801935'
accountType = 'STOCK'


class _State:
    pass


S = _State()


def _load_positions():
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_positions():
    try:
        tmp = POSITIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(S.positions, f)
        os.replace(tmp, POSITIONS_FILE)
    except Exception as e:
        print(f"[mailbox] could not save positions file: {e}")


def init(ContextInfo):
    for d in (INBOX_DIR, PROCESSED_DIR, ERROR_DIR, OUTBOX_DIR):
        os.makedirs(d, exist_ok=True)

    S.pending_orders = {}       # signal_id -> {..., 'confirmed': bool}
    S.positions = _load_positions()   # stock -> {tp, sl, source, signal_id}
    S.default_max_position = 500
    S.min_cash_buffer = 0.0
    print(f"[mailbox] loaded {len(S.positions)} tracked TP/SL position(s) from disk")

    # poll the inbox every 3s; check outstanding order status every 5s;
    # check tp/sl on tracked positions every 30s (no need to be as tight —
    # T+1 means nothing can fill same-day for a fresh buy regardless)
    ContextInfo.run_time("poll_inbox", "3nSecond", "2019-10-14 13:20:00")
    ContextInfo.run_time("check_pending_orders", "5nSecond", "2019-10-14 13:20:00")
    ContextInfo.run_time("check_tp_sl", "30nSecond", "2019-10-14 13:20:00")


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
        'tp': signal.get('tp'), 'sl': signal.get('sl'),
        'source': signal.get('source', 'mailbox_signal'),
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

            # A filled buy with tp/sl starts TP/SL tracking for this stock —
            # this is the only point a fill is confirmed, so it's the right
            # moment to start watching it (a rejected/unfilled buy never
            # reaches here at all).
            if info['side'] == 'buy' and (info.get('tp') is not None or info.get('sl') is not None):
                S.positions[info['stock']] = {
                    'tp': info.get('tp'), 'sl': info.get('sl'),
                    'source': info.get('source'), 'signal_id': sid,
                    'tracked_since': time.time(),
                }
                _save_positions()
                print(f"[mailbox] TP/SL tracking started for {info['stock']}: "
                      f"tp={info.get('tp')} sl={info.get('sl')}")
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


# ── STEP 5: TP/SL enforcement on tracked positions ─────────────────────────────
# QMT has no broker-side conditional order for A-shares, so this poll IS the
# stop-loss/take-profit. can_use_volume naturally handles T+1 -- a position
# bought today simply isn't sellable yet, so the check harmlessly no-ops
# until the shares become available, no special-casing needed.

def check_tp_sl(ContextInfo):
    if not S.positions:
        return

    positions = get_trade_detail_data(account, accountType, 'position')
    held = {p.m_strInstrumentID + '.' + p.m_strExchangeID: p for p in positions}

    for stock, info in list(S.positions.items()):
        pos = held.get(stock)
        if pos is None or pos.m_nVolume <= 0:
            # no longer held at all (sold some other way) -- stop tracking
            print(f"[mailbox] {stock} no longer held, dropping TP/SL tracking")
            del S.positions[stock]
            _save_positions()
            continue

        can_use_volume = pos.m_nCanUseVolume
        if can_use_volume <= 0:
            # bought today (T+1) or already fully pending elsewhere -- wait
            continue

        full_tick = ContextInfo.get_full_tick([stock])
        last_price = full_tick.get(stock, {}).get('lastPrice', 0)
        if last_price <= 0:
            continue

        tp, sl = info.get('tp'), info.get('sl')
        hit = None
        if tp is not None and last_price >= tp:
            hit = 'tp_hit'
        elif sl is not None and last_price <= sl:
            hit = 'sl_hit'
        if hit is None:
            continue

        sell_id = f"{stock}_{hit}_{int(time.time())}"
        print(f"[mailbox] {hit.upper()} on {stock}: price={last_price} "
              f"tp={tp} sl={sl} -- selling {can_use_volume} shares")
        passorder(
            24, 1101, account, stock,   # 24 = STOCK_SELL
            5, -1, can_use_volume,
            hit, 2, sell_id,
            ContextInfo
        )
        S.pending_orders[sell_id] = {
            'stock': stock, 'side': 'sell', 'volume': can_use_volume,
            'submit_time': time.time(), 'confirmed': False,
            'tp': None, 'sl': None, 'source': hit,
        }
        _write_status(sell_id, "submitted", f"{hit} sell {can_use_volume} {stock}")
        del S.positions[stock]
        _save_positions()
