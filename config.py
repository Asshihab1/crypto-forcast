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

# Coins selectable in the dashboard (symbol -> display label). Add any pair
# your exchange lists — market data is public, no API key needed.
COINS = {
    "BTC/USDT": "Bitcoin",
    "BNB/USDT": "BNB",
    "LTC/USDT": "Litecoin",
    "ETH/USDT": "Ethereum",
    "SOL/USDT": "Solana",
    "XRP/USDT": "XRP",
}

# --- Forecast (Prophet) -----------------------------------------------------
# Horizon key -> which candle timeframe to fit/forecast on, how many candles
# ahead ("periods"), the pandas frequency alias for that candle size, and how
# much history to pull for fitting. Short intraday horizons fit on intraday
# candles (Prophet has far less signal to work with there — treat these as
# even less reliable than the longer horizons); month/year fit on daily
# candles regardless of the chosen chart timeframe.
FORECAST_HORIZONS = {
    "15m":   {"timeframe": "15m", "periods": 1,   "freq": "15min", "history_candles": 2000},
    "30m":   {"timeframe": "30m", "periods": 1,   "freq": "30min", "history_candles": 2000},
    "1h":    {"timeframe": "1h",  "periods": 1,   "freq": "h",     "history_candles": 2000},
    "4h":    {"timeframe": "4h",  "periods": 1,   "freq": "4h",    "history_candles": 1000},
    "day":   {"timeframe": "1d",  "periods": 1,   "freq": "D",     "history_candles": 730},
    "month": {"timeframe": "1d",  "periods": 30,  "freq": "D",     "history_candles": 730},
    "year":  {"timeframe": "1d",  "periods": 365, "freq": "D",     "history_candles": 730},
}

# --- Entry sizing (dashboard entry-suggestion card) --------------------------
# Stop-loss / take-profit distance from entry, in multiples of ATR (average
# true range) — a volatility-scaled distance, not a fixed price offset.
ATR_STOP_MULT = 1.5
ATR_TARGET_MULT = 3.0   # 2:1 reward:risk at these defaults

# --- Dashboard chart ---------------------------------------------------------
# One unified time control drives the chart timeframe, indicators, and the
# forecast horizon at once — key -> (label, candle timeframe, forecast
# horizon key). Binance has no native 45m candle, so the ladder below is the
# closest increasing sequence it actually supports.
CHART_TIMEFRAME = "15m"         # default candle size for the live dashboard chart
TIME_OPTIONS = [
    {"key": "15m", "label": "15m", "timeframe": "15m", "horizon": "15m"},
    {"key": "30m", "label": "30m", "timeframe": "30m", "horizon": "30m"},
    {"key": "1h",  "label": "1h",  "timeframe": "1h",  "horizon": "1h"},
    {"key": "4h",  "label": "4h",  "timeframe": "4h",  "horizon": "4h"},
    {"key": "1d",  "label": "1D",  "timeframe": "1d",  "horizon": "day"},
    {"key": "1mo", "label": "1M",  "timeframe": "1d",  "horizon": "month"},
    {"key": "1y",  "label": "1Y",  "timeframe": "1d",  "horizon": "year"},
]
CHART_HISTORY_LIMIT = 300       # candles to load for the initial chart draw
REALTIME_POLL_SECONDS = 5       # how often the dashboard pushes live price ticks

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
