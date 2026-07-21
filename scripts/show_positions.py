#!/usr/bin/env python3
"""Show all positions: symbol, entry, current, size, TP, SL, PnL, progress."""
import urllib.request, json
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent.parent / '.env')

ALP_KEY    = os.environ['ALPACA_API_KEY']
ALP_SEC    = os.environ['ALPACA_SECRET_KEY']
HL_WALLET  = os.environ['HL_WALLET_ADDRESS']
STATE_FILE = Path(__file__).parent.parent / 'output' / 'position_state.json'

# Four OANDA accounts, split by signal timeframe (see trader/oanda_trader.py):
# one shared API key, "OANDA" = original/mix account, others = 1h/4h/1D splits.
_OANDA_KEY = os.environ['OANDA_API_KEY']
OANDA_ACCOUNTS = {
    'OANDA':       (_OANDA_KEY, os.environ['OANDA_ACCOUNT_ID']),
    'OANDA_SHORT': (_OANDA_KEY, os.environ.get('OANDA_ACCOUNT_ID_SHORT', '')),
    'OANDA_MID':   (_OANDA_KEY, os.environ.get('OANDA_ACCOUNT_ID_MID', '')),
    'OANDA_LONG':  (_OANDA_KEY, os.environ.get('OANDA_ACCOUNT_ID_LONG', '')),
}

