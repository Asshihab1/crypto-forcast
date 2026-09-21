"""
Web dashboard for the analysis bot.

Runs a Flask + Socket.IO server (default port 3100):
  - Live candlestick chart per coin (BTC/BNB/LTC/... from config.COINS),
    updated in real time over a websocket.
  - Rules-based signal panel (same logic as bot.py), per coin.
  - On-demand backtest (backtester.py) for the selected coin.
  - On-demand Prophet forecast (forecast.py) for day/month/year horizons.

Read-only: still never places a trade.

Run: python dashboard.py
"""

from __future__ import annotations
import threading
import time
import traceback
from dataclasses import asdict

from flask import Flask, jsonify, render_template_string, request
from flask_socketio import SocketIO

import config
from data_fetcher import get_exchange, fetch_ohlcv, fetch_ohlcv_history
from indicators import add_all_indicators
from signal_engine import generate_signal
from backtester import run_backtest
from forecast import run_forecast

app = Flask(__name__)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

_lock = threading.Lock()
_signals: dict[str, dict] = {}          # symbol -> {"signal": ..., "updated_at": ..., "error": ...}
_backtest_cache: dict[str, dict] = {}   # symbol -> {"running": bool, "result": ...}
_forecast_cache: dict[str, dict] = {}   # f"{symbol}|{horizon}" -> {"running": bool, "result": ...}


def _signal_to_dict(signal) -> dict:
    d = asdict(signal)
    d["timestamp"] = str(signal.timestamp)
    return d


def _signal_poll_loop():
    """Recompute the rules-based signal for every configured coin on a loop."""
    exchange = get_exchange()
    last_seen: dict[str, object] = {}
    while True:
        for symbol in config.COINS:
            try:
                df = fetch_ohlcv(
                    symbol=symbol,
                    limit=max(200, config.EMA_TREND + config.SR_LOOKBACK + 10),
                    exchange=exchange,
                )
                closed = df.iloc[:-1]
                latest_ts = closed.index[-1]
                if last_seen.get(symbol) != latest_ts:
                    last_seen[symbol] = latest_ts
                    signal = generate_signal(closed)
                    atr_val = float(add_all_indicators(closed)["atr"].iloc[-1])
                    with _lock:
                        _signals[symbol] = {
                            "signal": _signal_to_dict(signal),
                            "atr": atr_val,
                            "updated_at": time.time(),
                            "error": None,
                        }
                    socketio.emit("signal", {"symbol": symbol, **_signals[symbol]})
            except Exception as exc:
                traceback.print_exc()
                with _lock:
                    _signals[symbol] = {"signal": None, "updated_at": time.time(), "error": str(exc)}
        time.sleep(config.POLL_SECONDS)


def _realtime_price_loop():
    """Push a live price tick per coin over the websocket every few seconds."""
    exchange = get_exchange()
    while True:
        for symbol in config.COINS:
            try:
                ticker = exchange.fetch_ticker(symbol)
                price = ticker.get("last") or ticker.get("close")
                if price is not None:
                    socketio.emit("price", {"symbol": symbol, "price": float(price), "ts": time.time()})
            except Exception:
                traceback.print_exc()
        time.sleep(config.REALTIME_POLL_SECONDS)


def _run_backtest_async(symbol: str):
    def work():
        try:
            df = fetch_ohlcv_history(symbol=symbol)
            result = run_backtest(df)
            with _lock:
                _backtest_cache[symbol] = {
                    "running": False,
                    "result": {
                        "n_trades": result.n_trades,
                        "win_rate": result.win_rate,
                        "avg_win_pct": result.avg_win_pct,
                        "avg_loss_pct": result.avg_loss_pct,
                        "expectancy_pct": result.expectancy_pct,
                        "profit_factor": result.profit_factor,
                        "total_return_pct": result.total_return_pct,
                        "max_drawdown_pct": result.max_drawdown_pct,
                        "directional_hit_rate": result.directional_hit_rate,
                        "directional_signals": result.directional_signals,
                    },
                }
        except Exception as exc:
            traceback.print_exc()
            with _lock:
                _backtest_cache[symbol] = {"running": False, "result": {"error": str(exc)}}

    with _lock:
        if _backtest_cache.get(symbol, {}).get("running"):
            return
        _backtest_cache[symbol] = {"running": True, "result": _backtest_cache.get(symbol, {}).get("result")}
    threading.Thread(target=work, daemon=True).start()


