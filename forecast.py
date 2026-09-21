"""
Prophet-based price forecast, separate from signal_engine's rules-based
BUY/SELL/HOLD logic.

Honesty note (same spirit as signal_engine.py): Prophet fits a trend +
seasonality curve to historical closes (daily candles for the day/month/year
horizons, intraday candles for 15m/30m/4h) and extrapolates it. It has no
knowledge of news, order flow, or regime changes, and crypto prices are
close to a random walk at these horizons — treat the forecast band as "one
statistical projection of recent trend continuing," not a prediction. The
intraday horizons have far less history to fit on and are the least
reliable of the six. Widening uncertainty (yhat_lower/yhat_upper) further
out is the honest part of the output; the point estimate (yhat) further out
is the least reliable.
"""

from __future__ import annotations
import pandas as pd
from prophet import Prophet

import config
from data_fetcher import fetch_ohlcv_history, get_exchange


def run_forecast(symbol: str, horizon_key: str, exchange=None) -> dict:
    """
    Fit Prophet on `symbol` candles at the timeframe configured for
    `horizon_key` (config.FORECAST_HORIZONS — intraday keys like "15m"/"30m"/
    "4h" fit on that candle size; "day"/"month"/"year" fit on daily candles)
    and forecast that horizon's number of candles ahead.

    Returns a JSON-friendly dict with the forecast (including uncertainty
    bounds), so the dashboard can plot it. `time` in each forecast point is
    a unix timestamp (seconds), matching the dashboard's chart data.
    """
    if horizon_key not in config.FORECAST_HORIZONS:
        raise ValueError(f"Unknown horizon '{horizon_key}', expected one of {list(config.FORECAST_HORIZONS)}")
    spec = config.FORECAST_HORIZONS[horizon_key]
    timeframe, periods, freq, history_candles = spec["timeframe"], spec["periods"], spec["freq"], spec["history_candles"]

    ex = exchange or get_exchange()
    df = fetch_ohlcv_history(symbol=symbol, timeframe=timeframe, total_candles=history_candles, exchange=ex)
    if len(df) < 30:
        raise ValueError(f"Not enough {timeframe} history to fit a forecast (need 30+ candles).")

    prophet_df = pd.DataFrame({
        "ds": df.index.tz_localize(None),
        "y": df["close"].values,
    })

    # Only enable a seasonality component if there's enough history span to
    # actually estimate it — otherwise Prophet either warns or overfits noise.
    span_days = (df.index[-1] - df.index[0]).days
    is_intraday = timeframe not in ("1d",)
    model = Prophet(
        daily_seasonality=is_intraday and span_days >= 2,
        weekly_seasonality=span_days >= 14,
        yearly_seasonality=span_days >= 365,
    )
    model.fit(prophet_df)

    future = model.make_future_dataframe(periods=periods, freq=freq)
    forecast = model.predict(future)

    forecast_points = forecast[forecast["ds"] > prophet_df["ds"].iloc[-1]]
    forecast_out = [
        {
            "time": int(row.ds.timestamp()),
            "yhat": float(row.yhat),
            "yhat_lower": float(row.yhat_lower),
            "yhat_upper": float(row.yhat_upper),
        }
        for row in forecast_points.itertuples()
    ]

    last_price = float(prophet_df["y"].iloc[-1])
    end_price = forecast_out[-1]["yhat"] if forecast_out else last_price
    band_width_pct = (
        (forecast_out[-1]["yhat_upper"] - forecast_out[-1]["yhat_lower"]) / last_price * 100.0
        if forecast_out else 0.0
    )

    return {
        "symbol": symbol,
        "horizon": horizon_key,
        "timeframe": timeframe,
        "periods": periods,
        "forecast": forecast_out,
        "last_price": last_price,
        "projected_change_pct": (end_price - last_price) / last_price * 100.0,
        # Transparency: exactly what fed the model, so the number isn't a black box.
        "fit_details": {
            "history_candles": len(df),
            "history_start": prophet_df["ds"].iloc[0].strftime("%Y-%m-%d %H:%M"),
            "history_end": prophet_df["ds"].iloc[-1].strftime("%Y-%m-%d %H:%M"),
            "span_days": span_days,
            "seasonality": {
                "daily": is_intraday and span_days >= 2,
                "weekly": span_days >= 14,
                "yearly": span_days >= 365,
            },
            "uncertainty_band_pct_of_price": band_width_pct,
        },
    }


if __name__ == "__main__":
    import json
    result = run_forecast(config.SYMBOL, "month")
    print(f"{result['symbol']} — {result['periods']}d forecast:")
    print(f"  last price:        {result['last_price']:.2f}")
    print(f"  projected change:  {result['projected_change_pct']:+.2f}%")
    print(json.dumps(result["forecast"][:3], indent=2))
