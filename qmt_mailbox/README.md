# QMT mailbox — the active China A-share execution path

Replaces the miniQMT/xtquant path (archived in `../miniQMT/`, unsupported
by the broker since 2026-07-01). QMT itself has no bound external API the
way miniQMT did — the only way to place orders is a script running inside
QMT's own built-in Python console, which a human has to paste in and start
manually. That script can't make network calls (QMT's single shared
strategy thread must never block on I/O), so it can only read local files.

## Architecture

```
TradingView → mcp_processor.py / tv_alert_server.py / china_sma_report.py
              (MacBook / Mac Mini)
                    │
                    │  watcher/china_queue.py — HTTP POST /signal
                    ▼
mailbox_writer.py — FastAPI relay running ON the Windows QMT box
                    │  writes P:\qmt_signal_mailbox\inbox\{id}.json
                    ▼
qmt_mailbox_executor.py — pasted into QMT's built-in Python console,
                           polls the inbox on a timer, places the order,
                           writes status to outbox\{id}.json
```

`watcher/china_queue.py` polls `GET /signal/{id}/status` (or the caller
can) to see submitted/rejected/filled/stale once the QMT-side script has
processed it. `output/china_pending/` is now just local bookkeeping for
dedup — it is not read by anything on the Windows side anymore.

## Windows-side setup

1. Copy `mailbox_writer.py` to the Windows box, install `fastapi`,
   `uvicorn`, `python-dotenv`, `pydantic`, `requests`.
2. Set `QMT_MAILBOX_API_KEY` in that machine's `.env` (must match
   `QMT_MAILBOX_API_KEY` on the Mac/Mini side).
3. Run it persistently:
   `uvicorn mailbox_writer:app --host 0.0.0.0 --port 8800`
4. In QMT: open the built-in Python console, paste in the full contents
   of `qmt_mailbox_executor.py`, verify `account`/`accountType` at the top
   match the account QMT has this strategy bound to, and run it.
5. `P:\qmt_signal_mailbox\{inbox,outbox,processed,error}` are created
   automatically by both scripts on first run.

## Mac/Mini-side config (.env)

```
QMT_MAILBOX_URL=http://<windows-tailscale-ip>:8800
QMT_MAILBOX_API_KEY=<same key as the Windows box>
```
