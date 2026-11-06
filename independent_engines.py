"""Independent, closed-candle research engines. Manual signals only.

Call with OHLCV frames containing *completed* bars; the caller owns candle closure.
No order placement, probability claims or automatic alert generation.
"""
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class EngineEvidence:
    engine: str
    direction: str
    confirmed: bool
    reason: str

def _rows(frame):
    if frame is None or len(frame) < 25:
        return None
    try:
        if hasattr(frame, "iloc"):
            return [frame.iloc[i] for i in range(len(frame)-25, len(frame))]
        return list(frame)[-25:]
    except (TypeError, ValueError, IndexError):
        return None

def _num(row, key):
    try:
        v = row[key] if isinstance(row, dict) else getattr(row, key) if hasattr(row, key) else row[key]
        f = float(v)
        return f if math.isfinite(f) else None
    except (KeyError, AttributeError, TypeError, ValueError):
        return None

def evaluate_engines(frame, direction="LONG"):
    """Return independent evidence; never use the current forming candle."""
    if direction not in ("LONG", "SHORT"):
        return []
    rows = _rows(frame)
    if rows is None:
        return []
    candles = []
    for r in rows:
        c = {k: _num(r,k) for k in ("open","high","low","close","volume")}
        if any(v is None for v in c.values()) or c["high"] < max(c["open"],c["close"],c["low"]) or c["low"] > min(c["open"],c["close"]) or c["volume"] < 0:
            return []
        candles.append(c)
    last, prev = candles[-1], candles[-2]
    prior = candles[-21:-1]
    avg_vol = sum(c["volume"] for c in prior)/len(prior)
    vol_ok = avg_vol > 0 and last["volume"] >= 1.5 * avg_vol
    high = max(c["high"] for c in prior)
    low = min(c["low"] for c in prior)
    bullish = direction == "LONG"
    sweep = last["low"] < low and last["close"] > low if bullish else last["high"] > high and last["close"] < high
    breakout = last["close"] > high and prev["close"] <= high if bullish else last["close"] < low and prev["close"] >= low
    closes = [c["close"] for c in candles]
    recent = sum(closes[-5:])/5
    baseline = sum(closes[-20:])/20
    trend = recent > baseline and last["close"] > recent if bullish else recent < baseline and last["close"] < recent
    body = last["close"] > last["open"] if bullish else last["close"] < last["open"]
    checks = (
        ("Liquidity Sweep", sweep and body, "Prior range swept and reclaimed"),
        ("Breakout", breakout and body and vol_ok, "Prior range broken with elevated volume"),
        ("Trend Continuation", trend and body and vol_ok, "Trend and volume confirm continuation"),
        ("Volume Pressure", vol_ok and body, "Directional candle with elevated volume"),
    )
    return [EngineEvidence(name,direction,bool(ok),reason) for name,ok,reason in checks]