def _compute_entry_verdict(symbol: str) -> dict:
    """
    Combine the three independent views this dashboard already produces —
    live confluence signal, historical backtest expectancy, and Prophet
    trend forecast — into one entry verdict with the reasoning spelled out.

    This is still just rules stacked on rules, not a prediction: it only
    says ENTRY_LONG/ENTRY_SHORT when the near-term signal, the longer-term
    forecast trend, and a positive historical expectancy all agree; any
    disagreement or missing data falls back to WAIT.
    """
    with _lock:
        sig_entry = _signals.get(symbol)
        bt_entry = _backtest_cache.get(symbol, {}).get("result")
        fc_entry = None
        for h in ("15m", "30m", "4h", "day", "month", "year"):
            cached = _forecast_cache.get(f"{symbol}|{h}")
            if cached and not cached.get("running") and cached.get("result") and "error" not in cached["result"]:
                fc_entry = cached["result"]
                break

    if not sig_entry or not sig_entry.get("signal"):
        return {"verdict": "WAIT", "reasons": ["No live signal yet — wait for the first closed candle."]}

    signal = sig_entry["signal"]
    direction = signal["direction"]
    entry_price = signal["price"]
    reasons = [f"Signal: {direction} (confidence {signal['confidence']:.0f}/100)"]

    edge_ok = None
    if bt_entry and "error" not in bt_entry:
        edge_ok = bt_entry["expectancy_pct"] > 0
        reasons.append(
            f"Backtest expectancy: {bt_entry['expectancy_pct']:+.3f}% per trade "
            f"({'positive edge' if edge_ok else 'no edge / negative'})"
        )
    else:
        reasons.append("Backtest not run yet — run it to confirm this logic has any historical edge before trusting a signal.")

    fc_dir = None
    if fc_entry:
        fc_dir = "up" if fc_entry["projected_change_pct"] > 0 else "down"
        reasons.append(f"Forecast ({fc_entry['horizon']}): {fc_entry['projected_change_pct']:+.2f}% projected ({fc_dir})")
    else:
        reasons.append("Forecast not run yet — run it to check whether the longer-term trend agrees with the signal.")

    if direction == "HOLD":
        verdict = "WAIT"
        reasons.append("No aligned indicator edge right now — this is exactly what HOLD means.")
    elif edge_ok is False:
        verdict = "AVOID"
        reasons.append("Backtest shows negative/no expectancy for this exact symbol+timeframe+config — a live call here hasn't proven profitable net of costs.")
    elif fc_dir is not None and ((direction == "BUY" and fc_dir == "down") or (direction == "SELL" and fc_dir == "up")):
        verdict = "WAIT"
        reasons.append("Near-term signal and longer-term forecast trend disagree — no aligned edge, better to wait.")
    elif direction == "BUY" and edge_ok is not False and fc_dir in ("up", None):
        verdict = "ENTRY_LONG"
    elif direction == "SELL" and edge_ok is not False and fc_dir in ("down", None):
        verdict = "ENTRY_SHORT"
    else:
        verdict = "WAIT"

    result = {
        "verdict": verdict,
        "direction": direction,
        "entry_price": entry_price,
        "reasons": reasons,
    }

    atr_val = sig_entry.get("atr")
    if verdict in ("ENTRY_LONG", "ENTRY_SHORT") and atr_val:
        stop_dist = config.ATR_STOP_MULT * atr_val
        target_dist = config.ATR_TARGET_MULT * atr_val
        if verdict == "ENTRY_LONG":
            stop_price = entry_price - stop_dist
            target_price = entry_price + target_dist
        else:
            stop_price = entry_price + stop_dist
            target_price = entry_price - target_dist
        result["stop_price"] = stop_price
        result["target_price"] = target_price
        result["risk_reward"] = config.ATR_TARGET_MULT / config.ATR_STOP_MULT
        reasons.append(
            f"Stop {stop_price:.4f} / target {target_price:.4f} "
            f"({config.ATR_STOP_MULT:g}x/{config.ATR_TARGET_MULT:g}x ATR — {result['risk_reward']:.1f}:1 reward:risk)"
        )

    return result


def _run_forecast_async(symbol: str, horizon: str):
    key = f"{symbol}|{horizon}"

    def work():
        try:
            result = run_forecast(symbol, horizon)
            with _lock:
                _forecast_cache[key] = {"running": False, "result": result}
        except Exception as exc:
            traceback.print_exc()
            with _lock:
                _forecast_cache[key] = {"running": False, "result": {"error": str(exc)}}

    with _lock:
        if _forecast_cache.get(key, {}).get("running"):
            return
        _forecast_cache[key] = {"running": True, "result": _forecast_cache.get(key, {}).get("result")}
    threading.Thread(target=work, daemon=True).start()


