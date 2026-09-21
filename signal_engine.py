"""
Combines the indicators into one directional call (BUY / SELL / HOLD) with
a 0-100 confidence score and a plain-language breakdown of *why*.

Design intent: this is a confluence model, not a black box. Each indicator
casts a vote (bullish / bearish / neutral) with a fixed weight; the votes
are summed and normalized into a confidence score. You can see exactly
which factors fired by reading `reasons` on the returned Signal.

Honesty note (read this before trusting the number):
A confidence score here is NOT a probability of the trade being correct —
it's a measure of how many independent indicators currently agree with
each other. Indicators are correlated (several of them are just different
lenses on recent price momentum), so "5 indicators agree" does not mean
"90% chance of being right." Use backtester.py on your actual pair/
timeframe to see the real historical hit rate and expectancy of this
exact logic before you trust it with money — see README.md.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import pandas as pd

import config
from indicators import add_all_indicators


@dataclass
class Signal:
    timestamp: pd.Timestamp
    direction: str          # "BUY", "SELL", or "HOLD"
    confidence: float       # 0-100
    price: float
    reasons: list = field(default_factory=list)

    def __str__(self):
        lines = [
            f"[{self.timestamp}] {self.direction}  (confidence {self.confidence:.0f}/100)  price={self.price:.4f}"
        ]
        for r in self.reasons:
            lines.append(f"  - {r}")
        return "\n".join(lines)


# Each vote is (weight, bullish_condition_fn, bearish_condition_fn, label)
# Weights sum to 100 so confidence is naturally 0-100.
_WEIGHTS = {
    "trend_stack": 25,   # EMA fast/slow/trend ordering
    "macd": 20,          # MACD histogram sign + direction
    "rsi": 20,           # RSI level / reversal zone
    "bollinger": 15,     # price position vs bands
    "volume": 10,        # volume confirmation
    "support_resistance": 10,  # proximity to recent swing S/R
}
assert sum(_WEIGHTS.values()) == 100


def _score_trend_stack(row) -> tuple[float, str | None]:
    if row.ema_fast > row.ema_slow > row.ema_trend:
        return _WEIGHTS["trend_stack"], "EMA stack bullish (fast > slow > trend) — uptrend alignment"
    if row.ema_fast < row.ema_slow < row.ema_trend:
        return -_WEIGHTS["trend_stack"], "EMA stack bearish (fast < slow < trend) — downtrend alignment"
    return 0.0, None


def _score_macd(row, prev_row) -> tuple[float, str | None]:
    crossed_up = prev_row.macd_hist <= 0 and row.macd_hist > 0
    crossed_down = prev_row.macd_hist >= 0 and row.macd_hist < 0
    if crossed_up:
        return _WEIGHTS["macd"], "MACD histogram just crossed positive — bullish momentum shift"
    if crossed_down:
        return -_WEIGHTS["macd"], "MACD histogram just crossed negative — bearish momentum shift"
    if row.macd_hist > 0:
        return _WEIGHTS["macd"] * 0.5, "MACD histogram positive — momentum still bullish"
    if row.macd_hist < 0:
        return -_WEIGHTS["macd"] * 0.5, "MACD histogram negative — momentum still bearish"
    return 0.0, None


def _score_rsi(row) -> tuple[float, str | None]:
    if row.rsi < config.RSI_OVERSOLD:
        return _WEIGHTS["rsi"], f"RSI {row.rsi:.1f} in oversold zone — potential bounce"
    if row.rsi > config.RSI_OVERBOUGHT:
        return -_WEIGHTS["rsi"], f"RSI {row.rsi:.1f} in overbought zone — potential pullback"
    if row.rsi > 55:
        return _WEIGHTS["rsi"] * 0.3, f"RSI {row.rsi:.1f} above midline — mild bullish bias"
    if row.rsi < 45:
        return -_WEIGHTS["rsi"] * 0.3, f"RSI {row.rsi:.1f} below midline — mild bearish bias"
    return 0.0, None


def _score_bollinger(row) -> tuple[float, str | None]:
    if row.close <= row.bb_lower:
        return _WEIGHTS["bollinger"], "Price at/below lower Bollinger Band — statistically stretched to the downside"
    if row.close >= row.bb_upper:
        return -_WEIGHTS["bollinger"], "Price at/above upper Bollinger Band — statistically stretched to the upside"
    return 0.0, None


def _score_volume(row, direction_so_far: float) -> tuple[float, str | None]:
    if pd.isna(row.vol_sma) or row.vol_sma == 0:
        return 0.0, None
    if row.volume > 1.5 * row.vol_sma:
        if direction_so_far > 0:
            return _WEIGHTS["volume"], "Volume well above average, confirming the bullish move"
        if direction_so_far < 0:
            return -_WEIGHTS["volume"], "Volume well above average, confirming the bearish move"
    return 0.0, None


def _score_support_resistance(row, support: float, resistance: float) -> tuple[float, str | None]:
    span = resistance - support
    if span <= 0:
        return 0.0, None
    near_support = (row.close - support) / span < 0.05
    near_resistance = (resistance - row.close) / span < 0.05
    if near_support:
        return _WEIGHTS["support_resistance"], f"Price near recent swing support ({support:.4f})"
    if near_resistance:
        return -_WEIGHTS["support_resistance"], f"Price near recent swing resistance ({resistance:.4f})"
    return 0.0, None


def signal_at(enriched: pd.DataFrame, i: int) -> Signal:
    """
    Compute a Signal for row `i` of an already-indicator-enriched DataFrame,
    using only data at or before `i` (all indicators here — EMA/RSI/MACD/
    Bollinger/rolling volume/swing S&R — are causal, so this is safe to call
    inside a walk-forward backtest without leaking future information).
    """
    row = enriched.iloc[i]
    prev_row = enriched.iloc[i - 1]
    window = enriched.iloc[max(0, i - config.SR_LOOKBACK):i]
    if len(window) == 0:
        support, resistance = row.close, row.close
    else:
        support, resistance = window["low"].min(), window["high"].max()

    net_score = 0.0
    reasons: list[str] = []

    for score, reason in [
        _score_trend_stack(row),
        _score_macd(row, prev_row),
        _score_rsi(row),
    ]:
        net_score += score
        if reason:
            reasons.append(reason)

    score, reason = _score_bollinger(row)
    net_score += score
    if reason:
        reasons.append(reason)

    score, reason = _score_volume(row, net_score)
    net_score += score
    if reason:
        reasons.append(reason)

    score, reason = _score_support_resistance(row, support, resistance)
    net_score += score
    if reason:
        reasons.append(reason)

    confidence = min(abs(net_score), 100.0)

    if confidence >= config.MIN_CONFIDENCE and net_score > 0:
        direction = "BUY"
    elif confidence >= config.MIN_CONFIDENCE and net_score < 0:
        direction = "SELL"
    else:
        direction = "HOLD"
        if not reasons:
            reasons.append("No indicators showing a clear, aligned edge right now.")

    return Signal(
        timestamp=enriched.index[i],
        direction=direction,
        confidence=confidence,
        price=float(row.close),
        reasons=reasons,
    )


def generate_signal(df: pd.DataFrame) -> Signal:
    """
    df: raw OHLCV DataFrame (at least ~60 candles so indicators are warmed up).
    Returns a Signal for the LAST CLOSED candle in df.
    """
    enriched = add_all_indicators(df)
    if len(enriched) < max(config.EMA_TREND, config.SR_LOOKBACK) + 2:
        raise ValueError("Not enough candles to compute stable indicators (need ~60+).")
    return signal_at(enriched, len(enriched) - 1)
