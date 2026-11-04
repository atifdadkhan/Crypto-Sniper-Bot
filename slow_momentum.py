"""Experimental slow 4H momentum-breakout confirmation for manual signals only.

Research rule predeclared in November-2026 Bitget study; not proof of profits.
Used as a fallback inside the existing 4H engine, NOT a third polling bot.
No Bitget private API calls, keys, leverage settings or order placement.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
import numpy as np
import pandas as pd

from relative_strength import _validated, _finite, TF_MS

STRATEGY_ID = "SLOW_4H"
STRATEGY_NAME = "4H Slow Momentum Breakout"
CHANNEL_BARS = 36
MAX_SIGNAL_DELAY_SECONDS = 3600


@dataclass
class SlowMomentumSignal:
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
    ttl_seconds: int = MAX_SIGNAL_DELAY_SECONDS
    counter_macro: bool = False
    hold_review_days: int = 10

    def to_dict(self):
        return asdict(self)


def precheck_slow_momentum(recent):
    """Cheap, *necessary* prefilter with no EMA warmup/lookahead.

    Only the 36-prior-4H channel and previous 20-bar volume baseline are
    inspected. Full EMA/ADX validation follows after real historical paging.
    """
    if recent is None or not isinstance(recent, pd.DataFrame) or len(recent) < 45:
        return False
    try:
        r = recent.iloc[-1]
        c, v = float(r.close), float(r.volume)
        avg = float(recent.volume.iloc[-21:-1].mean())
        if not _finite(c, v, avg) or c <= 0 or avg <= 0 or v / avg < .65:
            return False
        return bool(c > float(recent.high.iloc[-37:-1].max()) or
                    c < float(recent.low.iloc[-37:-1].min()))
    except (ValueError, TypeError, IndexError, OverflowError):
        return False


def evaluate_slow_momentum(symbol, candles, live_price, ttl_seconds=3600):
    """Slow 4H momentum, as used in the previously simulated Bitget policy.

    Conditions: EMA120 over EMA300, EMA120 slope over 6 complete bars,
    36-candle channel breakout, ADX14>=16 and vol>=0.65 * prev20 average.
    Reference historical entry is the next 4h open; live uses fresh quote.
    """
    x = _validated(candles)  # >=320 real closed, consecutive 4H OHLCV bars
    if not precheck_slow_momentum(x):
        return None
    c, h, l = x.close, x.high, x.low
    e120 = c.ewm(span=120, adjust=False, min_periods=120).mean()
    e300 = c.ewm(span=300, adjust=False, min_periods=300).mean()
    prev = c.shift(1)
    tr = pd.concat((h-l, (h-prev).abs(), (l-prev).abs()), axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    plus = h.diff()
    minus = -l.diff()
    pdm = plus.where((plus > minus) & (plus > 0), 0.0)
    mdm = minus.where((minus > plus) & (minus > 0), 0.0)
    pdi = 100 * pdm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr_series.replace(0, np.nan)
    mdi = 100 * mdm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr_series.replace(0, np.nan)
    dx = 100 * (pdi-mdi).abs() / (pdi+mdi).replace(0, np.nan)
    adx = float(dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean().iloc[-1])
    atr, close = float(atr_series.iloc[-1]), float(c.iloc[-1])
    avgvol = float(x.volume.iloc[-21:-1].mean())
    volratio = float(x.volume.iloc[-1]) / avgvol if avgvol > 0 else float("nan")
    e_fast, e_slow = float(e120.iloc[-1]), float(e300.iloc[-1])
    prev_fast = float(e120.iloc[-7])
    if (not _finite(adx, atr, volratio, e_fast, e_slow, prev_fast)
            or atr <= 0 or adx < 16 or volratio < .65):
        return None

    long_ok = (close > e_fast > e_slow and e_fast > prev_fast
               and close > float(h.iloc[-37:-1].max()))
    short_ok = (close < e_fast < e_slow and e_fast < prev_fast
                and close < float(l.iloc[-37:-1].min()))
    if long_ok == short_ok:
        return None

    try:
        entry = float(live_price)
    except (TypeError, ValueError):
        return None
    if not _finite(entry) or entry <= 0 or abs(entry - close) > 0.65*atr:
        return None
    risk = 2.5*atr
    risk_pct = 100*risk/entry
    if not .3 <= risk_pct <= 12.0:
        return None
    side = "LONG" if long_ok else "SHORT"
    direction = 1 if long_ok else -1
    stop = entry-direction*risk
    target = entry+direction*3*risk
    if not _finite(stop, target) or min(stop, target) <= 0:
        return None
    if not isinstance(ttl_seconds, int) or not 120 <= ttl_seconds <= TF_MS:
        raise ValueError("Invalid 4H signal expiry")

    close_ms = int(x.timestamp.iloc[-1]) + TF_MS
    return SlowMomentumSignal(
        symbol=symbol, side=side, entry=entry, stop_loss=stop,
        tp1=target, tp2=target, score=0,
        reasons=[f"4H EMA120/EMA300 aligned with 36-candle breakout",
                 f"ADX {adx:.1f} and 4H relative volume {volratio:.2f}"],
        timestamp=datetime.fromtimestamp(close_ms/1000, timezone.utc).isoformat(),
        atr=atr, risk_distance_pct=risk_pct,
        setup="Slow Momentum Breakout", candle_close_ms=close_ms,
        ttl_seconds=ttl_seconds,
    )
