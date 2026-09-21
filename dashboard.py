"""
Web dashboard for the analysis bot.

Runs a small Flask server (default port 3100) that shows the latest
signal, refreshed by a background poll loop (same cadence/logic as
bot.py — closed candles only, no lookahead), plus an on-demand backtest.
Read-only: still never places a trade.

Run: python dashboard.py
"""

from __future__ import annotations
import threading
import time
import traceback
from dataclasses import asdict

from flask import Flask, jsonify, render_template_string

import config
from data_fetcher import get_exchange, fetch_ohlcv, fetch_ohlcv_history
from signal_engine import generate_signal
from backtester import run_backtest

app = Flask(__name__)

_state = {
    "signal": None,       # dict form of latest Signal
    "updated_at": None,
    "error": None,
}
_backtest_cache = {"result": None, "running": False}
_lock = threading.Lock()


def _signal_to_dict(signal) -> dict:
    d = asdict(signal)
    d["timestamp"] = str(signal.timestamp)
    return d


def _poll_loop():
    exchange = get_exchange()
    last_seen_ts = None
    while True:
        try:
            df = fetch_ohlcv(limit=max(200, config.EMA_TREND + config.SR_LOOKBACK + 10), exchange=exchange)
            closed = df.iloc[:-1]
            latest_ts = closed.index[-1]
            if latest_ts != last_seen_ts:
                last_seen_ts = latest_ts
                signal = generate_signal(closed)
                with _lock:
                    _state["signal"] = _signal_to_dict(signal)
                    _state["updated_at"] = time.time()
                    _state["error"] = None
        except Exception as exc:
            traceback.print_exc()
            with _lock:
                _state["error"] = str(exc)
        time.sleep(config.POLL_SECONDS)


def _run_backtest_async():
    def work():
        try:
            df = fetch_ohlcv_history()
            result = run_backtest(df)
            with _lock:
                _backtest_cache["result"] = {
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
                }
        except Exception as exc:
            traceback.print_exc()
            with _lock:
                _backtest_cache["result"] = {"error": str(exc)}
        finally:
            with _lock:
                _backtest_cache["running"] = False

    with _lock:
        if _backtest_cache["running"]:
            return
        _backtest_cache["running"] = True
    threading.Thread(target=work, daemon=True).start()


_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Crypto Analysis Bot</title>
  <style>
    :root { color-scheme: dark; }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background:#0b0e14; color:#e6e6e6; padding:2rem; max-width:900px; margin:0 auto;
    }
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
    button {
      background:#238636; color:white; border:none; padding:0.6rem 1.1rem;
      border-radius:8px; cursor:pointer; font-size:0.9rem; font-weight:500;
    }
    button:disabled { background:#2d3340; cursor:default; }
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
  </style>
</head>
<body>
  <h1>{{ symbol }} / {{ timeframe }} &middot; {{ exchange }}</h1>

  <div class="card" id="signal-card"><span class="placeholder">Loading signal...</span></div>

  <div class="card">
    <div class="section-title">Backtest</div>
    <button id="bt-btn" onclick="runBacktest()">Run backtest ({{ backtest_candles }} candles)</button>
    <div id="backtest-out"></div>
  </div>

  <script>
    function fmtPct(n, digits) {
      digits = digits === undefined ? 2 : digits;
      const cls = n > 0 ? 'pos' : (n < 0 ? 'neg' : 'neutral');
      const sign = n > 0 ? '+' : '';
      return '<span class="' + cls + '">' + sign + n.toFixed(digits) + '%</span>';
    }

    async function refreshSignal() {
      const r = await fetch('/api/signal');
      const j = await r.json();
      const el = document.getElementById('signal-card');
      if (j.error) {
        el.innerHTML = '<b class="sell">Error:</b> ' + j.error;
        return;
      }
      if (!j.signal) { el.innerHTML = '<span class="placeholder">No signal yet — waiting on first closed candle.</span>'; return; }
      const s = j.signal;
      const cls = s.direction === 'BUY' ? 'buy' : (s.direction === 'SELL' ? 'sell' : 'hold');
      el.innerHTML = '<h2 class="' + cls + '">' + s.direction + ' <span style="font-size:0.9rem;color:#7d8797;font-weight:400">(confidence ' + s.confidence.toFixed(0) + '/100)</span></h2>' +
        '<div class="meta">price ' + s.price.toFixed(4) + ' &middot; ' + s.timestamp + '</div>' +
        '<ul class="reasons">' + s.reasons.map(r => '<li>' + r + '</li>').join('') + '</ul>' +
        '<div class="updated">updated ' + new Date(j.updated_at * 1000).toLocaleTimeString() + '</div>';
    }

    async function runBacktest() {
      const btn = document.getElementById('bt-btn');
      btn.disabled = true;
      document.getElementById('backtest-out').innerHTML =
        '<div class="placeholder" style="margin-top:1rem">Fetching history and walking forward, may take a bit...</div>';
      await fetch('/api/backtest', { method: 'POST' });
      poll();
    }

    function renderBacktest(r) {
      const out = document.getElementById('backtest-out');
      const btn = document.getElementById('bt-btn');
      btn.disabled = false;
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

    async function poll() {
      const r = await fetch('/api/backtest');
      const j = await r.json();
      if (j.running) { setTimeout(poll, 2000); return; }
      if (!j.result) { document.getElementById('bt-btn').disabled = false; return; }
      renderBacktest(j.result);
    }

    refreshSignal();
    setInterval(refreshSignal, 10000);
    poll();
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        _PAGE,
        symbol=config.SYMBOL,
        timeframe=config.TIMEFRAME,
        exchange=config.EXCHANGE_ID,
        backtest_candles=config.BACKTEST_CANDLES,
    )


@app.route("/api/signal")
def api_signal():
    with _lock:
        return jsonify({
            "signal": _state["signal"],
            "updated_at": _state["updated_at"],
            "error": _state["error"],
        })


@app.route("/api/backtest", methods=["GET", "POST"])
def api_backtest():
    from flask import request
    if request.method == "POST":
        _run_backtest_async()
    with _lock:
        return jsonify({"running": _backtest_cache["running"], "result": _backtest_cache["result"]})


if __name__ == "__main__":
    threading.Thread(target=_poll_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=3100, debug=False)
