"""
Pulls OHLCV (open/high/low/close/volume) candle data from a real exchange
via ccxt. Market data (candles) is public on Binance and most exchanges —
no API key is required for anything in this file.

NOTE: This talks to the live internet (api.binance.com by default), so it
must be run somewhere with normal outbound network access — your own
computer or a VPS. It will not run inside a network-sandboxed environment
that blocks exchange domains.
"""

from __future__ import annotations
import time
import pandas as pd
import ccxt

import config


def get_exchange(exchange_id: str = config.EXCHANGE_ID):
    """Instantiate a ccxt exchange object for public market-data calls."""
    klass = getattr(ccxt, exchange_id)
    return klass({
        "enableRateLimit": True,
        # Restrict to spot markets only — some exchanges (e.g. Binance)
        # otherwise load spot + margin + futures markets on init, which
        # hits extra endpoints (like dapi.binance.com) that may be
        # unreachable/geo-blocked even when spot isn't.
        "options": {"defaultType": "spot", "fetchMarkets": ["spot"]},
    })


def fetch_ohlcv(
    symbol: str = config.SYMBOL,
    timeframe: str = config.TIMEFRAME,
    limit: int = 500,
    exchange=None,
) -> pd.DataFrame:
    """
    Fetch up to `limit` most recent candles and return a DataFrame indexed
    by UTC timestamp with columns: open, high, low, close, volume.
    """
    ex = exchange or get_exchange()
    raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df


def fetch_ohlcv_history(
    symbol: str = config.SYMBOL,
    timeframe: str = config.TIMEFRAME,
    total_candles: int = config.BACKTEST_CANDLES,
    exchange=None,
) -> pd.DataFrame:
    """
    Page backwards through history to assemble more candles than a single
    API call returns (most exchanges cap a single fetch at ~1000 candles).
    """
    ex = exchange or get_exchange()
    all_rows = []
    since = None
    per_call = 1000
    fetched = 0

    # First, get the most recent candles to establish the end of the range.
    latest = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=per_call)
    if not latest:
        raise RuntimeError(f"No data returned for {symbol} {timeframe} on {ex.id}")
    all_rows.extend(latest)
    fetched += len(latest)
    earliest_ts = latest[0][0]

    tf_ms = ex.parse_timeframe(timeframe) * 1000

    while fetched < total_candles:
        since = earliest_ts - per_call * tf_ms
        chunk = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=per_call)
        if not chunk:
            break
        # Prepend, avoiding overlap with what we already have
        chunk = [c for c in chunk if c[0] < earliest_ts]
        if not chunk:
            break
        all_rows = chunk + all_rows
        earliest_ts = chunk[0][0]
        fetched += len(chunk)
        time.sleep(ex.rateLimit / 1000)

    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df.drop_duplicates(subset="timestamp", inplace=True)
    df.sort_values("timestamp", inplace=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    if len(df) > total_candles:
        df = df.iloc[-total_candles:]
    return df


if __name__ == "__main__":
    ex = get_exchange()
    df = fetch_ohlcv(limit=10)
    print(df)
