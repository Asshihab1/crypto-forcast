"""
Central configuration for the analysis bot.

Nothing in this file requires API keys to run the analysis/backtest side of
the bot — Binance's market-data endpoints are public. API keys are only
needed if you later choose to place real orders yourself (this bot does not
place trades automatically — see README.md).
"""

import os
from dotenv import load_dotenv

load_dotenv()

# --- Market ---------------------------------------------------------------
EXCHANGE_ID = "binance"        # ccxt exchange id; e.g. "binance", "kraken", "coinbase"
SYMBOL = "BTC/USDT"            # any pair the exchange lists, e.g. "ETH/USDT"
TIMEFRAME = "15m"              # candle size: "1m","5m","15m","1h","4h","1d", ...

# --- Indicator settings -----------------------------------------------------
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND = 50
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20
BB_STD = 2
ATR_PERIOD = 14
SR_LOOKBACK = 50               # bars used to detect swing support/resistance

# --- Signal engine ----------------------------------------------------------
# Minimum confidence (0-100) before the bot will surface a BUY/SELL call
# instead of HOLD. Raising this makes the bot pickier (fewer, higher-quality
# signals); lowering it produces more signals of lower average quality.
MIN_CONFIDENCE = 60

# --- Backtest ---------------------------------------------------------------
BACKTEST_CANDLES = 2000        # how many historical candles to pull for a backtest
TAKER_FEE = 0.001              # 0.1% per side, typical Binance spot taker fee
SLIPPAGE = 0.0005              # 0.05% assumed slippage per fill — tune to your pair's liquidity

# --- Alerts (optional) -------------------------------------------------------
# Leave both blank to just print signals to the console.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Live loop ---------------------------------------------------------------
POLL_SECONDS = 60              # how often bot.py checks for a new closed candle
