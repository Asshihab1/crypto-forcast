# Crypto Chart-Analysis Assistant

A rules-based bot that watches a real market (via a regulated exchange's
public API), computes standard technical indicators, and gives you a
BUY / SELL / HOLD signal with a confidence score and its reasoning — so
you can make the final call. **It does not place trades for you and it
does not use Quotex or any binary options platform** — see the "Why not
Quotex / binary options" note at the bottom for the reasoning.

## What's in here

| File | Purpose |
|---|---|
| `config.py` | All settings: exchange, symbol, timeframe, indicator periods, fees. |
| `data_fetcher.py` | Pulls live/historical candles from the exchange (public data, no API key needed). |
| `indicators.py` | EMA, RSI, MACD, Bollinger Bands, ATR, swing support/resistance — plain pandas, fully auditable. |
| `signal_engine.py` | Combines indicators into one BUY/SELL/HOLD call with a 0-100 confidence score and reasons. |
| `backtester.py` | Walks the signal engine over history with real fees/slippage and reports honest win rate, expectancy, drawdown. |
| `bot.py` | Live loop: polls for new candles, prints/alerts a signal. No auto-trading. |
| `alerts.py` | Optional Telegram alerts (falls back to console-only). |
| `forecast.py` | Prophet-based trend forecast (day/month/year) on daily closes — separate from the rules-based signal. |
| `dashboard.py` | Optional web dashboard (Flask + Socket.IO, port 3100) — real-time chart per coin, live signal, on-demand backtest and Prophet forecast, in a browser. |

## Setup

```bash
cd crypto-analysis-bot
pip install -r requirements.txt
```

Edit `config.py` (or set environment variables) to pick your exchange,
symbol, and timeframe. Defaults to Binance BTC/USDT on 15-minute candles.

