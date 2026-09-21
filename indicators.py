"""
Technical indicators computed with plain pandas/numpy (no black-box
libraries) so every number here is easy to audit.

All functions take a DataFrame with columns open/high/low/close/volume and
return the DataFrame with new columns added, or a Series for single values.
"""

from __future__ import annotations
import numpy as np
import pandas as pd

import config


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = config.RSI_PERIOD) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)  # neutral when undefined (e.g. no losses yet)


def macd(
    series: pd.Series,
    fast: int = config.MACD_FAST,
    slow: int = config.MACD_SLOW,
    signal: int = config.MACD_SIGNAL,
):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger_bands(series: pd.Series, period: int = config.BB_PERIOD, num_std: float = config.BB_STD):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def atr(df: pd.DataFrame, period: int = config.ATR_PERIOD) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def swing_support_resistance(df: pd.DataFrame, lookback: int = config.SR_LOOKBACK):
    """
    Simple, transparent swing high/low support & resistance: the highest
    high and lowest low over the trailing `lookback` bars (excluding the
    current, still-forming bar).
    """
    window = df.iloc[-(lookback + 1):-1]
    resistance = window["high"].max()
    support = window["low"].min()
    return support, resistance


def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with every indicator column attached."""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], config.EMA_FAST)
    out["ema_slow"] = ema(out["close"], config.EMA_SLOW)
    out["ema_trend"] = ema(out["close"], config.EMA_TREND)
    out["rsi"] = rsi(out["close"])
    macd_line, signal_line, hist = macd(out["close"])
    out["macd"] = macd_line
    out["macd_signal"] = signal_line
    out["macd_hist"] = hist
    upper, mid, lower = bollinger_bands(out["close"])
    out["bb_upper"] = upper
    out["bb_mid"] = mid
    out["bb_lower"] = lower
    out["atr"] = atr(out)
    out["vol_sma"] = out["volume"].rolling(20).mean()
    return out
