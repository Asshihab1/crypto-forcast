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
                    socketio.emit("price", {
                        "symbol": symbol,
                        "price": float(price),
                        "ts": time.time(),
                        "change_pct": ticker.get("percentage"),
                        "high": ticker.get("high"),
                        "low": ticker.get("low"),
                        "quote_volume": ticker.get("quoteVolume"),
                    })
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
        for h in ("15m", "30m", "1h", "4h", "day", "month", "year"):
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


_best_time_cache: dict[str, dict] = {}   # symbol -> {"running": bool, "result": ...}


def _run_best_time_async(symbol: str):
    """
    Scan every configured forecast horizon for `symbol` and rank them by
    signal-to-noise: |projected move| divided by the forecast's own
    uncertainty band. This is still just Prophet's trend extrapolation
    repeated at different candle sizes — a high ratio means "this horizon's
    trend is large relative to how wide its own uncertainty is," not
    "this will happen." Ties/no-signal horizons rank low on purpose.
    """
    def work():
        try:
            ranked = []
            for horizon in config.FORECAST_HORIZONS:
                try:
                    fc = run_forecast(symbol, horizon)
                except Exception as exc:
                    ranked.append({"horizon": horizon, "error": str(exc)})
                    continue
                band = fc["fit_details"]["uncertainty_band_pct_of_price"] / 2.0
                score = abs(fc["projected_change_pct"]) / max(band, 0.01)
                ranked.append({
                    "horizon": horizon,
                    "timeframe": fc["timeframe"],
                    "projected_change_pct": fc["projected_change_pct"],
                    "uncertainty_band_pct": band,
                    "score": score,
                })
                with _lock:
                    _forecast_cache[f"{symbol}|{horizon}"] = {"running": False, "result": fc}

            valid = [r for r in ranked if "error" not in r]
            valid.sort(key=lambda r: r["score"], reverse=True)
            best = valid[0] if valid else None

            with _lock:
                _best_time_cache[symbol] = {
                    "running": False,
                    "result": {"ranked": valid, "best": best, "errors": [r for r in ranked if "error" in r]},
                }
        except Exception as exc:
            traceback.print_exc()
            with _lock:
                _best_time_cache[symbol] = {"running": False, "result": {"error": str(exc)}}

    with _lock:
        if _best_time_cache.get(symbol, {}).get("running"):
            return
        _best_time_cache[symbol] = {"running": True, "result": _best_time_cache.get(symbol, {}).get("result")}
    threading.Thread(target=work, daemon=True).start()