If Binance isn't accessible from where you are, `EXCHANGE_ID` also works
with `"kraken"`, `"coinbase"`, `"bybit"`, etc. — anything [ccxt](https://github.com/ccxt/ccxt)
supports, since market-data calls are public on all of them.

**Note on where to run this:** this needs normal outbound internet access
to reach the exchange's API. It won't run inside a network-sandboxed
environment (which is why I built and unit-tested the logic here using
synthetic data, but couldn't run a live fetch from this session — see
"How this was tested" below). Run it on your own computer or a VPS.

## Step 1 — always run the backtest first

Before you look at a single live signal, find out whether this logic has
any real edge on the pair/timeframe you care about:

```bash
python backtester.py
```

This fetches `config.BACKTEST_CANDLES` (default 2000) historical candles
and walks forward through them bar by bar, generating a signal at each
point using *only* data available up to that point (no lookahead), then
simulates entering/exiting a position on each signal with real fees and
slippage. It prints:

- **Win rate** — % of trades that were profitable
- **Expectancy per trade** — average P&L% per trade, net of costs (the
  single most important number: if this is negative, the strategy loses
  money even if the win rate looks fine)
- **Profit factor, total return, max drawdown**
- **Raw directional hit rate** — the % of BUY/SELL calls where the very
  next candle closed in the predicted direction. This is the closest
  analog to the "accuracy" percentage people quote for binary-option
  bots. Expect this number to land somewhere in the 45-60% range on
  liquid pairs, not 80-90% — and note it is *not* the same as the
  profitability numbers above.

Try different symbols, timeframes, and `MIN_CONFIDENCE` thresholds in
`config.py` and re-run. If you can't get positive expectancy after
reasonable tuning, that's real information — don't run it live.

## Step 2 — run it live as an assistant

```bash
python bot.py
```

This polls for newly closed candles and prints (and optionally Telegram-
alerts) each non-HOLD signal with its confidence score and the specific
indicators behind it, e.g.:

```
[2026-09-21 10:15:00+00:00] BUY  (confidence 72/100)  price=63241.5000
  - EMA stack bullish (fast > slow > trend) — uptrend alignment
  - MACD histogram just crossed positive — bullish momentum shift
  - RSI 58.3 above midline — mild bullish bias
  - Volume well above average, confirming the bullish move
```

### Optional: web dashboard

```bash
python dashboard.py
```

Serves a browser dashboard at `http://localhost:3100`:

- **Coin picker** — pick from `config.COINS` (BTC, BNB, LTC, ETH, SOL, XRP by
  default; add any pair your exchange lists).
- **Real-time chart** — candlestick history plus a live price line pushed
  over a websocket (Socket.IO), updated every `config.REALTIME_POLL_SECONDS`.
- **Signal panel** — same closed-candle rules-based signal as `bot.py`, per
  coin.
- **Entry suggestion** — combines the live signal, backtest expectancy, and
  forecast trend into one ENTRY_LONG / ENTRY_SHORT / AVOID / WAIT verdict,
  with every input it used spelled out. Only calls an entry when the
  near-term signal, longer-term forecast, and historical backtest
  expectancy all agree; disagreement or missing data (backtest/forecast not
  run yet) falls back to WAIT. Still rules on rules, not a prediction —
  read the reasons, not just the badge.
- **Backtest** — runs `backtester.py` for the selected coin on demand.
- **Forecast** — runs `forecast.py` (Prophet) for day/month/year horizons on
  demand; plots the projection and uncertainty band on the chart. This is a
  separate, purely statistical trend extrapolation — it does not feed into
  the BUY/SELL/HOLD signal above, and per `forecast.py`'s docstring, the
  widening uncertainty band further out is the honest part of the output.

Still read-only, no order placement.

For Telegram alerts, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
(via a `.env` file or environment variables) — get a token from
[@BotFather](https://t.me/BotFather) and your chat ID from
[@userinfobot](https://t.me/userinfobot).

**It never submits an order.** Extending it to place real trades is a
deliberate extra step I didn't wire up by default — if you want that,
add it explicitly (e.g. `exchange.create_order(...)` with your own API
keys loaded from environment variables, never hardcoded), and I'd
strongly suggest running against the exchange's **testnet/paper-trading
mode** for weeks before touching real funds, with a hard-coded max
position size and a daily loss limit that halts the bot.

## How this was tested

The sandbox this bot was built in has outbound network restricted to an
allowlist that doesn't include exchange APIs (Binance/Kraken/Coinbase were
all blocked at the proxy level when I tried). So:

- Every module was unit-tested with **synthetic OHLCV data** (random walks
  and mixed-regime series I generated locally) to confirm the indicators,
  signal engine, and backtester run correctly, handle edge cases, and
  produce sane, auditable output — a backtest on synthetic noise
  correctly showed poor results, which is the expected, honest outcome.
- The `data_fetcher.py` / `bot.py` / `backtester.py` live-data paths use
  standard, well-tested `ccxt` calls, but I have not been able to run
  them end-to-end against a live exchange from here. **Run
  `python backtester.py` yourself first** — if anything's wrong with the
  live data path it'll surface immediately as an error, before you ever
  get to `bot.py`.

## Honest expectations

Read `signal_engine.py`'s module docstring — it explains why the
confidence score is a measure of indicator agreement, not a probability
of being right. On liquid crypto pairs, a genuinely decent rules-based
strategy typically nets out to a small, positive expectancy per trade
after costs — often a win rate not far above 50%, made profitable by
letting winners run longer than losers (asymmetric exits), not by being
"right" 80-90% of the time. If backtester.py shows something close to
80-90% win rate on real market data, the far more likely explanation is
a bug or overfitting, not a breakthrough — go back and check for
lookahead bias or an unrealistically favorable exit rule before trusting
it.

## Why not Quotex / binary options

This was built as the legitimate alternative after I explained why I
wouldn't build the Quotex/binary-options version: those platforms'
"OTC" pairs are synthetic prices generated by the broker itself (not a
real market), the broker is typically your counterparty rather than a
neutral exchange, and short up/down windows don't support the accuracy
needed to beat their payout structure. Everything in this project instead
points at a regulated exchange with real market data and no house edge
baked into the payout.

## One more thing, since you're trading from Bangladesh

I don't have live web access from this session to check current specifics,
but Bangladesh Bank has historically treated cryptocurrency transactions
and unauthorized foreign-currency trading as falling under foreign
exchange control law, and has issued public warnings on this. Please
verify the current rules with Bangladesh Bank or a local financial/legal
advisor before depositing real money anywhere — this applies regardless
of which platform or bot you use.

This project and its outputs are for educational/informational purposes,
not financial advice.
# crypto-forcast
