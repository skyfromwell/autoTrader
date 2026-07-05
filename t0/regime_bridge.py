"""
regime_bridge.py — Shared JSON file bridge between TV/MCP (low-frequency) and
local T+0 executor (high-frequency).

TV side (via mcp_processor or tv_alert_server webhook):
    from t0.regime_bridge import write_regime
    write_regime("SSE:600036", t_mode="aggressive")

Local T+0 side (polls on each tick):
    from t0.regime_bridge import read_regime
    cfg = read_regime("SSE:600036")
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_FILE = Path(os.getenv("T0_REGIME_FILE", "output/t0_regime.json"))


def write_regime(symbol: str, t_mode: str,
                 file: Path = _DEFAULT_FILE,
                 extra: dict | None = None) -> None:
    """
    Write regime parameters for a symbol.
    Called by TV webhook / mcp_processor when a regime change signal arrives.

    t_mode: "aggressive" | "normal" | "conservative"
    extra:  optional override fields (e.g. sell_fraction, no_new_sell_after)
    """
    data = _load(file)
    data[symbol] = {
        "t_mode":     t_mode,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        **(extra or {}),
    }
    _save(file, data)
    log.info(f"[regime_bridge] write  {symbol}  t_mode={t_mode}")


def read_regime(symbol: str, file: Path = _DEFAULT_FILE) -> dict:
    """
    Read the latest regime for a symbol.
    Returns dict with at least {'t_mode': str}. Falls back to 'normal' if not set.
    """
    data = _load(file)
    entry = data.get(symbol, {})
    if not entry:
        log.debug(f"[regime_bridge] no regime for {symbol} — defaulting to normal")
        return {"t_mode": "normal"}
    return entry


def clear_regime(symbol: str, file: Path = _DEFAULT_FILE) -> None:
    data = _load(file)
    if symbol in data:
        del data[symbol]
        _save(file, data)
        log.info(f"[regime_bridge] cleared {symbol}")


def list_regimes(file: Path = _DEFAULT_FILE) -> dict:
    return _load(file)


def _load(file: Path) -> dict:
    if file.exists():
        try:
            return json.loads(file.read_text())
        except Exception:
            return {}
    return {}


def _save(file: Path, data: dict) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