def get(url, headers={}):
    req = urllib.request.Request(url, headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def post_hl(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request('https://api.hyperliquid.xyz/info', data=data,
                                 headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def fetch_broker_orders():
    """Returns {pair: {'tp': price_or_None, 'sl': price_or_None}} for broker-confirmed orders."""
    confirmed = {}

    # Oanda pending TP/SL orders — both accounts
    for prefix, (key, acct) in OANDA_ACCOUNTS.items():
        if not (key and acct):
            continue
        try:
            resp = get(f'https://api-fxtrade.oanda.com/v3/accounts/{acct}/orders?state=PENDING',
                       {'Authorization': f'Bearer {key}'})
            # Build tradeID → instrument mapping from open trades
            trades_resp = get(f'https://api-fxtrade.oanda.com/v3/accounts/{acct}/openTrades',
                              {'Authorization': f'Bearer {key}'})
            tid_to_inst = {t['id']: t['instrument'] for t in trades_resp.get('trades', [])}

            for o in resp.get('orders', []):
                tid = o.get('tradeID')
                inst = tid_to_inst.get(tid)
                if not inst:
                    continue
                pair = prefix + ':' + inst.replace('_', '')
                confirmed.setdefault(pair, {'tp': None, 'sl': None})
                if o['type'] == 'TAKE_PROFIT':
                    confirmed[pair]['tp'] = float(o['price'])
                elif o['type'] == 'STOP_LOSS':
                    confirmed[pair]['sl'] = float(o['price'])
        except Exception:
            pass

    # Hyperliquid reduce-only trigger orders (main dex + xyz dex).
    # frontendOpenOrders exposes triggerPx/orderType directly, so TP vs SL is
    # read off the order itself rather than inferred from limitPx (which is
    # just a ±5% execution buffer around the trigger, not the trigger price).
    for dex_kwargs, tv_prefix in (({}, 'HYPERLIQUID'), ({'dex': 'xyz'}, 'HIP3XYZ')):
        try:
            orders = post_hl({'type': 'frontendOpenOrders', 'user': HL_WALLET, **dex_kwargs})
            for o in orders:
                if not o.get('reduceOnly') or not o.get('isTrigger'):
                    continue
                raw_coin = o.get('coin', '')
                # HL XYZ returns coin as "xyz:BRENTOIL" — strip the prefix
                coin = raw_coin.split(':', 1)[-1]
                pair = tv_prefix + ':' + coin + 'USDC.P'
                confirmed.setdefault(pair, {'tp': None, 'sl': None})
                trigger_px = float(o.get('triggerPx', 0))
                order_type = o.get('orderType', '')
                if 'Take Profit' in order_type:
                    confirmed[pair]['tp'] = trigger_px
                elif 'Stop' in order_type:
                    confirmed[pair]['sl'] = trigger_px
        except Exception:
            pass

    return confirmed


def fetch_all():
    # Oanda — positions + mid prices, both accounts
    oanda = {}
    for prefix, (key, acct) in OANDA_ACCOUNTS.items():
        if not (key and acct):
            continue
        oanda_pos = get(f'https://api-fxtrade.oanda.com/v3/accounts/{acct}/openPositions',
                        {'Authorization': f'Bearer {key}'})
        instruments = [p['instrument'] for p in oanda_pos['positions']]
        px_map = {}
        if instruments:
            price_str = '%2C'.join(instruments)
            oanda_prices = get(
                f'https://api-fxtrade.oanda.com/v3/accounts/{acct}/pricing?instruments={price_str}',
                {'Authorization': f'Bearer {key}'})
            px_map = {p['instrument']: (float(p['bids'][0]['price']) + float(p['asks'][0]['price'])) / 2
                      for p in oanda_prices['prices']}
        for p in oanda_pos['positions']:
            inst = p['instrument']
            pair = prefix + ':' + inst.replace('_', '')
            lu, su = int(p['long']['units']), int(p['short']['units'])
            cur = px_map.get(inst, 0)
            if lu > 0:
                oanda[pair] = ('long',  float(p['long']['averagePrice']),  cur,
                               float(p['long']['unrealizedPL']),  lu)
            if su < 0:
                oanda[pair] = ('short', float(p['short']['averagePrice']), cur,
                               float(p['short']['unrealizedPL']), abs(su))

    # Alpaca paper
    alpaca = {}
    for p in get('https://paper-api.alpaca.markets/v2/positions',
                 {'APCA-API-KEY-ID': ALP_KEY, 'APCA-API-SECRET-KEY': ALP_SEC}):
        alpaca['BATS:' + p['symbol']] = (
            p['side'], float(p['avg_entry_price']), float(p['current_price']),
            float(p['unrealized_pl']), float(p['qty']))

    # Hyperliquid + XYZ
    hl = {}
    def parse_hl(state, prefix='HYPERLIQUID'):
        for p in state.get('assetPositions', []):
            pos = p['position']
            raw_coin = pos['coin']
            # Strip dex prefix e.g. "xyz:BRENTOIL" → "BRENTOIL"
            coin = raw_coin.split(':', 1)[-1]
            szi = float(pos['szi'])
            if szi == 0: continue
            entry = float(pos['entryPx'])
            upnl  = float(pos.get('unrealizedPnl', 0))
            val   = float(pos.get('positionValue', 0))
            mark  = val / abs(szi) if abs(szi) else 0
            tv = prefix + ':' + coin + 'USDC.P'
            hl[tv] = ('long' if szi > 0 else 'short', entry, mark, upnl, abs(szi))
    parse_hl(post_hl({'type': 'clearinghouseState', 'user': HL_WALLET}))
    # xyz-DEX assets (GOLD/SILVER/CL/BRENTOIL) are tracked in position_state.json
    # under jingda's HIP3XYZ: prefix, not HYPERLIQUID: — must match or these
    # positions silently vanish from the display (they're real broker positions,
    # just never joined against local tp/sl/tracking).
    parse_hl(post_hl({'type': 'clearinghouseState', 'user': HL_WALLET, 'dex': 'xyz'}), prefix='HIP3XYZ')

    return {**oanda, **alpaca, **hl}

# ── Formatting ────────────────────────────────────────────────────────────────

def fp(v, d=5):
    return '{:.{p}f}'.format(v, p=d) if v else '—'

def fpct(entry, cur, direction):
    if not entry or not cur: return '—'
    v = ((cur - entry) / entry * 100) if direction == 'long' else ((entry - cur) / entry * 100)
    return '{:+.2f}%'.format(v)

def fusd(v):
    return '${:+,.2f}'.format(v)

def fsize(v):
    if v is None: return '—'
    return '{:,.2f}'.format(v) if v >= 100 else '{:.4f}'.format(v)

W   = 128
SEP = '─' * W
HDR = '{:<22} {:<6} {:<12} {:<12} {:<14} {:<14} {:<14} {:<12} {}'.format(
      'Symbol', 'Dir', 'Entry', 'Current', 'Size', 'TP', 'SL', 'PnL $', 'Progress')

def _close_enough(a, b):
    """True if a and b match within HL's ~5-significant-figure trigger rounding."""
    return abs(a - b) <= max(0.0001, abs(b) * 0.001)

def tp_marker(tp_val, confirmed):
    """Return '✓' if broker confirmed this TP price, else ''."""
    if confirmed is None or tp_val is None:
        return ''
    btp = confirmed.get('tp')
    if btp is not None and _close_enough(btp, tp_val):
        return ' ✓'
    return ''

def sl_marker(sl_val, confirmed):
    """Return '✓' if broker confirmed this SL price, else ''."""
    if confirmed is None or sl_val is None:
        return ''
    bsl = confirmed.get('sl')
    if bsl is not None and _close_enough(bsl, sl_val):
        return ' ✓'
    return ''

def print_subsection(subtitle, pairs, broker, broker_orders, dec=5):
    """Print a subtitle row + rows for matching pairs; return subtotal PnL."""
    print('  -- {} --'.format(subtitle))
    sub = 0.0
    for pair, t in sorted(pairs):
        bd = broker.get(pair)
        if bd is None:
            continue
        d      = t['direction']
        tp     = t.get('tp') or t.get('manual_tp') or t.get('watcher_tp')
        sl     = t.get('sl') or t.get('manual_sl') or t.get('watcher_sl')
        conf   = broker_orders.get(pair)
        _, avg, cur, upnl, bsize = bd
        # Use broker's average entry price — correct for multi-entry positions
        entry  = avg
        sub += upnl
        sym = pair.split(':', 1)[1] if ':' in pair else pair
        tp_str = (fp(tp, dec) + tp_marker(tp, conf)) if tp else '—'
        sl_str = (fp(sl, dec) + sl_marker(sl, conf)) if sl else '—'
        print('  {:<22} {:<6} {:<12} {:<12} {:<14} {:<14} {:<14} {:<12} {}'.format(
            sym, d,
            fp(entry, dec), fp(cur, dec),
            fsize(bsize),
            tp_str, sl_str,
            fusd(upnl),
            fpct(entry, cur, d)))
    return sub


def main():
    broker = fetch_all()
    broker_orders = fetch_broker_orders()

    with open(STATE_FILE) as f:
        state = json.load(f)
    trades = {k: v for k, v in state['open_trades'].items() if not v.get('closed')}

    # Forex sub-accounts: mix (original/legacy) + short(1h)/mid(4h)/long(1D) splits.
    _FOREX_ACCOUNTS = [
        ('OANDA:',       'Forex (mix account)'),
        ('OANDA_SHORT:', 'Forex (short/1h account)'),
        ('OANDA_MID:',   'Forex (mid/4h account)'),
        ('OANDA_LONG:',  'Forex (long/1D account)'),
    ]
    crypto = [(p, t) for p, t in trades.items()
              if p.startswith(('HYPERLIQUID:', 'BYBIT:', 'COINBASE:', 'XYZ:', 'HIP3XYZ:'))]
    paper  = [(p, t) for p, t in trades.items() if p.startswith('BATS:')]

    print('\n' + '╔' + '═' * (W - 2) + '╗')
    print('║' + 'REAL MONEY'.center(W - 2) + '║')
    print('╚' + '═' * (W - 2) + '╝')
    print('  ' + HDR)
    print('  ' + SEP)

    fx_totals = {}
    first = True
    for prefix, label in _FOREX_ACCOUNTS:
        rows = [(p, t) for p, t in trades.items() if p.startswith(prefix)]
        if not rows and prefix != 'OANDA:':
            continue  # skip empty split accounts, always show mix even if empty
        if not first:
            print('  ' + '·' * W)
        first = False
        fx_totals[label] = print_subsection(label, rows, broker, broker_orders, dec=5)
    print('  ' + '·' * W)
    crypto_total = print_subsection('Crypto', crypto, broker, broker_orders, dec=5)
    print('  ' + SEP)
    fx_total   = sum(fx_totals.values())
    real_total = fx_total + crypto_total
    fx_summary = '  '.join(f'{lbl.replace("Forex ", "")}: {fusd(v):<14}' for lbl, v in fx_totals.items())
    print(f'  {fx_summary}  Crypto: {fusd(crypto_total):<14}  Real Money Total: {fusd(real_total)}')

    print('\n' + '╔' + '═' * (W - 2) + '╗')
    print('║' + 'PAPER MONEY — Alpaca US Stocks'.center(W - 2) + '║')
    print('╚' + '═' * (W - 2) + '╝')
    print('  ' + HDR)
    print('  ' + SEP)
    paper_total = print_subsection('US Stocks', paper, broker, broker_orders, dec=2)
    print('  ' + SEP)
    print('  Paper Money Total: {}'.format(fusd(paper_total)))

    print('\n  ✓ = TP/SL confirmed on broker platform')
    print()

if __name__ == '__main__':
    main()
