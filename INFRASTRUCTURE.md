# autoTrader infrastructure — where things run

Last updated: 2026-07-21

## Machine roles

**Mini (`aimini`, Tailscale `100.64.0.5`, SSH alias `mini`) — the only live instance.**
Runs every process that touches TradingView alerts or broker orders:
- `com.autotrader.tradingview` — TradingView.app with remote debugging (chart data source for the pull-loop)
- `com.autotrader.tv-alert-server` — FastAPI webhook server, port 9999
- `com.autotrader.watcher` — pull-loop / margin reports / reconcile / evict_stale
- `com.autotrader.oanda-stream` — OANDA transaction-stream listener (all 4 accounts)
- `com.autotrader.hl-stream` — Hyperliquid userFills WebSocket listener
- `com.autotrader.forex-sl-tp-watcher` — software SL/TP watcher (FIFO-blocked pairs)
- `com.autotrader.cloudflared` — the ONLY registered connector for tunnel `09d89e12-aede-42d0-a55d-3367cbd0aff6` (`tv-alert.gmoainc.com` -> `localhost:9999`)

**MacBook (`Shas-MBP`) — dev machine, passive.**
None of the above services should be loaded here. It's used for writing code,
running one-off scripts (`show_positions.py`, `show_trade_history.py`), and
git/GitHub. If any `com.autotrader.*` service other than
`tailscale-headscale` ever shows up running here (`launchctl list | grep
autotrader`), that's a bug — a second live instance means duplicate
processing and state drift, which is exactly the failure mode this split
was built to eliminate. Unload it.

## Why this split exists

Before 2026-07-21, both machines ran the full stack simultaneously.
Cloudflare load-balances requests across every registered connector for a
tunnel, so TradingView alerts were being randomly split between the two
machines' independent `position_state.json` files — the root cause of
most of the "position silently disappeared / never got tracked" incidents
from earlier in July. Mini was chosen as the sole host because it's the
always-on machine; the MacBook sleeps/closes.

## What syncs where, and what doesn't

- **Syncthing** (folders `autotrader-output`, `-scripts`, `-trader`,
  `-watcher`, `china-pending`, `qmt-results`) keeps `output/`, `scripts/`,
  `trader/`, `watcher/` byte-identical between MacBook and Mini in near
  real time. Check `curl -s -H "X-API-Key: $KEY"
  http://127.0.0.1:8384/rest/db/completion?device=<mini-id>&folder=<id>`
  — `completion` should read 100 for all six.
- **`.env` does NOT sync** (correctly gitignored, and Syncthing isn't
  configured to touch it). Any credential or account-ID change made on
  one machine must be applied to the other by hand. This bit us once
  already: Mini was running on a since-regenerated OANDA API key and was
  missing `OANDA_ACCOUNT_ID_SHORT/MID/LONG` entirely until the 2026-07-21
  migration caught it.
- **GitHub** (`git@github.com:skyfromwell/autoTrader.git`) is the code
  history / backup, pushed from the MacBook. Mini is not a git repo — it
  gets code purely via Syncthing, so `git log` on Mini will always say
  "not a git repository." That's expected, not a bug.
- Syncthing leaves `.stfolder/` marker dirs and `*.sync-conflict-*` files
  when both sides edit the same file near-simultaneously (mostly
  `position_state.json` back when both machines were live). These are
  gitignored now; if you see fresh `sync-conflict` files reappear, it
  usually means two processes are writing the same state file again —
  check for a duplicate live instance first.

## If something looks wrong

1. `launchctl list | grep autotrader` on **both** machines. Mini should
   show all 6 services + tailscale-headscale; MacBook should show only
   tailscale-headscale.
2. `cloudflared tunnel info 09d89e12-aede-42d0-a55d-3367cbd0aff6` (run
   from the MacBook, which holds the origin cert) — should list exactly
   one connector, matching Mini's `cloudflared --version`.
3. Compare `output/position_state.json` on both machines
   (`ssh mini cat ~/autoTrader/output/position_state.json` vs local) — if
   they differ beyond a `last_pull` timestamp, Syncthing has stalled or
   something wrote to one side out of band.
4. Check `.env` keys match (`diff <(grep -o '^[A-Z_]*=' .env | sort)
   <(ssh mini grep -o '^[A-Z_]*=' ~/autoTrader/.env | sort)`) — this only
   confirms key names, not secret values match; re-check values by hand
   after any credential rotation.
