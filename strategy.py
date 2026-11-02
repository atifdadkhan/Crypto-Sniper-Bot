from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math

from indicators import enrich, trend


@dataclass
class Signal:
    symbol: str
    side: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    score: int
    reasons: list
    timestamp: str
    atr: float
    risk_distance_pct: float
    setup: str

    def to_dict(self):
        return asdict(self)


def _valid(*values):
    try:
        return all(math.isfinite(float(v)) and float(v) > 0 for v in values)
    except (TypeError, ValueError):
        return False


def _candidate(row, side, live_price):
    """Evaluate only completed candles; price must still be close to the setup."""
    is_long = side == "LONG"
    close, atr = float(row.close), float(row.atr)
    if not _valid(close, atr):
        return None

    try:
        live = float(live_price)
    except (TypeError, ValueError):
        return None
    # Fail closed on missing/stale-looking quotes or a move that has already run.
    if not _valid(live) or abs(live - close) > 0.22 * atr:
        return None
    # Do not accept a quote that has already crossed the invalidation level.
    if is_long and live <= float(row.ema9):
        return None
    if not is_long and live >= float(row.ema9):
        return None

    rsi, adx = float(row.rsi), float(row.adx)
    extension = float(row.ema21_extension)
    vol_ratio = float(row.vol_ratio)
    efficiency = float(row.efficiency)
    candle_range = float(row.range)
    body_atr = float(row.body_atr)
    close_loc = float(row.close_location)
    range_width = max(float(row.range_high - row.range_low), 1e-12)
    range_position = (close - float(row.range_low)) / range_width

    # Reject abnormal impulse candles and low-quality/choppy regimes.
    if candle_range > 1.55 * atr or body_atr < 0.12 or adx < 12 or adx > 50:
        return None
    if efficiency < 0.14 or not (0.55 <= vol_ratio <= 5.0):
        return None

    if is_long:
        if not (row.close > row.ema21 > row.ema50 and row.ema21 > row.ema21_prev3):
            return None
        if not (48 <= rsi <= 67 and -0.55 <= extension <= 1.00):
            return None
        if close_loc < 0.60 or row.close <= row.open:
            return None
        # A pullback must have touched the EMA zone recently and then reclaimed it.
        pullback = bool(row.pullback_touch_long and row.close > row.ema9)
        # A retest requires a prior breakout in recent completed candles, not merely
        # a breakout candle whose wick happens to cross resistance.
        breakout_retest = bool(row.breakout_retest_long and row.close > row.prior_high)
        if range_position > 0.94 and not breakout_retest:
            return None
        if not (pullback or breakout_retest):
            return None
        if rsi >= 65 and extension > 0.70:
            return None
        swing = float(row.recent_swing_low)
        stop = min(swing - 0.12 * atr, live - 0.85 * atr)
        setup = "Breakout retest" if breakout_retest else "Pullback"
        structural_quality = float(row.lower_wick) >= max(float(row.body) * 0.10, 0.02 * atr)
    else:
        if not (row.close < row.ema21 < row.ema50 and row.ema21 < row.ema21_prev3):
            return None
        if not (33 <= rsi <= 52 and -1.00 <= extension <= 0.55):
            return None
        if close_loc > 0.40 or row.close >= row.open:
            return None
        pullback = bool(row.pullback_touch_short and row.close < row.ema9)
        breakout_retest = bool(row.breakout_retest_short and row.close < row.prior_low)
        if range_position < 0.06 and not breakout_retest:
            return None
        if not (pullback or breakout_retest):
            return None
        if rsi <= 35 and extension < -0.70:
            return None
        swing = float(row.recent_swing_high)
        stop = max(swing + 0.12 * atr, live + 0.85 * atr)
        setup = "Breakout retest" if breakout_retest else "Pullback"
        structural_quality = float(row.upper_wick) >= max(float(row.body) * 0.10, 0.02 * atr)

    risk = abs(live - stop)
    if not math.isfinite(risk) or risk < 0.65 * atr or risk > 2.0 * atr:
        return None
    if (is_long and stop >= live) or ((not is_long) and stop <= live):
        return None

    # Explainable confluence score, not a win-probability estimate.
    score = 5
    reasons = [setup]
    if 17 <= adx <= 40:
        score += 1
        reasons.append("trend is established")
    if 0.85 <= vol_ratio <= 3.5:
        score += 1
        reasons.append("trading activity confirms the move")
    if 0.22 <= efficiency <= 0.82:
        score += 1
        reasons.append("price movement is reasonably clean")
    if (is_long and 51 <= rsi <= 63) or ((not is_long) and 37 <= rsi <= 49):
        score += 1
        reasons.append("momentum is not stretched")
    if structural_quality:
        score += 1
        reasons.append("price rejected the pullback area")
    if score < 7:
        return None

    # Entry is the current quote, not the old candle close. Recalculate risk/targets.
    tp1 = live + (1.5 * risk if is_long else -1.5 * risk)
    tp2 = live + (2.5 * risk if is_long else -2.5 * risk)
    if not _valid(live, stop, tp1, tp2):
        return None
    return Signal(
        symbol="", side=side, entry=live, stop_loss=stop, tp1=tp1, tp2=tp2,
        score=min(score, 10), reasons=reasons,
        timestamp=datetime.now(timezone.utc).isoformat(), atr=atr,
        risk_distance_pct=risk / live * 100, setup=setup,
    )


def evaluate(symbol, candles, btc_trend, eth_trend, min_score=7, current_price=None):
    """Multi-timeframe quantitative setup with anti-chase and structure filters."""
    required = ("15m", "1h", "4h", "1d")
    if not isinstance(candles, dict) or any(tf not in candles for tf in required):
        return None
    try:
        frames = {tf: enrich(candles[tf]) for tf in required}
    except (ValueError, KeyError, TypeError):
        return None

    latest = {tf: frame.iloc[-1] for tf, frame in frames.items()}
    fast, h1, h4, d1 = (latest[tf] for tf in required)
    candidates = []
    for side in ("LONG", "SHORT"):
        wanted = "BULLISH" if side == "LONG" else "BEARISH"
        opposite = "BEARISH" if side == "LONG" else "BULLISH"
        if trend(h1) != wanted or trend(h4) != wanted or trend(d1) == opposite:
            continue
        if btc_trend == opposite or eth_trend == opposite:
            continue
        candidate = _candidate(fast, side, current_price)
        if candidate is None or candidate.score < min_score:
            continue
        candidate.symbol = symbol
        if btc_trend == wanted and eth_trend == wanted:
            candidate.score = min(10, candidate.score + 1)
            candidate.reasons.append("BTC and ETH support the direction")
        candidates.append(candidate)
    return max(candidates, key=lambda item: item.score) if candidates else None
