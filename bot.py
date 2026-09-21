"""
Live assistant loop.

IMPORTANT — what this does and does not do:
  - It fetches live candles, generates a signal, and ALERTS you (console
    and, if configured, Telegram). It does NOT place any trade for you.
  - You decide whether to act on a signal. Nothing here touches your
    exchange account, holds API secret keys for trading, or submits orders.
    That's deliberate: see README.md for why "assist" stops there.

Run: python bot.py
Stop: Ctrl+C
"""

from __future__ import annotations
import time
import traceback

import config
from data_fetcher import get_exchange, fetch_ohlcv
from signal_engine import generate_signal
from alerts import notify


def main():
    exchange = get_exchange()
    print(f"Watching {config.SYMBOL} on {config.EXCHANGE_ID}, timeframe {config.TIMEFRAME}.")
    print(f"Polling every {config.POLL_SECONDS}s for a newly closed candle. Ctrl+C to stop.\n")

    last_seen_ts = None

    while True:
        try:
            # Fetch a bit more than the minimum so indicators are stable.
            df = fetch_ohlcv(limit=max(200, config.EMA_TREND + config.SR_LOOKBACK + 10), exchange=exchange)

            # The most recent row from most exchanges' REST APIs is the
            # currently-forming (unclosed) candle. Drop it so we only ever
            # signal on fully closed candles — using a still-forming candle
            # would let the "signal" change out from under you mid-bar.
            closed = df.iloc[:-1]
            latest_ts = closed.index[-1]

            if latest_ts != last_seen_ts:
                last_seen_ts = latest_ts
                signal = generate_signal(closed)
                if signal.direction != "HOLD":
                    notify(str(signal))
                else:
                    print(str(signal))
            time.sleep(config.POLL_SECONDS)

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception:
            print("[bot] Error during poll loop, will retry:")
            traceback.print_exc()
            time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
