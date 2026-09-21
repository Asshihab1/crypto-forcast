"""
Honest walk-forward backtest of the signal engine.

Design goals, stated up front:
  - No lookahead bias: every signal at bar i is computed only from indicator
    values that are causal up to and including bar i (see signal_engine.signal_at).
  - Realistic costs: every entry and exit pays config.TAKER_FEE (fee) and
    config.SLIPPAGE (assumed slippage), both ways, like a real trade would.
  - Reports what actually matters for profitability — win rate AND average
    win/loss size AND expectancy AND max drawdown — not just "accuracy",
    because a high win rate with small wins and rare huge losses can still
    lose money overall, and vice versa.

This is still not a guarantee of future performance. Markets change regime;
a strategy that backtests well on the last N candles can still fail going
forward. Treat this as a sanity check that the logic isn't obviously broken
or curve-fit, not as proof of a durable edge.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import pandas as pd
import numpy as np

import config
from indicators import add_all_indicators
from signal_engine import signal_at


@dataclass
class Trade:
    direction: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    bars_held: int
    pnl_pct: float  # net of fees & slippage


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)  # cumulative return multiplier per bar
    directional_hit_rate: float = 0.0   # of bars where a BUY/SELL fired, % where NEXT bar closed in that direction
    directional_signals: int = 0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl_pct > 0)
        return 100.0 * wins / len(self.trades)

    @property
    def avg_win_pct(self) -> float:
        wins = [t.pnl_pct for t in self.trades if t.pnl_pct > 0]
        return float(np.mean(wins)) * 100 if wins else 0.0

    @property
    def avg_loss_pct(self) -> float:
        losses = [t.pnl_pct for t in self.trades if t.pnl_pct <= 0]
        return float(np.mean(losses)) * 100 if losses else 0.0

    @property
    def expectancy_pct(self) -> float:
        """Average PnL% per trade, net of costs — the number that actually matters."""
        if not self.trades:
            return 0.0
        return float(np.mean([t.pnl_pct for t in self.trades])) * 100

    @property
    def total_return_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        return (self.equity_curve[-1] - 1.0) * 100

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        curve = np.array(self.equity_curve)
        running_max = np.maximum.accumulate(curve)
        drawdown = (curve - running_max) / running_max
        return float(drawdown.min()) * 100

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl_pct for t in self.trades if t.pnl_pct > 0)
        losses = -sum(t.pnl_pct for t in self.trades if t.pnl_pct <= 0)
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    def summary(self) -> str:
        lines = [
            "=" * 60,
            "BACKTEST RESULTS (net of fees + assumed slippage)",
            "=" * 60,
            f"Trades taken:                {self.n_trades}",
            f"Win rate:                    {self.win_rate:.1f}%",
            f"Avg win:                     {self.avg_win_pct:+.3f}%",
            f"Avg loss:                    {self.avg_loss_pct:+.3f}%",
            f"Expectancy per trade:        {self.expectancy_pct:+.3f}%",
            f"Profit factor:                {self.profit_factor:.2f}",
            f"Total return over period:    {self.total_return_pct:+.2f}%",
            f"Max drawdown:                {self.max_drawdown_pct:.2f}%",
            "-" * 60,
            f"Raw directional hit rate:    {self.directional_hit_rate:.1f}%  "
            f"(next-bar-close direction matched signal, {self.directional_signals} signals — "
            f"this is the closest analog to a binary-option 'accuracy' number, "
            f"and it is NOT the same as profitability above)",
            "=" * 60,
        ]
        return "\n".join(lines)


def run_backtest(
    df: pd.DataFrame,
    max_hold_bars: int = 20,
    fee: float = config.TAKER_FEE,
    slippage: float = config.SLIPPAGE,
) -> BacktestResult:
    """
    Walk forward through df bar by bar. On each bar, ask the signal engine
    for a call. If flat and the call is BUY/SELL, "enter" at that bar's
    close. While in a position, exit when the signal flips to the opposite
    direction (or HOLD-then-opposite), or after max_hold_bars, whichever
    comes first — exiting at that bar's close.
    """
    enriched = add_all_indicators(df)
    warmup = max(config.EMA_TREND, config.SR_LOOKBACK) + 2
    if len(enriched) < warmup + 10:
        raise ValueError("Not enough candles for a meaningful backtest (need warmup + several hundred bars).")

    result = BacktestResult()
    equity = 1.0
    result.equity_curve.append(equity)

    position = None  # dict: direction, entry_idx, entry_price
    hit_count = 0
    hit_total = 0

    n = len(enriched)
    for i in range(warmup, n - 1):  # -1 so we can always look at next bar's close for directional check
        sig = signal_at(enriched, i)

        # Directional "accuracy" bookkeeping (informational only, see summary()).
        if sig.direction in ("BUY", "SELL"):
            next_close = enriched.iloc[i + 1].close
            this_close = enriched.iloc[i].close
            actual_up = next_close > this_close
            predicted_up = sig.direction == "BUY"
            hit_total += 1
            if predicted_up == actual_up:
                hit_count += 1

        # Position management (PnL backtest).
        if position is None:
            if sig.direction in ("BUY", "SELL"):
                position = {
                    "direction": sig.direction,
                    "entry_idx": i,
                    "entry_time": enriched.index[i],
                    "entry_price": enriched.iloc[i].close,
                }
        else:
            bars_held = i - position["entry_idx"]
            opposite = (position["direction"] == "BUY" and sig.direction == "SELL") or (
                position["direction"] == "SELL" and sig.direction == "BUY"
            )
            timed_out = bars_held >= max_hold_bars
            if opposite or timed_out:
                exit_price = enriched.iloc[i].close
                raw_move = (exit_price - position["entry_price"]) / position["entry_price"]
                if position["direction"] == "SELL":
                    raw_move = -raw_move
                cost = 2 * (fee + slippage)  # entry + exit, each paying fee+slippage
                pnl_pct = raw_move - cost
                trade = Trade(
                    direction=position["direction"],
                    entry_time=position["entry_time"],
                    entry_price=position["entry_price"],
                    exit_time=enriched.index[i],
                    exit_price=exit_price,
                    bars_held=bars_held,
                    pnl_pct=pnl_pct,
                )
                result.trades.append(trade)
                equity *= (1 + pnl_pct)
                position = None
                # If the new signal itself is a fresh opposite entry, open it now.
                if opposite and sig.direction in ("BUY", "SELL"):
                    position = {
                        "direction": sig.direction,
                        "entry_idx": i,
                        "entry_time": enriched.index[i],
                        "entry_price": enriched.iloc[i].close,
                    }
        result.equity_curve.append(equity)

    result.directional_signals = hit_total
    result.directional_hit_rate = (100.0 * hit_count / hit_total) if hit_total else 0.0
    return result


if __name__ == "__main__":
    from data_fetcher import fetch_ohlcv_history

    print(f"Fetching {config.BACKTEST_CANDLES} candles of {config.SYMBOL} {config.TIMEFRAME} from {config.EXCHANGE_ID}...")
    df = fetch_ohlcv_history()
    print(f"Got {len(df)} candles, {df.index[0]} -> {df.index[-1]}")

    result = run_backtest(df)
    print(result.summary())