_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Crypto Analysis Bot</title>
  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
  <style>
    :root {
      color-scheme: light;
      /* SAP Fiori (Quartz Light) design tokens */
      --bg:#f2f2f2; --card:#ffffff; --border:#d9d9d9; --border-soft:#e5e5e5;
      --text:#32363a; --text-muted:#6a6d70; --text-faint:#89919a;
      --accent:#0a6ed1; --accent-soft:#e6f2fd; --accent-dark:#0854a0;
      --pos:#107e3e; --pos-soft:#e5f5ea; --neg:#bb0000; --neg-soft:#ffebeb;
      --amber:#e9730c; --amber-soft:#fef3e6;
      --shell-bg:#354a5f; --shell-text:#ffffff;
      --sidebar-w:180px; --radius:4px; --radius-lg:8px;
    }
    :root[data-theme="dark"] {
      color-scheme: dark;
      /* SAP Fiori (Quartz Dark) design tokens */
      --bg:#12181f; --card:#1a232c; --border:#3b4a59; --border-soft:#2b3947;
      --text:#ffffff; --text-muted:#a6b7c4; --text-faint:#7b8fa0;
      --accent:#4a9eff; --accent-soft:#1c2e40; --accent-dark:#7cbaff;
      --pos:#4ecb73; --pos-soft:#16281c; --neg:#ff5c5c; --neg-soft:#301616;
      --amber:#f0a35d; --amber-soft:#2e2213;
      --shell-bg:#1a232c; --shell-text:#ffffff;
    }
    * { box-sizing: border-box; }
    html, body {
      font-family: "72", "72full", Arial, Helvetica, sans-serif;
      background:var(--bg); color:var(--text); margin:0; width:100%; height:100%; overflow:hidden;
      font-size:14px;
    }
    .shellbar {
      height:44px; flex-shrink:0; background:var(--shell-bg); color:var(--shell-text);
      display:flex; align-items:center; justify-content:space-between; padding:0 1rem;
    }
    .shellbar-left { display:flex; align-items:center; gap:0.6rem; font-size:0.95rem; font-weight:600; }
    .shellbar-icon { width:26px; height:26px; border-radius:50%; background:rgba(255,255,255,0.15); display:flex; align-items:center; justify-content:center; font-size:0.8rem; }
    .shellbar-right { display:flex; align-items:center; gap:0.9rem; }
    .shellbar .live-pill { background:rgba(255,255,255,0.1); border:1px solid rgba(255,255,255,0.2); color:#fff; }
    .theme-switch-btn {
      background:rgba(255,255,255,0.12); border:1px solid rgba(255,255,255,0.25); color:#fff;
      border-radius:var(--radius); padding:0.3rem 0.6rem; font-size:0.78rem; cursor:pointer;
    }
    .app-shell { display:flex; height:calc(100vh - 44px); }
    .sidebar {
      width:var(--sidebar-w); flex-shrink:0; background:var(--card); border-right:1px solid var(--border);
      padding:1.25rem 1rem; height:100vh; display:flex; flex-direction:column;
    }
    .side-nav { display:flex; flex-direction:column; gap:0.15rem; }
    .nav-item {
      display:block; padding:0.5rem 0.75rem; border-radius:var(--radius); color:var(--text-muted);
      text-decoration:none; font-size:0.88rem; font-weight:500;
    }
    .nav-item:hover { background:var(--accent-soft); color:var(--accent-dark); }
    .nav-item.active { background:var(--accent); color:#fff; }

    .main { flex:1; min-width:0; height:100vh; display:flex; flex-direction:column; padding:1rem 1.5rem; overflow:hidden; }
    .topbar { flex-shrink:0; display:flex; align-items:center; justify-content:space-between; margin-bottom:0.75rem; }
    .crumbs { font-size:0.95rem; color:var(--text-muted); }
    .crumbs b { color:var(--text); }
    .live-pill { display:flex; align-items:center; justify-content:center; width:28px; height:28px; background:var(--card); border:1px solid var(--border); border-radius:50%; }
    .live-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--pos); }
    .live-dot.off { background:var(--text-faint); }

    .stats-row { flex-shrink:0; display:grid; grid-template-columns: 1.3fr repeat(3, 1fr); gap:0.75rem; margin-bottom:0.75rem; }
    .balance-card {
      background:linear-gradient(135deg, var(--accent), var(--accent-dark)); color:#fff;
      border-radius:var(--radius-lg); padding:0.8rem 1.1rem;
    }
    .balance-card .label { font-size:0.76rem; opacity:0.85; margin-bottom:0.25rem; }
    .balance-card input {
      width:100%; background:rgba(255,255,255,0.15); border:1px solid rgba(255,255,255,0.3); color:#fff;
      padding:0.4rem 0.6rem; border-radius:var(--radius); font-size:0.92rem; margin-bottom:0.4rem;
    }
    .balance-card input::placeholder { color:rgba(255,255,255,0.7); }
    .balance-summary { font-size:0.78rem; opacity:0.95; display:flex; flex-direction:column; gap:0.15rem; }
    .mini-coin { background:var(--card); border:1px solid var(--border); border-radius:var(--radius-lg); padding:0.7rem 0.9rem; cursor:pointer; }
    .mini-coin:hover { border-color:var(--accent); }
    .mini-coin .row { display:flex; align-items:center; justify-content:space-between; margin-bottom:0.35rem; }
    .mini-coin .name { font-weight:600; font-size:0.82rem; }
    .mini-coin .pair { font-size:0.7rem; color:var(--text-faint); }
    .mini-coin .price { font-size:1rem; font-weight:700; }
    .coin-badge { width:24px; height:24px; border-radius:50%; background:var(--accent-soft); color:var(--accent-dark); display:flex; align-items:center; justify-content:center; font-size:0.72rem; font-weight:700; }

    .content-grid { flex:1; min-height:0; display:grid; grid-template-columns: minmax(0, 2.1fr) minmax(280px, 1fr); gap:1rem; }

    .card { background:var(--card); border:1px solid var(--border); border-radius:var(--radius-lg); padding:1rem 1.2rem; }
    .section-title { font-size:0.92rem; font-weight:600; color:var(--text); margin-bottom:0.75rem; }
    .meta { color:var(--text-muted); font-size:0.85rem; margin-bottom:0.75rem; }
    .placeholder { color:var(--text-faint); font-size:0.9rem; }
    @keyframes skeleton-shimmer { 0% { background-position:-200px 0; } 100% { background-position:200px 0; } }
    .skel {
      display:block; border-radius:var(--radius); background:var(--border-soft);
      background-image:linear-gradient(90deg, var(--border-soft) 0px, var(--border) 40px, var(--border-soft) 80px);
      background-size:200px 100%; background-repeat:no-repeat; animation:skeleton-shimmer 1.2s infinite linear;
    }
    .skel-line { height:0.85rem; margin-bottom:0.5rem; width:100%; }
    .skel-line.short { width:40%; }
    .skel-line.medium { width:65%; }
    .skel-block { height:2.5rem; }
    .skel-row { display:flex; align-items:center; gap:0.6rem; padding:0.5rem 0; }
    .skel-circle { width:24px; height:24px; border-radius:50%; flex-shrink:0; }
    .updated { color:var(--text-faint); font-size:0.78rem; margin-top:0.9rem; }

    .chart-card { display:flex; flex-direction:column; min-height:0; }
    .chart-head { flex-shrink:0; display:flex; flex-wrap:wrap; align-items:flex-start; justify-content:space-between; gap:1rem; margin-bottom:0.5rem; }
    .chart-title-row { display:flex; align-items:center; gap:0.6rem; margin-bottom:0.2rem; }
    .chart-price-big { font-size:1.4rem; font-weight:700; }
    .chart-substats { display:flex; gap:1.1rem; font-size:0.78rem; color:var(--text-muted); flex-wrap:wrap; }
    .chart-substats b { color:var(--text); }
    #rsi-chart { flex:1; min-height:0; }
    #macd-chart { flex:1; min-height:0; }

    .side-panel { display:flex; flex-direction:column; min-height:0; padding-bottom:0.5rem; }
    .side-tabs { flex-shrink:0; display:flex; flex-wrap:wrap; gap:0; margin-bottom:0.75rem; border-bottom:1px solid var(--border); }
    .side-tab {
      background:transparent; border:none; border-bottom:2px solid transparent; color:var(--text-muted);
      padding:0.5rem 0.65rem; border-radius:0; font-size:0.76rem; font-weight:400; margin-bottom:-1px;
    }
    .side-tab:hover { color:var(--text); background:var(--bg); }
    .side-tab.active { background:transparent; border-bottom-color:var(--accent); color:var(--accent); font-weight:600; }
    .tab-content { flex:1; min-height:0; overflow-y:auto; }
    .tab-panel { display:none; }
    .tab-panel.active { display:block; }
    #chart { width:100%; height:340px; }

    select, button, input {
      background:var(--card); color:var(--text); border:1px solid var(--border); padding:0.55rem 0.85rem;
      border-radius:var(--radius); font-size:0.88rem; font-weight:400; cursor:pointer; font-family:inherit;
    }
    button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
    button:disabled { background:var(--border-soft); color:var(--text-faint); cursor:default; }
    .toolbar { display:flex; gap:0.75rem; align-items:center; flex-wrap:wrap; margin-bottom:1rem; }
    .tf-tabs { display:inline-flex; gap:0.25rem; background:var(--bg); border:1px solid var(--border); border-radius:var(--radius); padding:0.15rem; }
    .tf-tab { background:transparent; border:none; padding:0.35rem 0.7rem; border-radius:2px; font-size:0.8rem; font-weight:400; color:var(--text-muted); }
    .tf-tab:hover { color:var(--text); background:var(--border-soft); }
    .tf-tab.active { background:var(--accent); color:#fff; font-weight:600; }

    .buy, .pos { color:var(--pos); } .sell, .neg { color:var(--neg); } .hold, .neutral { color:var(--text); }
    ul.reasons { margin:0; padding-left:1.1rem; }
    ul.reasons li { margin:0.3rem 0; font-size:0.92rem; color:var(--text-muted); }

    .stat-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:0.75rem; margin-top:1rem; }
    .stat { background:var(--bg); border:1px solid var(--border-soft); border-radius:var(--radius); padding:0.85rem 1rem; }
    .stat .label { font-size:0.78rem; color:var(--text-muted); margin-bottom:0.35rem; }
    .stat .value { font-size:1.2rem; font-weight:600; }

    .verdict { margin-top:1rem; padding:0.6rem 0.9rem; border-radius:0 var(--radius) var(--radius) 0; border-left:3px solid var(--amber); font-size:0.85rem; background:var(--amber-soft); color:var(--text); }
    .verdict.good { background:var(--pos-soft); border-left-color:var(--pos); }

    .overview { margin-top:1rem; border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }
    .overview summary { cursor:pointer; padding:0.7rem 1rem; font-size:0.82rem; color:var(--text-muted); background:var(--bg); list-style:none; user-select:none; }
    .overview summary::-webkit-details-marker { display:none; }
    .overview summary::before { content:'\\25B8  '; }
    .overview[open] summary::before { content:'\\25BE  '; }
    .overview-body { padding:0.9rem 1rem; font-size:0.83rem; color:var(--text-muted); }
    .overview-row { display:flex; justify-content:space-between; padding:0.3rem 0; border-bottom:1px solid var(--border-soft); }
    .overview-row:last-child { border-bottom:none; }
    .overview-row .k { color:var(--text-faint); }
    .seasonality-tag { display:inline-block; padding:0.15rem 0.5rem; border-radius:4px; font-size:0.72rem; margin-left:0.3rem; }
    .seasonality-tag.on { background:var(--pos-soft); color:var(--pos); }
    .seasonality-tag.off { background:var(--border-soft); color:var(--text-faint); }

    .entry-card { border-width:2px; }
    .entry-card.entry-long { border-color:#bfe9cf; background:var(--pos-soft); }
    .entry-card.entry-short { border-color:#f6c6c6; background:var(--neg-soft); }
    .entry-card.entry-avoid { border-color:#f6c6c6; background:var(--neg-soft); }
    .entry-card.entry-wait { border-color:var(--border); }
    .entry-badge { display:inline-block; padding:0.3rem 0.8rem; border-radius:6px; font-weight:700; font-size:1rem; }
    .entry-badge.entry-long { background:var(--pos); color:#fff; }
    .entry-badge.entry-short { background:var(--neg); color:#fff; }
    .entry-badge.entry-avoid { background:var(--neg); color:#fff; }
    .entry-badge.entry-wait { background:var(--text-faint); color:#fff; }
    .entry-segments { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:0.75rem; margin:0.9rem 0; }
    .segment { background:var(--card); border:1px solid var(--border-soft); border-radius:var(--radius); padding:0.85rem 1rem; }
    .segment .label { font-size:0.78rem; color:var(--text-muted); margin-bottom:0.35rem; }
    .segment .value { font-size:1.15rem; font-weight:600; }
    .dir-up { color:var(--pos); } .dir-down { color:var(--neg); } .dir-flat { color:var(--text-muted); }

    table.markets-table { width:100%; border-collapse:collapse; font-size:0.85rem; }
    table.markets-table th { text-align:left; color:var(--text-faint); font-weight:500; font-size:0.75rem; padding:0.4rem 0.6rem; border-bottom:1px solid var(--border); }
    table.markets-table td { padding:0.65rem 0.6rem; border-bottom:1px solid var(--border-soft); }
    table.markets-table tr.mkt-row { cursor:pointer; }
    table.markets-table tr.mkt-row:hover { background:var(--bg); }
    table.markets-table tr.mkt-row.active { background:var(--accent-soft); }
  </style>
</head>
<body>
  <div class="shellbar">
    <div class="shellbar-left">
      <span class="shellbar-icon">₿</span>
      {{ exchange|capitalize }} Analysis Bot
    </div>
    <div class="shellbar-right">
      <div class="live-pill" id="live-pill" title="Connecting"><span class="live-dot off" id="live-dot"></span></div>
      <button type="button" class="theme-switch-btn" id="theme-toggle-btn">Dark mode</button>
    </div>
  </div>

  <div class="app-shell">
    <aside class="sidebar">
      <nav class="side-nav">
        <a href="#dashboard" class="nav-item active">Dashboard</a>
      </nav>
    </aside>

    <main class="main">
      <header class="topbar">
        <div class="crumbs">Dashboard <span>/</span> <b id="crumb-coin">{{ exchange }}</b></div>
      </header>

      <section class="stats-row">
        <div class="balance-card">
          <div class="label">Investment amount</div>
          <input id="amount-input" type="number" min="0" step="any" placeholder="$0.00">
          <div class="balance-summary" id="position-summary">Enter an amount to size the current entry.</div>
        </div>
        <div class="mini-coin" id="mini-coin-0"></div>
        <div class="mini-coin" id="mini-coin-1"></div>
        <div class="mini-coin" id="mini-coin-2"></div>
      </section>

      <section class="content-grid" id="dashboard">
        <div class="card chart-card">
          <div class="chart-head">
            <div>
              <div class="chart-title-row">
                <select id="coin-select">
                  {% for symbol, label in coins.items() %}
                  <option value="{{ symbol }}">{{ label }} ({{ symbol }})</option>
                  {% endfor %}
                </select>
              </div>
              <div class="chart-price-big" id="chart-price-big">—</div>
              <div class="chart-substats">
                <span>24h <b id="stat-change">—</b></span>
                <span>High 24h <b id="stat-high">—</b></span>
                <span>Low 24h <b id="stat-low">—</b></span>
                <span>Volume 24h <b id="stat-vol">—</b></span>
              </div>
            </div>
            <div class="tf-tabs" id="tf-tabs">
              {% for opt in time_options %}
              <button type="button" class="tf-tab{% if opt.key == default_time_key %} active{% endif %}" data-key="{{ opt.key }}">{{ opt.label }}</button>
              {% endfor %}
            </div>
          </div>
          <div style="position:relative; flex:3; min-height:0; display:flex;">
            <div id="chart" style="flex:1"></div>
            <div id="chart-skel" class="skel" style="position:absolute; inset:0; border-radius:0;"></div>
          </div>
          <div id="rsi-chart"></div>
          <div id="macd-chart"></div>
        </div>

        <div class="card side-panel">
          <div class="side-tabs" id="side-tabs">
            <button type="button" class="side-tab active" data-tab="entry">Entry</button>
            <button type="button" class="side-tab" data-tab="signal">Signal</button>
            <button type="button" class="side-tab" data-tab="position">Position</button>
            <button type="button" class="side-tab" data-tab="forecast">Forecast</button>
            <button type="button" class="side-tab" data-tab="besttime">Best time</button>
            <button type="button" class="side-tab" data-tab="backtest">Backtest</button>
            <button type="button" class="side-tab" data-tab="markets">Markets</button>
          </div>
          <div class="tab-content">
            <div class="tab-panel active" data-panel="entry">
              <div id="entry-card">
                <span class="skel skel-line short" style="height:1.4rem;margin-bottom:0.75rem"></span>
                <span class="skel skel-line"></span>
                <span class="skel skel-line medium"></span>
              </div>
            </div>
            <div class="tab-panel" data-panel="signal">
              <div id="signal-card">
                <span class="skel skel-line short" style="height:1.4rem;margin-bottom:0.75rem"></span>
                <span class="skel skel-line"></span>
                <span class="skel skel-line medium"></span>
              </div>
            </div>
            <div class="tab-panel" data-panel="position">
              <div class="section-title">Position calculator</div>
              <div id="position-out"><span class="placeholder">Enter an amount above to size the current entry.</span></div>
            </div>
            <div class="tab-panel" data-panel="forecast">
              <div class="section-title">Prophet forecast</div>
              <div class="toolbar">
                <span class="meta">Horizon: <b id="forecast-horizon-label">—</b></span>
                <button class="primary" id="forecast-btn" onclick="runForecast()">Run forecast</button>
              </div>
              <div id="forecast-out"></div>
            </div>
            <div class="tab-panel" data-panel="besttime">
              <div class="section-title">Best time to invest</div>
              <div class="toolbar">
                <button class="primary" id="best-time-btn" onclick="runBestTime()">Scan all horizons</button>
              </div>
              <div id="best-time-out"><span class="placeholder">Scans 15m/30m/1h/4h/1D/1M/1Y and ranks each by projected move vs its own uncertainty. Runs a Prophet fit per horizon — can take a few minutes.</span></div>
            </div>
            <div class="tab-panel" data-panel="backtest">
              <div class="section-title">Backtest</div>
              <button id="bt-btn" onclick="runBacktest()">Run backtest ({{ backtest_candles }} candles)</button>
              <div id="backtest-out"></div>
              <div id="backtest-donut-wrap" style="margin-top:0.75rem"></div>
            </div>
            <div class="tab-panel" data-panel="markets">
              <div class="section-title">Markets</div>
              <table class="markets-table">
                <thead><tr><th>Asset</th><th>Price</th><th>24h change</th></tr></thead>
                <tbody id="markets-body"></tbody>
              </table>
            </div>
          </div>
        </div>
      </section>
    </main>
  </div>

  <script>
    const COINS = {{ coins_json|safe }};
    const TIME_OPTIONS = {{ time_options_json|safe }};
    let currentSymbol = document.getElementById('coin-select').value;
    let currentTimeOpt = TIME_OPTIONS.find(o => o.key === '{{ default_time_key }}') || TIME_OPTIONS[0];
    let currentTimeframe = currentTimeOpt.timeframe;
    const chartEl = document.getElementById('chart');
    const chart = LightweightCharts.createChart(chartEl, {
      layout: { background: { color: '#ffffff' }, textColor: '#6b7280' },
      grid: { vertLines: { color: '#eef0f5' }, horzLines: { color: '#eef0f5' } },
      rightPriceScale: { borderColor: '#e8eaf1' },
      timeScale: { borderColor: '#e8eaf1', timeVisible: true },
      autoSize: true,
    });

    const rsiChart = LightweightCharts.createChart(document.getElementById('rsi-chart'), {
      layout: { background: { color: '#ffffff' }, textColor: '#6b7280' },
      grid: { vertLines: { color: '#eef0f5' }, horzLines: { color: '#eef0f5' } },
      rightPriceScale: { borderColor: '#e8eaf1' },
      timeScale: { borderColor: '#e8eaf1', timeVisible: true, visible: false },
      autoSize: true,
    });
    const rsiSeries = rsiChart.addLineSeries({ color: '#4f5bff', lineWidth: 1 });
    rsiSeries.createPriceLine({ price: 70, color: '#ef4444', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '70' });
    rsiSeries.createPriceLine({ price: 30, color: '#16a34a', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '30' });

    const macdChart = LightweightCharts.createChart(document.getElementById('macd-chart'), {
      layout: { background: { color: '#ffffff' }, textColor: '#6b7280' },
      grid: { vertLines: { color: '#eef0f5' }, horzLines: { color: '#eef0f5' } },
      rightPriceScale: { borderColor: '#e8eaf1' },
      timeScale: { borderColor: '#e8eaf1', timeVisible: true, visible: false },
      autoSize: true,
    });
    const macdSeries = macdChart.addHistogramSeries({ priceFormat: { type: 'price', precision: 2 } });

    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range) return;
      rsiChart.timeScale().setVisibleLogicalRange(range);
      macdChart.timeScale().setVisibleLogicalRange(range);
    });
    chart.priceScale('right').applyOptions({ scaleMargins: { top: 0.05, bottom: 0.28 } });
    const candleSeries = chart.addCandlestickSeries({
      upColor: '#16a34a', downColor: '#ef4444', borderVisible: false,
      wickUpColor: '#16a34a', wickDownColor: '#ef4444',
    });
    const volumeSeries = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });
    chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
    const liveSeries = chart.addLineSeries({ color: '#d97706', lineWidth: 1 });
    let forecastSeries = null, forecastUpper = null, forecastLower = null;

    // --- Theme toggle ----------------------------------------------------------
    function applyChartTheme(isDark) {
      const bg = isDark ? '#12161f' : '#ffffff';
      const text = isDark ? '#9aa4b2' : '#6b7280';
      const grid = isDark ? '#1c2130' : '#eef0f5';
      const border = isDark ? '#232838' : '#e8eaf1';
      [chart, rsiChart, macdChart].forEach((c) => {
        c.applyOptions({
          layout: { background: { color: bg }, textColor: text },
          grid: { vertLines: { color: grid }, horzLines: { color: grid } },
          rightPriceScale: { borderColor: border },
          timeScale: { borderColor: border },
        });
      });
    }

    function setTheme(isDark) {
      document.documentElement.dataset.theme = isDark ? 'dark' : 'light';
      document.getElementById('theme-toggle-btn').textContent = isDark ? 'Light mode' : 'Dark mode';
      applyChartTheme(isDark);
      try { localStorage.setItem('theme', isDark ? 'dark' : 'light'); } catch (e) {}
    }

    document.getElementById('theme-toggle-btn').addEventListener('click', () => {
      setTheme(document.documentElement.dataset.theme !== 'dark');
    });

    (function initTheme() {
      let saved = null;
      try { saved = localStorage.getItem('theme'); } catch (e) {}
      const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
      setTheme(saved ? saved === 'dark' : prefersDark);
    })();

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
      document.getElementById('crumb-coin').textContent = COINS[currentSymbol] || currentSymbol;
      const skel = document.getElementById('chart-skel');
      if (skel) skel.remove();
    }

    function selectSymbol(symbol) {
      if (symbol === currentSymbol) return;
      currentSymbol = symbol;
      document.getElementById('coin-select').value = symbol;
      loadHistory();
      refreshSignal();
      refreshEntry();
      document.getElementById('forecast-out').innerHTML = '';
      lastForecast = null;
      renderMarkets();
      renderMiniCoins();
      renderChartHeaderStats();
      runForecast();
      runBacktest();
    }

    document.getElementById('coin-select').addEventListener('change', (e) => selectSymbol(e.target.value));

    function setForecastHorizonLabel() {
      document.getElementById('forecast-horizon-label').textContent = currentTimeOpt.horizon;
    }

    document.querySelectorAll('.tf-tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.tf-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentTimeOpt = TIME_OPTIONS.find(o => o.key === btn.dataset.key);
        currentTimeframe = currentTimeOpt.timeframe;
        loadHistory();
        setForecastHorizonLabel();
        document.getElementById('forecast-out').innerHTML = '';
        lastForecast = null;
        runForecast();
      });
    });
    setForecastHorizonLabel();

    // --- Side panel tabs -----------------------------------------------------
    document.querySelectorAll('.side-tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.side-tab').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.querySelector('.tab-panel[data-panel="' + btn.dataset.tab + '"]').classList.add('active');
      });
    });

    // --- Realtime price via websocket ---------------------------------------
    const socket = io();
    socket.on('connect', () => {
      document.getElementById('live-dot').classList.remove('off');
      document.getElementById('live-pill').title = 'Live';
    });
    socket.on('disconnect', () => {
      document.getElementById('live-dot').classList.add('off');
      document.getElementById('live-pill').title = 'Offline';
    });

    const coinTickers = {};   // symbol -> latest {price, change_pct, high, low, quote_volume}
    let lastLivePrice = null;

    function fmtPrice(p) { return p.toLocaleString(undefined, { maximumFractionDigits: p < 10 ? 6 : 2 }); }
    function fmtChangeSpan(pct) {
      if (pct === null || pct === undefined) return '<span class="placeholder">—</span>';
      const cls = pct >= 0 ? 'pos' : 'neg';
      return '<span class="' + cls + '">' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%</span>';
    }

    function renderChartHeaderStats() {
      const t = coinTickers[currentSymbol];
      if (!t) return;
      document.getElementById('chart-price-big').textContent = '$' + fmtPrice(t.price);
      document.getElementById('stat-change').innerHTML = fmtChangeSpan(t.change_pct);
      document.getElementById('stat-high').textContent = t.high !== null && t.high !== undefined ? fmtPrice(t.high) : '—';
      document.getElementById('stat-low').textContent = t.low !== null && t.low !== undefined ? fmtPrice(t.low) : '—';
      document.getElementById('stat-vol').textContent = t.quote_volume ? '$' + Math.round(t.quote_volume).toLocaleString() : '—';
    }

    function renderMiniCoins() {
      const symbols = Object.keys(COINS).filter(s => s !== currentSymbol).slice(0, 3);
      symbols.forEach((sym, i) => {
        const el = document.getElementById('mini-coin-' + i);
        if (!el) return;
        const t = coinTickers[sym];
        el.onclick = () => selectSymbol(sym);
        el.innerHTML =
          '<div class="row"><span class="coin-badge">' + COINS[sym][0] + '</span>' +
            '<span class="pair">' + sym + '</span></div>' +
          '<div class="name">' + COINS[sym] + '</div>' +
          (t
            ? '<div class="price">$' + fmtPrice(t.price) + '</div>' +
              '<div class="meta" style="margin:0.2rem 0 0">' + fmtChangeSpan(t.change_pct) + '</div>'
            : '<span class="skel skel-line medium" style="height:1.1rem;margin:0 0 0.3rem"></span>' +
              '<span class="skel skel-line short" style="height:0.7rem;margin:0"></span>');
      });
    }

    function renderMarkets() {
      const tbody = document.getElementById('markets-body');
      tbody.innerHTML = Object.keys(COINS).map((sym) => {
        const t = coinTickers[sym];
        const active = sym === currentSymbol ? ' active' : '';
        return '<tr class="mkt-row' + active + '" onclick="selectSymbol(\\'' + sym + '\\')">' +
          '<td><span class="coin-badge" style="margin-right:0.5rem">' + COINS[sym][0] + '</span>' + COINS[sym] +
            ' <span class="meta" style="display:inline;margin:0">' + sym + '</span></td>' +
          '<td>' + (t ? '$' + fmtPrice(t.price) : '<span class="skel skel-line short" style="height:0.9rem;margin:0"></span>') + '</td>' +
          '<td>' + (t ? fmtChangeSpan(t.change_pct) : '<span class="skel skel-line short" style="height:0.9rem;margin:0"></span>') + '</td>' +
        '</tr>';
      }).join('');
    }
    renderMarkets();

    socket.on('price', (msg) => {
      coinTickers[msg.symbol] = msg;
      renderMiniCoins();
      renderMarkets();
      if (msg.symbol !== currentSymbol) return;
      lastLivePrice = msg.price;
      liveSeries.update({ time: Math.floor(msg.ts), value: msg.price });
      renderChartHeaderStats();
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
      const summary = document.getElementById('position-summary');
      const amount = parseFloat(document.getElementById('amount-input').value);
      if (!amount || amount <= 0) {
        out.innerHTML = '<span class="placeholder">Enter an amount to size the current entry.</span>';
        summary.textContent = 'Enter an amount to size the current entry.';
        return;
      }
      if (!lastEntry || lastEntry.entry_price === undefined) {
        out.innerHTML = '<span class="placeholder">Waiting for a price...</span>';
        summary.textContent = 'Waiting for a price...';
        return;
      }

      const qty = amount / lastEntry.entry_price;
      const hasEntry = lastEntry.verdict === 'ENTRY_LONG' || lastEntry.verdict === 'ENTRY_SHORT';

      if (lastLivePrice !== null) {
        const move = lastEntry.direction === 'SELL' ? (lastEntry.entry_price - lastLivePrice) : (lastLivePrice - lastEntry.entry_price);
        const pnlUsd = qty * move;
        summary.innerHTML = qty.toFixed(6) + ' ' + currentSymbol.split('/')[0] + ' &middot; unrealized ' +
          (pnlUsd >= 0 ? '+' : '') + '$' + pnlUsd.toFixed(2);
      } else {
        summary.textContent = qty.toFixed(6) + ' ' + currentSymbol.split('/')[0];
      }

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
      if (lastForecast && !lastForecast.error) renderForecast(lastForecast);
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
      const horizon = currentTimeOpt.horizon;
      btn.disabled = true;
      document.getElementById('forecast-out').innerHTML =
        '<div class="placeholder" style="margin-top:1rem">Fitting Prophet model, may take ~10-30s...</div>';
      await fetch('/api/forecast?symbol=' + encodeURIComponent(currentSymbol) + '&horizon=' + horizon, { method: 'POST' });
      pollForecast(currentSymbol, horizon);
    }

    let lastForecast = null;
    function renderForecast(r) {
      lastForecast = r;
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

      const amount = parseFloat(document.getElementById('amount-input').value);
      let amountTile = '';
      if (amount > 0) {
        const projectedValue = amount * (1 + r.projected_change_pct / 100);
        amountTile = '<div class="stat"><div class="label">$' + amount.toFixed(2) + ' becomes</div><div class="value ' + dir + '">' +
          '$' + projectedValue.toFixed(2) + '</div></div>';
      }

      out.innerHTML = '<div class="stat-grid">' +
        '<div class="stat"><div class="label">Last price</div><div class="value neutral">' + r.last_price.toFixed(2) + '</div></div>' +
        '<div class="stat"><div class="label">Horizon</div><div class="value neutral">' + r.periods + ' x ' + r.timeframe + '</div></div>' +
        '<div class="stat"><div class="label">Projected change</div><div class="value ' + dir + '">' +
          (r.projected_change_pct >= 0 ? '+' : '') + r.projected_change_pct.toFixed(2) + '%</div></div>' +
        '<div class="stat"><div class="label">Uncertainty band</div><div class="value neutral">±' +
          (fd.uncertainty_band_pct_of_price / 2).toFixed(2) + '%</div></div>' +
        amountTile +
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
      if (symbol === currentSymbol && horizon === currentTimeOpt.horizon) { renderForecast(j.result); refreshEntry(); }
    }

    // --- Best time to invest (scans every horizon) --------------------------
    async function runBestTime() {
      const btn = document.getElementById('best-time-btn');
      btn.disabled = true;
      document.getElementById('best-time-out').innerHTML =
        '<div class="placeholder">Fitting Prophet across every horizon (15m → 1Y), one at a time — this can take a few minutes...</div>';
      await fetch('/api/best_time?symbol=' + encodeURIComponent(currentSymbol), { method: 'POST' });
      pollBestTime(currentSymbol);
    }

    function renderBestTime(r) {
      document.getElementById('best-time-btn').disabled = false;
      const out = document.getElementById('best-time-out');
      if (r.error) { out.innerHTML = '<div class="verdict"><b>Error:</b> ' + r.error + '</div>'; return; }
      if (!r.best) { out.innerHTML = '<span class="placeholder">No horizon produced a usable forecast.</span>'; return; }

      const b = r.best;
      const bDir = b.projected_change_pct >= 0 ? 'pos' : 'neg';
      const bLabel = TIME_OPTIONS.find(o => o.horizon === b.horizon);

      let html = '<div class="verdict good">Best signal-to-noise: <b>' + (bLabel ? bLabel.label : b.horizon) + '</b> — ' +
        '<span class="' + bDir + '">' + (b.projected_change_pct >= 0 ? '+' : '') + b.projected_change_pct.toFixed(2) + '%</span> projected, ' +
        '±' + b.uncertainty_band_pct.toFixed(2) + '% band (score ' + b.score.toFixed(2) + ').</div>';

      html += '<div class="stat-grid" style="margin-top:0.75rem">';
      for (const row of r.ranked) {
        const opt = TIME_OPTIONS.find(o => o.horizon === row.horizon);
        const cls = row.projected_change_pct >= 0 ? 'pos' : 'neg';
        html += '<div class="stat" style="cursor:pointer" onclick="jumpToHorizon(\\'' + row.horizon + '\\')">' +
          '<div class="label">' + (opt ? opt.label : row.horizon) + '</div>' +
          '<div class="value ' + cls + '">' + (row.projected_change_pct >= 0 ? '+' : '') + row.projected_change_pct.toFixed(2) + '%</div>' +
          '<div class="meta" style="margin:0">score ' + row.score.toFixed(2) + '</div>' +
        '</div>';
      }
      html += '</div>';
      if (r.errors && r.errors.length) {
        html += '<div class="placeholder" style="margin-top:0.6rem">' + r.errors.length + ' horizon(s) failed (usually not enough history yet).</div>';
      }
      out.innerHTML = html;
    }

    function jumpToHorizon(horizon) {
      const opt = TIME_OPTIONS.find(o => o.horizon === horizon);
      if (!opt) return;
      const tab = document.querySelector('.tf-tab[data-key="' + opt.key + '"]');
      if (tab) tab.click();
      runForecast();
    }

    async function pollBestTime(symbol) {
      const r = await fetch('/api/best_time?symbol=' + encodeURIComponent(symbol));
      const j = await r.json();
      if (j.running) { setTimeout(() => pollBestTime(symbol), 3000); return; }
      if (!j.result) { document.getElementById('best-time-btn').disabled = false; return; }
      if (symbol === currentSymbol) renderBestTime(j.result);
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
      renderBacktestDonut(r);
    }

    let backtestChart = null;
    function renderBacktestDonut(r) {
      const wrap = document.getElementById('backtest-donut-wrap');
      if (!r.n_trades) { wrap.innerHTML = '<span class="placeholder">No trades in this backtest window.</span>'; return; }
      const wins = Math.round(r.n_trades * r.win_rate / 100);
      const losses = r.n_trades - wins;
      wrap.innerHTML = '<div style="position:relative; height:220px; max-width:320px; margin:0 auto"><canvas id="backtest-donut"></canvas></div>';
      if (backtestChart) backtestChart.destroy();
      backtestChart = new Chart(document.getElementById('backtest-donut'), {
        type: 'doughnut',
        data: {
          labels: ['Wins (' + wins + ')', 'Losses (' + losses + ')'],
          datasets: [{ data: [wins, losses], backgroundColor: ['#3ddc84', '#ff5c5c'], borderWidth: 0 }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { position: 'bottom', labels: { color: '#c3cad6' } } },
          cutout: '65%',
        },
      });
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
    renderMiniCoins();
    runForecast();
    runBacktest();
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
        backtest_candles=config.BACKTEST_CANDLES,
        time_options=config.TIME_OPTIONS,
        time_options_json=json.dumps(config.TIME_OPTIONS),
        default_time_key=next((o["key"] for o in config.TIME_OPTIONS if o["timeframe"] == config.CHART_TIMEFRAME), config.TIME_OPTIONS[0]["key"]),
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


@app.route("/api/best_time", methods=["GET", "POST"])
def api_best_time():
    symbol = request.args.get("symbol", config.SYMBOL)
    if request.method == "POST":
        _run_best_time_async(symbol)
    with _lock:
        return jsonify(_best_time_cache.get(symbol, {"running": False, "result": None}))


if __name__ == "__main__":
    threading.Thread(target=_signal_poll_loop, daemon=True).start()
    threading.Thread(target=_realtime_price_loop, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=3100, debug=False, allow_unsafe_werkzeug=True)