_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Crypto Analysis Bot</title>
  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background:#0b0e14; color:#e6e6e6; padding:1.5rem 2rem; width:100%;
    }
    .layout { display:grid; grid-template-columns: minmax(0, 2.2fr) minmax(280px, 1fr); gap:1.25rem; align-items:start; }
    @media (max-width: 1000px) { .layout { grid-template-columns: 1fr; } }
    .col { min-width:0; }
    h1 { font-size:1.1rem; font-weight:600; color:#9aa4b2; letter-spacing:0.02em; margin-bottom:1.25rem; }
    .buy { color:#3ddc84; } .sell { color:#ff5c5c; } .hold { color:#c9c9c9; }
    .card {
      background:#12161f; border:1px solid #232838; border-radius:12px;
      padding:1.25rem 1.5rem; margin-bottom:1.25rem;
    }
    .card h2 { margin:0 0 0.25rem 0; font-size:1.4rem; }
    .meta { color:#7d8797; font-size:0.85rem; margin-bottom:0.75rem; }
    ul.reasons { margin:0; padding-left:1.1rem; }
    ul.reasons li { margin:0.3rem 0; font-size:0.92rem; color:#c3cad6; }
    .updated { color:#5a6472; font-size:0.78rem; margin-top:0.9rem; }
    .toolbar { display:flex; gap:0.75rem; align-items:center; flex-wrap:wrap; margin-bottom:1rem; }
    select, button {
      background:#1a1f2b; color:#e6e6e6; border:1px solid #2d3340; padding:0.55rem 0.9rem;
      border-radius:8px; font-size:0.9rem; font-weight:500; cursor:pointer;
    }
    button.primary { background:#238636; border-color:#238636; color:white; }
    button:disabled { background:#2d3340; color:#7d8797; cursor:default; }
    .live-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#3ddc84; margin-right:0.4rem; }
    .live-dot.off { background:#5a6472; }
    #chart { width:100%; height:380px; }
    .section-title { font-size:0.8rem; text-transform:uppercase; letter-spacing:0.06em; color:#7d8797; margin-bottom:0.9rem; }
    .stat-grid {
      display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr));
      gap:0.75rem; margin-top:1rem;
    }
    .stat {
      background:#0e131c; border:1px solid #1f2432; border-radius:10px; padding:0.85rem 1rem;
    }
    .stat .label { font-size:0.72rem; text-transform:uppercase; letter-spacing:0.05em; color:#6b7482; margin-bottom:0.35rem; }
    .stat .value { font-size:1.25rem; font-weight:600; }
    .pos { color:#3ddc84; } .neg { color:#ff5c5c; } .neutral { color:#e6e6e6; }
    .verdict {
      margin-top:1rem; padding:0.75rem 1rem; border-radius:8px; font-size:0.88rem;
      background:#1a1512; border:1px solid #3d2f1a; color:#e0b96b;
    }
    .verdict.good { background:#11201a; border-color:#1e3d2f; color:#5fd99a; }
    .placeholder { color:#5a6472; font-size:0.9rem; }
    .overview { margin-top:1rem; border:1px solid #232838; border-radius:10px; overflow:hidden; }
    .overview summary {
      cursor:pointer; padding:0.7rem 1rem; font-size:0.82rem; color:#9aa4b2;
      background:#0e131c; list-style:none; user-select:none;
    }
    .overview summary::-webkit-details-marker { display:none; }
    .overview summary::before { content:'\\25B8  '; }
    .overview[open] summary::before { content:'\\25BE  '; }
    .overview-body { padding:0.9rem 1rem; font-size:0.83rem; color:#c3cad6; }
    .overview-row { display:flex; justify-content:space-between; padding:0.3rem 0; border-bottom:1px solid #1c2130; }
    .overview-row:last-child { border-bottom:none; }
    .overview-row .k { color:#7d8797; }
    .seasonality-tag { display:inline-block; padding:0.15rem 0.5rem; border-radius:4px; font-size:0.72rem; margin-left:0.3rem; }
    .seasonality-tag.on { background:#1e3d2f; color:#5fd99a; }
    .seasonality-tag.off { background:#232838; color:#5a6472; }
    .entry-card { border-width:2px; }
    .entry-card.entry-long { border-color:#1e3d2f; background:#0f1a15; }
    .entry-card.entry-short { border-color:#3d1f1f; background:#1a1212; }
    .entry-card.entry-avoid { border-color:#3d1f1f; background:#1a1212; }
    .entry-card.entry-wait { border-color:#2d3340; }
    .entry-badge { display:inline-block; padding:0.3rem 0.8rem; border-radius:6px; font-weight:700; font-size:1rem; letter-spacing:0.03em; }
    .entry-badge.entry-long { background:#1e3d2f; color:#5fd99a; }
    .entry-badge.entry-short { background:#3d1f1f; color:#ff8f8f; }
    .entry-badge.entry-avoid { background:#3d1f1f; color:#ff8f8f; }
    .entry-badge.entry-wait { background:#2d3340; color:#c3cad6; }
    .entry-card ul { margin:0.75rem 0 0 1.1rem; padding:0; }
    .entry-card li { margin:0.3rem 0; font-size:0.9rem; color:#c3cad6; }
    .disclaimer { color:#5a6472; font-size:0.75rem; margin-top:0.9rem; }
    .entry-segments {
      display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr));
      gap:0.75rem; margin:0.9rem 0;
    }
    .segment { background:#0e131c; border:1px solid #1f2432; border-radius:10px; padding:0.85rem 1rem; }
    .segment .label { font-size:0.72rem; text-transform:uppercase; letter-spacing:0.05em; color:#6b7482; margin-bottom:0.35rem; }
    .segment .value { font-size:1.15rem; font-weight:600; }
    .dir-up { color:#3ddc84; } .dir-down { color:#ff5c5c; } .dir-flat { color:#c9c9c9; }
  </style>
</head>
<body>
  <h1><span class="live-dot off" id="live-dot"></span>Crypto Analysis Bot &middot; {{ exchange }}</h1>

  <div class="toolbar">
    <select id="coin-select">
      {% for symbol, label in coins.items() %}
      <option value="{{ symbol }}">{{ label }} ({{ symbol }})</option>
      {% endfor %}
    </select>
    <select id="tf-select">
      {% for tf in chart_timeframes %}
      <option value="{{ tf }}" {% if tf == default_timeframe %}selected{% endif %}>{{ tf }}</option>
      {% endfor %}
    </select>
    <span id="last-price" class="meta"></span>
  </div>

  <div class="layout">
    <div class="col">
      <div class="card">
        <div id="chart"></div>
        <div id="rsi-chart" style="margin-top:0.4rem"></div>
        <div id="macd-chart" style="margin-top:0.4rem"></div>
      </div>

      <div class="card entry-card" id="entry-card"><span class="placeholder">Loading...</span></div>

      <div class="card">
        <div class="section-title">Position calculator</div>
        <div class="toolbar">
          <input id="amount-input" type="number" min="0" step="any" placeholder="Investment amount ($)">
        </div>
        <div id="position-out"><span class="placeholder">Enter an amount to size the current entry.</span></div>
      </div>
    </div>

    <div class="col">
      <div class="card" id="signal-card"><span class="placeholder">Loading signal...</span></div>

      <div class="card">
        <div class="section-title">Prophet forecast</div>
        <div class="toolbar">
          <select id="horizon-select">
            {% for key in horizons %}
            <option value="{{ key }}">{{ key }}</option>
            {% endfor %}
          </select>
          <button class="primary" id="forecast-btn" onclick="runForecast()">Run forecast</button>
        </div>
        <div id="forecast-out"></div>
      </div>

      <div class="card">
        <div class="section-title">Backtest</div>
        <button id="bt-btn" onclick="runBacktest()">Run backtest ({{ backtest_candles }} candles)</button>
        <div id="backtest-out"></div>
      </div>
    </div>
  </div>

  <script>
    const COINS = {{ coins_json|safe }};
    let currentSymbol = document.getElementById('coin-select').value;
    let currentTimeframe = document.getElementById('tf-select').value;
    const chartEl = document.getElementById('chart');
    const chart = LightweightCharts.createChart(chartEl, {
      layout: { background: { color: '#12161f' }, textColor: '#c3cad6' },
      grid: { vertLines: { color: '#1c2130' }, horzLines: { color: '#1c2130' } },
      rightPriceScale: { borderColor: '#232838' },
      timeScale: { borderColor: '#232838', timeVisible: true },
      height: 380,
    });

    const rsiChart = LightweightCharts.createChart(document.getElementById('rsi-chart'), {
      layout: { background: { color: '#12161f' }, textColor: '#c3cad6' },
      grid: { vertLines: { color: '#1c2130' }, horzLines: { color: '#1c2130' } },
      rightPriceScale: { borderColor: '#232838' },
      timeScale: { borderColor: '#232838', timeVisible: true, visible: false },
      height: 120,
    });
    const rsiSeries = rsiChart.addLineSeries({ color: '#8ab4f8', lineWidth: 1 });
    rsiSeries.createPriceLine({ price: 70, color: '#ff5c5c', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '70' });
    rsiSeries.createPriceLine({ price: 30, color: '#3ddc84', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '30' });

    const macdChart = LightweightCharts.createChart(document.getElementById('macd-chart'), {
      layout: { background: { color: '#12161f' }, textColor: '#c3cad6' },
      grid: { vertLines: { color: '#1c2130' }, horzLines: { color: '#1c2130' } },
      rightPriceScale: { borderColor: '#232838' },
      timeScale: { borderColor: '#232838', timeVisible: true, visible: false },
      height: 100,
    });
    const macdSeries = macdChart.addHistogramSeries({ priceFormat: { type: 'price', precision: 2 } });

    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range) return;
      rsiChart.timeScale().setVisibleLogicalRange(range);
      macdChart.timeScale().setVisibleLogicalRange(range);
    });
    chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.05, bottom: 0.28 } });
    const candleSeries = chart.addCandlestickSeries({
      upColor: '#3ddc84', downColor: '#ff5c5c', borderVisible: false,
      wickUpColor: '#3ddc84', wickDownColor: '#ff5c5c',
    });
    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
    const liveSeries = chart.addLineSeries({ color: '#e0b96b', lineWidth: 1 });
    let forecastSeries = null, forecastUpper = null, forecastLower = null;

    function clearForecastSeries() {
      [forecastSeries, forecastUpper, forecastLower].forEach(s => { if (s) chart.removeSeries(s); });
      forecastSeries = forecastUpper = forecastLower = null;
    }

    async function loadHistory() {
      clearForecastSeries();
      liveSeries.setData([]);
      const params = 'symbol=' + encodeURIComponent(currentSymbol) + '&timeframe=' + currentTimeframe;
      const [histRes, indRes] = await Promise.all([
        fetch('/api/history?' + params),
        fetch('/api/indicators?' + params),
      ]);
      const j = await histRes.json();
      const ind = await indRes.json();
      if (j.candles) candleSeries.setData(j.candles);
      if (j.volume) volumeSeries.setData(j.volume);
      if (ind.rsi) rsiSeries.setData(ind.rsi);
      if (ind.macd_hist) macdSeries.setData(ind.macd_hist);
      document.getElementById('last-price').textContent = COINS[currentSymbol] + ' — loading...';
    }

    document.getElementById('coin-select').addEventListener('change', (e) => {
      currentSymbol = e.target.value;
      loadHistory();
      refreshSignal();
      refreshEntry();
      document.getElementById('forecast-out').innerHTML = '';
    });

    document.getElementById('tf-select').addEventListener('change', (e) => {
      currentTimeframe = e.target.value;
      loadHistory();
    });

    // --- Realtime price via websocket ---------------------------------------
    const socket = io();
    socket.on('connect', () => document.getElementById('live-dot').classList.remove('off'));
    socket.on('disconnect', () => document.getElementById('live-dot').classList.add('off'));
    let lastLivePrice = null;
    socket.on('price', (msg) => {
      if (msg.symbol !== currentSymbol) return;
      lastLivePrice = msg.price;
      liveSeries.update({ time: Math.floor(msg.ts), value: msg.price });
      document.getElementById('last-price').textContent =
        COINS[currentSymbol] + ' — ' + msg.price.toLocaleString(undefined, { maximumFractionDigits: 4 });
      renderPosition();
    });
    socket.on('signal', (msg) => {
      if (msg.symbol === currentSymbol) { renderSignal(msg); refreshEntry(); }
    });

    // --- Entry suggestion ------------------------------------------------------
    const ENTRY_LABEL = {
      ENTRY_LONG: 'ENTRY — LONG', ENTRY_SHORT: 'ENTRY — SHORT', AVOID: 'AVOID', WAIT: 'WAIT',
    };
    const ENTRY_CLASS = {
      ENTRY_LONG: 'entry-long', ENTRY_SHORT: 'entry-short', AVOID: 'entry-avoid', WAIT: 'entry-wait',
    };

    let lastEntry = null;

    async function refreshEntry() {
      const r = await fetch('/api/entry?symbol=' + encodeURIComponent(currentSymbol));
      const j = await r.json();
      lastEntry = j;
      const card = document.getElementById('entry-card');
      const cls = ENTRY_CLASS[j.verdict] || 'entry-wait';
      card.className = 'card entry-card ' + cls;

      const dirCls = j.direction === 'BUY' ? 'dir-up' : (j.direction === 'SELL' ? 'dir-down' : 'dir-flat');
      const dirLabel = j.direction === 'BUY' ? 'UP' : (j.direction === 'SELL' ? 'DOWN' : 'FLAT');
      const hasEntry = j.verdict === 'ENTRY_LONG' || j.verdict === 'ENTRY_SHORT';
      const dash = '<span class="placeholder">—</span>';

      let marginPct = dash, profitPct = dash, stopVal = dash, targetVal = dash;
      if (hasEntry) {
        marginPct = (Math.abs(j.entry_price - j.stop_price) / j.entry_price * 100).toFixed(2) + '%';
        profitPct = (Math.abs(j.target_price - j.entry_price) / j.entry_price * 100).toFixed(2) + '%';
        stopVal = '<span class="neg">' + j.stop_price.toFixed(4) + '</span>';
        targetVal = '<span class="pos">' + j.target_price.toFixed(4) + '</span>';
      }

      card.innerHTML =
        '<span class="entry-badge ' + cls + '">' + (ENTRY_LABEL[j.verdict] || j.verdict) + '</span>' +
        '<div class="entry-segments">' +
          '<div class="segment"><div class="label">Entry</div><div class="value neutral">' +
            (j.entry_price !== undefined ? j.entry_price.toFixed(4) : dash) + '</div></div>' +
          '<div class="segment"><div class="label">Direction</div><div class="value ' + dirCls + '">' + dirLabel + '</div></div>' +
          '<div class="segment"><div class="label">Stop</div><div class="value">' + stopVal + '</div></div>' +
          '<div class="segment"><div class="label">Profit</div><div class="value">' + targetVal + '</div></div>' +
          '<div class="segment"><div class="label">Margin (risk)</div><div class="value neg">' + marginPct + '</div></div>' +
          '<div class="segment"><div class="label">Reward</div><div class="value pos">' + profitPct + '</div></div>' +
        '</div>';

      renderPosition();
    }

    // --- Position calculator -------------------------------------------------
    function renderPosition() {
      const out = document.getElementById('position-out');
      const amount = parseFloat(document.getElementById('amount-input').value);
      if (!amount || amount <= 0) { out.innerHTML = '<span class="placeholder">Enter an amount to size the current entry.</span>'; return; }
      if (!lastEntry || lastEntry.entry_price === undefined) { out.innerHTML = '<span class="placeholder">Waiting for a price...</span>'; return; }

      const qty = amount / lastEntry.entry_price;
      const hasEntry = lastEntry.verdict === 'ENTRY_LONG' || lastEntry.verdict === 'ENTRY_SHORT';

      let html = '<div class="stat-grid">' +
        '<div class="stat"><div class="label">Quantity @ ' + lastEntry.entry_price.toFixed(4) + '</div><div class="value neutral">' + qty.toFixed(6) + '</div></div>';

      if (lastLivePrice !== null) {
        const move = lastEntry.direction === 'SELL' ? (lastEntry.entry_price - lastLivePrice) : (lastLivePrice - lastEntry.entry_price);
        const pnlUsd = qty * move;
        const pnlCls = pnlUsd > 0 ? 'pos' : (pnlUsd < 0 ? 'neg' : 'neutral');
        const pnlLabel = pnlUsd > 0 ? 'Profit' : (pnlUsd < 0 ? 'Loss' : 'Flat');
        html += '<div class="stat"><div class="label">Unrealized (' + pnlLabel + ') @ ' + lastLivePrice.toFixed(4) + '</div><div class="value ' + pnlCls + '">' +
          (pnlUsd >= 0 ? '+' : '') + '$' + pnlUsd.toFixed(2) + '</div></div>';
      }

      if (hasEntry) {
        const profitUsd = qty * Math.abs(lastEntry.target_price - lastEntry.entry_price);
        const lossUsd = qty * Math.abs(lastEntry.entry_price - lastEntry.stop_price);
        html += '<div class="stat"><div class="label">Est. profit at target</div><div class="value pos">+$' + profitUsd.toFixed(2) + '</div></div>' +
          '<div class="stat"><div class="label">Est. loss at stop</div><div class="value neg">-$' + lossUsd.toFixed(2) + '</div></div>';
      }
      html += '</div>';
      if (!hasEntry) {
        html += '<div class="placeholder" style="margin-top:0.6rem">No active BUY/SELL entry right now (' + lastEntry.verdict +
          ') — quantity shown at current price, but no target/stop to size profit/loss against yet.</div>';
      }
      out.innerHTML = html;
    }

    document.getElementById('amount-input').addEventListener('input', () => {
      renderPosition();
      try { localStorage.setItem('investAmount', document.getElementById('amount-input').value); } catch (e) {}
    });
    try {
      const saved = localStorage.getItem('investAmount');
      if (saved) document.getElementById('amount-input').value = saved;
    } catch (e) {}

    // --- Signal panel --------------------------------------------------------
    function renderSignal(j) {
      const el = document.getElementById('signal-card');
      if (j.error) { el.innerHTML = '<b class="sell">Error:</b> ' + j.error; return; }
      if (!j.signal) { el.innerHTML = '<span class="placeholder">No signal yet — waiting on first closed candle.</span>'; return; }
      const s = j.signal;
      const cls = s.direction === 'BUY' ? 'buy' : (s.direction === 'SELL' ? 'sell' : 'hold');
      el.innerHTML = '<h2 class="' + cls + '">' + s.direction + ' <span style="font-size:0.9rem;color:#7d8797;font-weight:400">(confidence ' + s.confidence.toFixed(0) + '/100)</span></h2>' +
        '<div class="meta">price ' + s.price.toFixed(4) + ' &middot; ' + s.timestamp + '</div>' +
        '<ul class="reasons">' + s.reasons.map(r => '<li>' + r + '</li>').join('') + '</ul>' +
        '<div class="updated">updated ' + new Date(j.updated_at * 1000).toLocaleTimeString() + '</div>';
    }

    async function refreshSignal() {
      const r = await fetch('/api/signal?symbol=' + encodeURIComponent(currentSymbol));
      const j = await r.json();
      renderSignal(j);
    }

    // --- Forecast --------------------------------------------------------------
    async function runForecast() {
      const btn = document.getElementById('forecast-btn');
      const horizon = document.getElementById('horizon-select').value;
      btn.disabled = true;
      document.getElementById('forecast-out').innerHTML =
        '<div class="placeholder" style="margin-top:1rem">Fitting Prophet model on daily history, may take ~10-30s...</div>';
      await fetch('/api/forecast?symbol=' + encodeURIComponent(currentSymbol) + '&horizon=' + horizon, { method: 'POST' });
      pollForecast(currentSymbol, horizon);
    }

    function renderForecast(r) {
      const btn = document.getElementById('forecast-btn');
      btn.disabled = false;
      const out = document.getElementById('forecast-out');
      if (r.error) { out.innerHTML = '<div class="verdict"><b>Error:</b> ' + r.error + '</div>'; return; }

      clearForecastSeries();
      forecastSeries = chart.addLineSeries({ color: '#e0b96b', lineWidth: 2, lineStyle: 2 });
      forecastUpper = chart.addLineSeries({ color: 'rgba(224,185,107,0.35)', lineWidth: 1, lineStyle: 3 });
      forecastLower = chart.addLineSeries({ color: 'rgba(224,185,107,0.35)', lineWidth: 1, lineStyle: 3 });
      forecastSeries.setData(r.forecast.map(p => ({ time: p.time, value: p.yhat })));
      forecastUpper.setData(r.forecast.map(p => ({ time: p.time, value: p.yhat_upper })));
      forecastLower.setData(r.forecast.map(p => ({ time: p.time, value: p.yhat_lower })));

      const dir = r.projected_change_pct >= 0 ? 'pos' : 'neg';
      const fd = r.fit_details || {};
      const seasTag = (on) => '<span class="seasonality-tag ' + (on ? 'on' : 'off') + '">' + (on ? 'on' : 'off') + '</span>';

      out.innerHTML = '<div class="stat-grid">' +
        '<div class="stat"><div class="label">Last price</div><div class="value neutral">' + r.last_price.toFixed(2) + '</div></div>' +
        '<div class="stat"><div class="label">Horizon</div><div class="value neutral">' + r.periods + ' x ' + r.timeframe + '</div></div>' +
        '<div class="stat"><div class="label">Projected change</div><div class="value ' + dir + '">' +
          (r.projected_change_pct >= 0 ? '+' : '') + r.projected_change_pct.toFixed(2) + '%</div></div>' +
        '<div class="stat"><div class="label">Uncertainty band</div><div class="value neutral">±' +
          (fd.uncertainty_band_pct_of_price / 2).toFixed(2) + '%</div></div>' +
        '</div>' +
        '<div class="verdict">Statistical trend projection only (Facebook Prophet) — ' +
        'not aware of news or regime change. The shaded band (widening further out) is the honest part of this output, ' +
        'the point estimate is the least reliable part. Not a substitute for the confluence signal above.</div>' +
        '<details class="overview">' +
          '<summary>Transparent overview — exactly what fed this forecast</summary>' +
          '<div class="overview-body">' +
            '<div class="overview-row"><span class="k">Fitted on</span><span>' + fd.history_candles + ' x ' + r.timeframe + ' candles</span></div>' +
            '<div class="overview-row"><span class="k">History window</span><span>' + fd.history_start + ' → ' + fd.history_end + ' (' + fd.span_days + 'd)</span></div>' +
            '<div class="overview-row"><span class="k">Seasonality used</span><span>daily' + seasTag(fd.seasonality.daily) +
              ' weekly' + seasTag(fd.seasonality.weekly) + ' yearly' + seasTag(fd.seasonality.yearly) + '</span></div>' +
            '<div class="overview-row"><span class="k">Model</span><span>Prophet (additive trend + seasonality), no exogenous inputs</span></div>' +
            '<div class="overview-row"><span class="k">Not included</span><span>news, order flow, on-chain data, other coins/macro</span></div>' +
          '</div>' +
        '</details>';
    }

    async function pollForecast(symbol, horizon) {
      const r = await fetch('/api/forecast?symbol=' + encodeURIComponent(symbol) + '&horizon=' + horizon);
      const j = await r.json();
      if (j.running) { setTimeout(() => pollForecast(symbol, horizon), 2000); return; }
      if (!j.result) { document.getElementById('forecast-btn').disabled = false; return; }
      if (symbol === currentSymbol) { renderForecast(j.result); refreshEntry(); }
    }

    // --- Backtest ----------------------------------------------------------
    async function runBacktest() {
      const btn = document.getElementById('bt-btn');
      btn.disabled = true;
      document.getElementById('backtest-out').innerHTML =
        '<div class="placeholder" style="margin-top:1rem">Fetching history and walking forward, may take a bit...</div>';
      await fetch('/api/backtest?symbol=' + encodeURIComponent(currentSymbol), { method: 'POST' });
      pollBacktest(currentSymbol);
    }

    function renderBacktest(r) {
      const out = document.getElementById('backtest-out');
      document.getElementById('bt-btn').disabled = false;
      if (r.error) { out.innerHTML = '<div class="verdict"><b>Error:</b> ' + r.error + '</div>'; return; }

      const stats = [
        ['Trades', r.n_trades, 'neutral', v => v],
        ['Win rate', r.win_rate, r.win_rate >= 50 ? 'pos' : 'neg', v => v.toFixed(1) + '%'],
        ['Expectancy / trade', r.expectancy_pct, r.expectancy_pct > 0 ? 'pos' : 'neg', v => (v > 0 ? '+' : '') + v.toFixed(3) + '%'],
        ['Profit factor', r.profit_factor, r.profit_factor >= 1 ? 'pos' : 'neg', v => v.toFixed(2)],
        ['Total return', r.total_return_pct, r.total_return_pct > 0 ? 'pos' : 'neg', v => (v > 0 ? '+' : '') + v.toFixed(2) + '%'],
        ['Max drawdown', r.max_drawdown_pct, 'neg', v => v.toFixed(2) + '%'],
        ['Avg win', r.avg_win_pct, 'pos', v => '+' + v.toFixed(3) + '%'],
        ['Avg loss', r.avg_loss_pct, 'neg', v => v.toFixed(3) + '%'],
        ['Directional hit rate', r.directional_hit_rate, 'neutral', v => v.toFixed(1) + '% (' + r.directional_signals + ' signals)'],
      ];

      let html = '<div class="stat-grid">';
      for (const [label, val, cls, fmt] of stats) {
        html += '<div class="stat"><div class="label">' + label + '</div><div class="value ' + cls + '">' + fmt(val) + '</div></div>';
      }
      html += '</div>';

      const profitable = r.expectancy_pct > 0;
      html += '<div class="verdict' + (profitable ? ' good' : '') + '">' +
        (profitable
          ? 'Positive expectancy net of fees/slippage — logic shows a small real edge on this run.'
          : 'Expectancy is negative or flat net of costs — this configuration is not showing a tradeable edge here. Don\\'t run it live as-is; try different symbol/timeframe/MIN_CONFIDENCE in config.py.') +
        '</div>';

      out.innerHTML = html;
    }

    async function pollBacktest(symbol) {
      const r = await fetch('/api/backtest?symbol=' + encodeURIComponent(symbol));
      const j = await r.json();
      if (j.running) { setTimeout(() => pollBacktest(symbol), 2000); return; }
      if (!j.result) { document.getElementById('bt-btn').disabled = false; return; }
      if (symbol === currentSymbol) { renderBacktest(j.result); refreshEntry(); }
    }

    loadHistory();
    refreshSignal();
    refreshEntry();
    setInterval(refreshSignal, 15000);
    setInterval(refreshEntry, 15000);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    import json
    return render_template_string(
        _PAGE,
        exchange=config.EXCHANGE_ID,
        coins=config.COINS,
        coins_json=json.dumps(config.COINS),
        horizons=list(config.FORECAST_HORIZONS.keys()),
        backtest_candles=config.BACKTEST_CANDLES,
        chart_timeframes=config.CHART_TIMEFRAMES,
        default_timeframe=config.CHART_TIMEFRAME,
    )


@app.route("/api/history")
def api_history():
    symbol = request.args.get("symbol", config.SYMBOL)
    timeframe = request.args.get("timeframe", config.CHART_TIMEFRAME)
    limit = int(request.args.get("limit", config.CHART_HISTORY_LIMIT))
    try:
        df = fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit)
        candles = [
            {
                "time": int(ts.timestamp()),
                "open": float(row.open),
                "high": float(row.high),
                "low": float(row.low),
                "close": float(row.close),
            }
            for ts, row in df.iterrows()
        ]
        volume = [
            {
                "time": int(ts.timestamp()),
                "value": float(row.volume),
                "color": "rgba(61,220,132,0.5)" if row.close >= row.open else "rgba(255,92,92,0.5)",
            }
            for ts, row in df.iterrows()
        ]
        return jsonify({"candles": candles, "volume": volume})
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


@app.route("/api/entry")
def api_entry():
    symbol = request.args.get("symbol", config.SYMBOL)
    return jsonify(_compute_entry_verdict(symbol))


@app.route("/api/indicators")
def api_indicators():
    symbol = request.args.get("symbol", config.SYMBOL)
    timeframe = request.args.get("timeframe", config.CHART_TIMEFRAME)
    limit = int(request.args.get("limit", config.CHART_HISTORY_LIMIT))
    try:
        df = fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit)
        enriched = add_all_indicators(df)
        rsi = [
            {"time": int(ts.timestamp()), "value": float(v)}
            for ts, v in enriched["rsi"].dropna().items()
        ]
        macd_hist = [
            {
                "time": int(ts.timestamp()),
                "value": float(v),
                "color": "rgba(61,220,132,0.7)" if v >= 0 else "rgba(255,92,92,0.7)",
            }
            for ts, v in enriched["macd_hist"].dropna().items()
        ]
        return jsonify({"rsi": rsi, "macd_hist": macd_hist})
    except Exception as exc:
        traceback.print_exc()
        return jsonify({"error": str(exc)}), 500


@app.route("/api/signal")
def api_signal():
    symbol = request.args.get("symbol", config.SYMBOL)
    with _lock:
        return jsonify(_signals.get(symbol, {"signal": None, "updated_at": None, "error": None}))


@app.route("/api/backtest", methods=["GET", "POST"])
def api_backtest():
    symbol = request.args.get("symbol", config.SYMBOL)
    if request.method == "POST":
        _run_backtest_async(symbol)
    with _lock:
        return jsonify(_backtest_cache.get(symbol, {"running": False, "result": None}))


@app.route("/api/forecast", methods=["GET", "POST"])
def api_forecast():
    symbol = request.args.get("symbol", config.SYMBOL)
    horizon = request.args.get("horizon", "month")
    key = f"{symbol}|{horizon}"
    if request.method == "POST":
        _run_forecast_async(symbol, horizon)
    with _lock:
        return jsonify(_forecast_cache.get(key, {"running": False, "result": None}))


if __name__ == "__main__":
    threading.Thread(target=_signal_poll_loop, daemon=True).start()
    threading.Thread(target=_realtime_price_loop, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=3100, debug=False, allow_unsafe_werkzeug=True)
