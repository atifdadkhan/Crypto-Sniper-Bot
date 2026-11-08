"""Three explainable 15m signal styles. Closed candles only; no order execution.

The caller is responsible for source freshness, candle-close timestamps and
multi-timeframe / live-price risk gates. A match is NOT a win probability.
"""
from __future__ import annotations

import math
import os

import pandas as pd

MODE_LABELS = {
    "recommended": "Best Match (Recommended)",
    "breakout": "Breakout + Volume",
    "ema": "EMA Trend Change",
    "rsi_macd": "RSI + MACD Confirmation",
}


def valid_mode(value: str) -> str:
    return value if value in MODE_LABELS else "recommended"


def _ready(frame) -> pd.DataFrame | None:
    if frame is None or len(frame) < 65:
        return None
    try:
        df = frame.copy()
        for column in ("open", "high", "low", "close", "volume"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        tail = df.tail(65)
        if tail[["open", "high", "low", "close", "volume"]].isna().any().any():
            return None
        if not all(math.isfinite(float(value)) for value in tail[["open", "high", "low", "close", "volume"]].to_numpy().flat):
            return None
        if (tail["close"] <= 0).any() or (tail["volume"] < 0).any():
            return None
        if (tail["high"] < tail[["open", "low", "close"]].max(axis=1)).any():
            return None
        if (tail["low"] > tail[["open", "high", "close"]].min(axis=1)).any():
            return None
        if "timestamp" in df:
            stamps = pd.to_datetime(df["timestamp"], errors="coerce")
            if stamps.isna().any() or not stamps.is_monotonic_increasing or stamps.duplicated().any():
                return None
        return df
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _volume_ratio(df: pd.DataFrame, pos: int, market: str) -> float:
    volume = df["volume"]
    if pos < 20:
        return 0.0
    baseline = volume.iloc[pos - 20:pos]
    # For PSX, compare with the same time-of-day bars when at least three
    # earlier sessions exist; avoids treating the opening burst as a breakout.
    if market == "psx" and "timestamp" in df:
        try:
            stamps = pd.to_datetime(df["timestamp"])
            slot = stamps.iloc[pos].strftime("%H:%M")
            history = volume.iloc[:pos][stamps.iloc[:pos].dt.strftime("%H:%M") == slot]
            if len(history) >= 3:
                baseline = history.tail(10)
        except (TypeError, ValueError, AttributeError):
            return 0.0
    avg = float(baseline.mean())
    return float(volume.iloc[pos]) / avg if avg > 0 else 0.0


def _atr(df: pd.DataFrame) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(),
         (low - close.shift(1)).abs()], axis=1
    ).max(axis=1)
    return float(tr.tail(14).mean())


def _rsi(close: pd.Series) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = up / down.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def select_mode(frame, side: str = "LONG", mode: str = "recommended",
                market: str = "crypto") -> tuple[str, str] | None:
    """Return the selected confirmed 15m style or None, with zero look-ahead."""
    if side not in ("LONG", "SHORT") or market not in ("crypto", "psx"):
        return None
    if market == "psx" and side != "LONG":
        return None  # cash-equity scanner: BUY signals only
    df = _ready(frame)
    if df is None:
        return None
    close = df["close"]
    last = float(close.iloc[-1])
    atr = _atr(df)
    if not math.isfinite(atr) or atr <= 0:
        return None
    if market == "psx":
        try:
            min_turnover = max(0.0, float(os.getenv("PSX_MIN_15M_TURNOVER_PKR", "500000")))
        except (ValueError, OverflowError):
            return None
        avg_turnover = float((df["close"] * df["volume"]).tail(20).mean())
        if not math.isfinite(avg_turnover) or avg_turnover < min_turnover:
            return None

    long = side == "LONG"
    matches: dict[str, bool] = {}

    # Require a volume-confirmed breakout within the last five CLOSED bars;
    # retain a nearby level so a first spike cannot justify chasing far away.
    breakout = False
    for pos in range(len(df) - 5, len(df)):
        level = (float(df["high"].iloc[pos - 20:pos].max()) if long
                 else float(df["low"].iloc[pos - 20:pos].min()))
        event = (float(close.iloc[pos]) > level if long
                 else float(close.iloc[pos]) < level)
        near = (0 <= last - level <= 1.0 * atr if long
                else 0 <= level - last <= 1.0 * atr)
        if event and near and _volume_ratio(df, pos, market) >= 1.5:
            breakout = True
            break
    matches["breakout"] = breakout

    fast = close.ewm(span=12, adjust=False).mean()
    slow = close.ewm(span=26, adjust=False).mean()
    spread = fast - slow if long else slow - fast
    fresh_cross = bool((spread.iloc[-9:-1] <= 0).any() and spread.iloc[-1] > 0)
    moving = float(fast.iloc[-1]) > float(fast.iloc[-4]) if long else float(fast.iloc[-1]) < float(fast.iloc[-4])
    near_fast = abs(last - float(fast.iloc[-1])) <= 1.0 * atr
    matches["ema"] = bool(fresh_cross and moving and near_fast)

    macd = fast - slow
    signal = macd.ewm(span=9, adjust=False).mean()
    histogram = macd - signal
    rsi = float(_rsi(close).iloc[-1])
    momentum = (histogram.iloc[-1] > 0 and histogram.iloc[-1] > histogram.iloc[-2]
                and 50 < rsi <= 68 and last > float(fast.iloc[-1])) if long else (
                histogram.iloc[-1] < 0 and histogram.iloc[-1] < histogram.iloc[-2]
                and 32 <= rsi < 50 and last < float(fast.iloc[-1]))
    matches["rsi_macd"] = bool(momentum)

    mode = valid_mode(mode)
    if mode == "recommended":
        mode = next((name for name in ("breakout", "ema", "rsi_macd") if matches[name]), "")
    return (mode, MODE_LABELS[mode]) if mode and matches.get(mode, False) else None
