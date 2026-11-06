"""Causal candle-pattern observations for live signal confirmation.

Consumes only completed OHLCV candles supplied by the caller.
No predictions, model training, trading orders or external dependencies.
"""
from __future__ import annotations
import math


def candle_patterns(frame):
    """Return detected patterns from the last two completed candles.

    Caller MUST remove any still-forming candle before invoking this function.
    Empty list means no confirmed pattern; it does not imply a trading direction.
    """
    if frame is None or len(frame) < 2:
        return []
    a, b = frame.iloc[-2], frame.iloc[-1]
    try:
        ao, ac = float(a["open"]), float(a["close"])
        bo, bc = float(b["open"]), float(b["close"])
        bh, bl = float(b["high"]), float(b["low"])
    except (KeyError, TypeError, ValueError):
        return []
    if not all(math.isfinite(v) for v in (ao, ac, bo, bc, bh, bl)):
        return []
    if bh <= bl or not (bl <= min(bo, bc) <= max(bo, bc) <= bh):
        return []
    rng = bh - bl
    body = abs(bc - bo)
    upper = bh - max(bo, bc)
    lower = min(bo, bc) - bl
    patterns = []
    if ac < ao and bc > bo and bo <= ac and bc >= ao:
        patterns.append("Bullish Engulfing")
    if ac > ao and bc < bo and bo >= ac and bc <= ao:
        patterns.append("Bearish Engulfing")
    if body / rng <= 0.12:
        patterns.append("Doji / Indecision")
    elif lower >= 2 * body and upper <= body and bc >= bl + 0.55 * rng:
        patterns.append("Bullish Rejection")
    elif upper >= 2 * body and lower <= body and bc <= bl + 0.45 * rng:
        patterns.append("Bearish Rejection")
    return patterns


def quality_label(score, maximum=10):
    """Quality is not a probability of profit."""
    if maximum <= 0:
        return "Unscored"
    ratio = score / maximum
    return "Strong" if ratio >= 0.9 else "Good" if ratio >= 0.7 else "Moderate"
