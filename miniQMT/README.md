# miniQMT / xtquant path — archived 2026-07-21

This is the original China A-share execution path: a FastAPI wrapper
(`api.py` + `broker.py`) running on the Windows box, calling `xtquant`
directly to control a miniQMT terminal, plus a Syncthing-based file-watcher
variant (`china_executor.py` / `china_server_china_executor.py` +
`qmt_trade_wrapper.py`'s `TradeGuard`).

**miniQMT/xtquant external API access has not been supported by the broker
since 2026-07-01.** Nothing in this folder is currently runnable — it's kept
here (not deleted) only because that support status might change again in
the future, in which case this is the reference implementation to resume
from.

The active path going forward is QMT's built-in Python console — see
`qmt_mailbox/` at the repo root.
