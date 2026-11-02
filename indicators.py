import numpy as np
import pandas as pd

REQUIRED = ("open", "high", "low", "close", "volume")


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Build causal features from completed OHLCV candles only."""
    if df is None or len(df) < 100:
        raise ValueError("DATA INSUFFICIENT: at least 100 candles required")
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"DATA INSUFFICIENT: missing columns {missing}")

    x = df.copy()
    for col in REQUIRED:
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x.replace([np.inf, -np.inf], np.nan).dropna(subset=list(REQUIRED))
    x = x[(x["open"] > 0) & (x["high"] > 0) & (x["low"] > 0) &
          (x["close"] > 0) & (x["volume"] >= 0)]
    x = x[(x["high"] >= x[["open", "close", "low"]].max(axis=1)) &
          (x["low"] <= x[["open", "close", "high"]].min(axis=1))]
    if "timestamp" in x.columns:
        x = x.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    if len(x) < 100:
        raise ValueError("DATA INSUFFICIENT: fewer than 100 valid unique candles")

    close, high, low = x.close, x.high, x.low
    x["ema9"] = close.ewm(span=9, adjust=False).mean()
    x["ema21"] = close.ewm(span=21, adjust=False).mean()
    x["ema50"] = close.ewm(span=50, adjust=False).mean()
    x["ema21_prev3"] = x.ema21.shift(3)

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    x["rsi"] = 100 - (100 / (1 + rs))
    x.loc[(loss == 0) & (gain > 0), "rsi"] = 100
    x.loc[(gain == 0) & (loss > 0), "rsi"] = 0
    x.loc[(gain == 0) & (loss == 0), "rsi"] = 50

    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    x["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr_safe = x.atr.replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr_safe
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr_safe
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    x["adx"] = dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean()

    x["vol_avg"] = x.volume.rolling(20, min_periods=20).mean()
    x["vol_ratio"] = x.volume / x.vol_avg.replace(0, np.nan)
    x["range"] = high - low
    x["body"] = (close - x.open).abs()
    x["body_atr"] = x.body / x.atr.replace(0, np.nan)
    x["close_location"] = (close - low) / (x.range.replace(0, np.nan))
    x["upper_wick"] = high - x[["open", "close"]].max(axis=1)
    x["lower_wick"] = x[["open", "close"]].min(axis=1) - low
    x["ema21_extension"] = (close - x.ema21) / x.atr.replace(0, np.nan)
    # Efficiency ratio: net movement divided by total path length (0..1).
    net = close.diff(10).abs()
    path = close.diff().abs().rolling(10, min_periods=10).sum()
    x["efficiency"] = net / path.replace(0, np.nan)
    x["range_high"] = high.shift(1).rolling(48, min_periods=48).max()
    x["range_low"] = low.shift(1).rolling(48, min_periods=48).min()
    x["prior_high"] = high.shift(1).rolling(20, min_periods=20).max()
    x["prior_low"] = low.shift(1).rolling(20, min_periods=20).min()
    x["recent_swing_low"] = low.shift(1).rolling(8, min_periods=8).min()
    x["recent_swing_high"] = high.shift(1).rolling(8, min_periods=8).max()

    # Recent pullback touch + reclaim. All conditions are causal and use closed bars.
    prior_low_5 = low.shift(1).rolling(5, min_periods=5).min()
    prior_high_5 = high.shift(1).rolling(5, min_periods=5).max()
    long_pullback_depth = (prior_low_5 - x.ema21.shift(1)) / x.atr.shift(1).replace(0, np.nan)
    short_pullback_depth = (prior_high_5 - x.ema21.shift(1)) / x.atr.shift(1).replace(0, np.nan)
    x["pullback_touch_long"] = long_pullback_depth.between(-0.65, 0.22)
    x["pullback_touch_short"] = short_pullback_depth.between(-0.22, 0.65)

    # Confirm that a breakout occurred earlier, then price came back to test the
    # broken level and closed back beyond it. Avoid treating the first impulse bar
    # as a completed retest.
    breakout_up = (close > x.prior_high) & (close.shift(1) <= x.prior_high.shift(1))
    breakout_down = (close < x.prior_low) & (close.shift(1) >= x.prior_low.shift(1))
    bar_index = pd.Series(np.arange(len(x)), index=x.index, dtype="float64")
    last_up_index = bar_index.where(breakout_up).ffill()
    last_down_index = bar_index.where(breakout_down).ffill()
    up_level = x.prior_high.where(breakout_up).ffill()
    down_level = x.prior_low.where(breakout_down).ffill()
    up_age = bar_index - last_up_index
    down_age = bar_index - last_down_index
    x["breakout_retest_long"] = (up_age.between(2, 6) &
                                  (low <= up_level + 0.15 * x.atr) &
                                  (low >= up_level - 0.35 * x.atr) & (close > up_level))
    x["breakout_retest_short"] = (down_age.between(2, 6) &
                                   (high >= down_level - 0.15 * x.atr) &
                                   (high <= down_level + 0.35 * x.atr) & (close < down_level))

    x = x.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
    if len(x) < 2:
        raise ValueError("DATA INSUFFICIENT: indicators could not be calculated")
    return x


def trend(row) -> str:
    if row.close > row.ema21 > row.ema50 and row.ema21 > row.ema21_prev3:
        return "BULLISH"
    if row.close < row.ema21 < row.ema50 and row.ema21 < row.ema21_prev3:
        return "BEARISH"
    return "NEUTRAL"
