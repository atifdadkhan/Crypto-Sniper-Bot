"""Independent experimental 4H Relative Strength Breakout signal engine.

Signal-only, no exchange private endpoints and no order execution. The model is
the predeclared Bitget 2026 research rule; it is not proof of future profit.
Original 15m Pullback/Retest engine remains in strategy.py.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math

import numpy as np
import pandas as pd

STRATEGY_ID = "RS_4H"
STRATEGY_NAME = "4H Relative Strength Breakout"
TF_MS = 14_400_000
MIN_HISTORY = 320
OUTPERFORMANCE_PP = 3.0


@dataclass
class RelativeStrengthSignal:
    symbol: str
    side: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    score: int
    reasons: list[str]
    timestamp: str
    atr: float
    risk_distance_pct: float
    setup: str
    candle_close_ms: int
    strategy_id: str = STRATEGY_ID
    strategy_name: str = STRATEGY_NAME
    timeframe: str = "4H"
    ttl_seconds: int = 3600
    counter_macro: bool = False

    def to_dict(self):
        return asdict(self)


def _finite(*values):
    try:
        return all(math.isfinite(float(value)) for value in values)
    except (TypeError, ValueError):
        return False


def _validated(df):
    if df is None or not isinstance(df, pd.DataFrame) or len(df) < MIN_HISTORY:
        raise ValueError("DATA INSUFFICIENT: 320 completed 4H candles are required")
    required = ("timestamp", "open", "high", "low", "close", "volume")
    if any(c not in df.columns for c in required):
        raise ValueError("DATA INSUFFICIENT: 4H OHLCV columns missing")
    x = df.loc[:, required].copy()
    for key in required:
        x[key] = pd.to_numeric(x[key], errors="coerce")
    if x.isna().any().any() or not np.isfinite(x.to_numpy(dtype=float)).all():
        raise ValueError("DATA INSUFFICIENT: non-finite 4H candle")
    x = x.sort_values("timestamp")
    if x.timestamp.duplicated().any() or not (x.timestamp.diff().dropna() == TF_MS).all():
        raise ValueError("DATA INSUFFICIENT: 4H timestamps not contiguous")
    if not (x["timestamp"] % TF_MS == 0).all():
        raise ValueError("DATA INSUFFICIENT: off-grid 4H timestamp")
    if (x[["open", "high", "low", "close"]] <= 0).any().any() or (x.volume < 0).any():
        raise ValueError("DATA INSUFFICIENT: bad OHLCV values")
    if ((x.high < x[["open", "close", "low"]].max(axis=1)) |
            (x.low > x[["open", "close", "high"]].min(axis=1))).any():
        raise ValueError("DATA INSUFFICIENT: impossible OHLCV geometry")
    return x.reset_index(drop=True)


def precheck_recent_breakout(df, btc, eth):
    """Cheap prefilter: ask for expensive EMA300 warmup only if a 4H breakout exists."""
    if df is None or len(df) < 50:
        return False
    try:
        recent = df.iloc[-1]
        price, vol = float(recent.close), float(recent.volume)
        avg = float(df.volume.iloc[-21:-1].mean())
        old = float(df.close.iloc[-43])
        btc_return = float(btc["return_7d_pct"])
        eth_return = float(eth["return_7d_pct"])
        stamp = int(recent.timestamp) + TF_MS
        if (stamp != int(btc["candle_close_ms"]) or stamp != int(eth["candle_close_ms"])):
            return False
        if not _finite(price, vol, avg, old, btc_return, eth_return) or min(price, avg, old) <= 0:
            return False
        if vol / avg < 0.7:
            return False
        roc = 100.0 * (price / old - 1.0)
        return bool((price > float(df.high.iloc[-21:-1].max()) and
                     roc > max(btc_return, eth_return) + OUTPERFORMANCE_PP) or
                    (price < float(df.low.iloc[-21:-1].min()) and
                     roc < min(btc_return, eth_return) - OUTPERFORMANCE_PP))
    except (ValueError, TypeError, IndexError, KeyError, ZeroDivisionError, OverflowError):
        return False


def evaluate_relative_strength(symbol, candles, live_price, btc, eth, ttl_seconds=3600):
    """Research-derived 4H signal from fully CLOSED and continuous OHLCV bars.

    Macro 7d returns must belong to the exact SAME 4h candle close. The
    reference backtest enters at the next 4h OPEN; live alerts use fresh quotes
    with a conservative 0.65 ATR anti-chase bound instead.
    """
    x = _validated(candles)
    if not precheck_recent_breakout(x, btc, eth):
        return None
    close, high, low = x.close, x.high, x.low
    ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean().iloc[-1]
    ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean().iloc[-1]
    # The original research required finite EMA300 warmup even for RS candidates.
    ema300 = close.ewm(span=300, adjust=False, min_periods=300).mean().iloc[-1]
    prev_close = close.shift(1)
    tr = pd.concat((high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()), axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    atr = float(atr_series.iloc[-1])
    up, down = high.diff(), -low.diff()
    plus = up.where((up > down) & (up > 0), 0.0)
    minus = down.where((down > up) & (down > 0), 0.0)
    pdi = 100 * plus.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr_series.replace(0, np.nan)
    mdi = 100 * minus.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr_series.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx = float(dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean().iloc[-1])
    price = float(close.iloc[-1])
    if not _finite(ema50, ema200, ema300, atr, adx) or atr <= 0 or adx < 18:
        return None

    coin_return = 100 * (price / float(close.iloc[-43]) - 1)
    btc_return, eth_return = float(btc["return_7d_pct"]), float(eth["return_7d_pct"])
    long_regime = price > ema200 and ema50 > ema200
    short_regime = price < ema200 and ema50 < ema200
    long_signal = long_regime and price > high.iloc[-21:-1].max() and (
        coin_return > max(btc_return, eth_return) + OUTPERFORMANCE_PP)
    short_signal = short_regime and price < low.iloc[-21:-1].min() and (
        coin_return < min(btc_return, eth_return) - OUTPERFORMANCE_PP)
    if long_signal == short_signal:
        return None

    try:
        live = float(live_price)
    except (ValueError, TypeError):
        return None
    if not _finite(live) or live <= 0 or abs(live - price) > 0.65 * atr:
        return None

    # Live alerts use quote entry, rather than falsely claiming the historic
    # next 4H candle open was a fill. 2ATR stop and a single 3R target.
    risk = 2 * atr
    risk_pct = risk / live * 100
    if not 0.3 <= risk_pct <= 12.0:
        return None
    side = "LONG" if long_signal else "SHORT"
    direction = 1 if long_signal else -1
    stop = live - direction * risk
    target = live + direction * 3.0 * risk
    if not _finite(stop, target) or min(stop, target) <= 0:
        return None
    candle_close_ms = int(x.timestamp.iloc[-1]) + TF_MS
    if not isinstance(ttl_seconds, int) or not 120 <= ttl_seconds <= TF_MS:
        raise ValueError("Invalid 4H signal expiry")
    return RelativeStrengthSignal(
        symbol=symbol, side=side, entry=live, stop_loss=stop,
        tp1=target, tp2=target, score=0,
        reasons=[f"7-day coin return {coin_return:+.2f}% vs BTC {btc_return:+.2f}% / ETH {eth_return:+.2f}%",
                 f"4H EMA50 / EMA200 trend with ADX {adx:.1f}",
                 "Breakout confirmed on completed 4H candle"],
        timestamp=datetime.fromtimestamp(candle_close_ms / 1000, timezone.utc).isoformat(),
        atr=atr, risk_distance_pct=risk_pct, setup="Relative Strength Breakout",
        candle_close_ms=candle_close_ms, ttl_seconds=ttl_seconds,
        counter_macro=(btc.get("trend") == ("BEARISH" if long_signal else "BULLISH") or
                       eth.get("trend") == ("BEARISH" if long_signal else "BULLISH")),
    )
