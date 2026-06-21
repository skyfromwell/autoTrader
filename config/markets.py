"""
Market registry. Add new markets here — screener and watcher pick them up automatically.
Each entry needs: suffix, benchmark, timezone, open_time, close_time (HH:MM local), tv_prefix, category.
"""

MARKETS = {
    "lse": {
        "name":       "London Stock Exchange",
        "short":      "LSE",
        "suffix":     ".L",
        "benchmark":  "^FTSE",
        "timezone":   "Europe/London",
        "open_time":  "08:00",
        "close_time": "16:30",
        "tv_prefix":  "LSE",
        "category":   "non-us",
    },
    "asx": {
        "name":       "Australian Securities Exchange",
        "short":      "ASX",
        "suffix":     ".AX",
        "benchmark":  "^AXJO",
        "timezone":   "Australia/Sydney",
        "open_time":  "10:00",
        "close_time": "16:00",
        "tv_prefix":  "ASX",
        "category":   "non-us",
    },
    "tsx": {
        "name":       "Toronto Stock Exchange",
        "short":      "TSX",
        "suffix":     ".TO",
        "benchmark":  "^GSPTSE",
        "timezone":   "America/Toronto",
        "open_time":  "09:30",
        "close_time": "16:00",
        "tv_prefix":  "TSX",
        "category":   "non-us",
    },
    "hkex": {
        "name":       "Hong Kong Exchange",
        "short":      "HKEX",
        "suffix":     ".HK",
        "benchmark":  "^HSI",
        "timezone":   "Asia/Hong_Kong",
        "open_time":  "09:30",
        "close_time": "16:00",
        "tv_prefix":  "HKEX",
        "category":   "non-us",
    },
    "nse": {
        "name":       "NSE India",
        "short":      "NSE",
        "suffix":     ".NS",
        "benchmark":  "^NSEI",
        "timezone":   "Asia/Kolkata",
        "open_time":  "09:15",
        "close_time": "15:30",
        "tv_prefix":  "NSE",
        "category":   "non-us",
    },
    "euronext": {
        "name":       "Euronext Paris",
        "short":      "EURONEXT",
        "suffix":     ".PA",
        "benchmark":  "^FCHI",
        "timezone":   "Europe/Paris",
        "open_time":  "09:00",
        "close_time": "17:30",
        "tv_prefix":  "EURONEXT",
        "category":   "non-us",
    },
    "xetra": {
        "name":       "Deutsche Börse XETRA",
        "short":      "XETRA",
        "suffix":     ".DE",
        "benchmark":  "^GDAXI",
        "timezone":   "Europe/Berlin",
        "open_time":  "09:00",
        "close_time": "17:30",
        "tv_prefix":  "XETRA",
        "category":   "non-us",
    },
    "sse": {
        "name":       "Shanghai Stock Exchange",
        "short":      "SSE",
        "suffix":     ".SS",
        "benchmark":  "000001.SS",
        "timezone":   "Asia/Shanghai",
        "open_time":  "09:30",
        "close_time": "15:00",
        "tv_prefix":  "SSE",
        "category":   "non-us",
        "filter_overrides": {"ma_short": 10, "ma_long": 21},
    },
    "szse": {
        "name":       "Shenzhen Stock Exchange",
        "short":      "SZSE",
        "suffix":     ".SZ",
        "benchmark":  "399001.SZ",
        "timezone":   "Asia/Shanghai",
        "open_time":  "09:30",
        "close_time": "15:00",
        "tv_prefix":  "SZSE",
        "category":   "non-us",
        "filter_overrides": {"ma_short": 10, "ma_long": 21},
    },
    "nasdaq": {
        "name":       "NASDAQ",
        "short":      "NASDAQ",
        "suffix":     "",
        "benchmark":  "^IXIC",
        "timezone":   "America/New_York",
        "open_time":  "09:30",
        "close_time": "16:00",
        "tv_prefix":  "NASDAQ",
        "category":   "us",
        "filter_overrides": {"enable_rs": False, "max_pe_ratio": 50, "max_ps_ratio": 35},
    },
    "nyse": {
        "name":       "New York Stock Exchange",
        "short":      "NYSE",
        "suffix":     "",
        "benchmark":  "^NYA",
        "timezone":   "America/New_York",
        "open_time":  "09:30",
        "close_time": "16:00",
        "tv_prefix":  "NYSE",
        "category":   "us",
        "filter_overrides": {"enable_rs": False, "max_pe_ratio": 50, "max_ps_ratio": 35},
    },
}

# tv_prefix → market config (for watcher symbol routing)
PREFIX_MAP = {m["tv_prefix"]: m for m in MARKETS.values()}


def resolve_markets(market_arg: str) -> list[dict]:
    """Resolve --market flag to a list of market config dicts."""
    key = market_arg.lower()
    if key == "all":
        return list(MARKETS.values())
    if key == "non-us":
        return [m for m in MARKETS.values() if m["category"] == "non-us"]
    if key == "us":
        return [m for m in MARKETS.values() if m["category"] == "us"]
    if key in MARKETS:
        return [MARKETS[key]]
    valid = "all, non-us, us, " + ", ".join(MARKETS.keys())
    raise ValueError(f"Unknown market {market_arg!r}. Valid options: {valid}")
