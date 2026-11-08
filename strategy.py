from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import math

from indicators import enrich, trend
from macro_policy import direction_policy
from candle_intelligence import candle_patterns
from independent_engines import evaluate_engines
from strategy_modes import select_mode


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
    counter_macro: bool = False
    candle_close_ms: int = 0
    htf_trends: str = ""
    macro_trends: str = ""
    strategy_name: str = ""

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
    # Honor MIN_SCORE (5..10); the caller owns the threshold.
    if score < 5:
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


def _independent_candidate(row, side, live_price, evidence, style_id=None):
    """Independent closed-candle entry when the legacy pullback/retest misses.

    A volume-only candle is NEVER an entry; the asset still needs a verified
    sweep, breakout, or continuation, directional structure, a fresh quote,
    controlled extension and an ATR-bounded stop.
    """
    names = {e.engine for e in evidence if e.confirmed}
    triggers = names & {"Liquidity Sweep", "Breakout", "Trend Continuation"}
    # An explicitly selected verified 15m style may provide the trigger;
    # directional trend, live-quote, volatility and risk checks stay below.
    if not triggers and style_id not in ("breakout", "ema", "rsi_macd"):
        return None
    try:
        live = float(live_price)
        close, atr = float(row.close), float(row.atr)
        adx, vol = float(row.adx), float(row.vol_ratio)
        width, body = float(row.range), float(row.body_atr)
        efficiency = float(row.efficiency)
        rsi = float(row.rsi)
        extension = float(row.ema21_extension)
        ema9, ema21, ema50 = float(row.ema9), float(row.ema21), float(row.ema50)
    except (ValueError, TypeError, AttributeError):
        return None
    if not _valid(live, close, atr):
        return None
    # A modestly wider entry zone than the legacy strategy, not a chase.
    if abs(live - close) > 0.32 * atr:
        return None
    if not (12 <= adx <= 50 and 0.85 <= vol <= 5.0
            and 0.15 <= body and width <= 1.55 * atr and efficiency >= 0.10):
        return None
    if "Liquidity Sweep" in triggers and len(triggers) == 1 and vol < 1.25:
        return None
    is_long = side == "LONG"
    if is_long:
        if not (close > ema9 and ema21 > ema50 and live > ema9
                and 45 <= rsi <= 75 and extension <= 1.20):
            return None
        stop = min(float(row.recent_swing_low) - 0.12 * atr, live - 0.85 * atr)
    else:
        if not (close < ema9 and ema21 < ema50 and live < ema9
                and 25 <= rsi <= 55 and extension >= -1.20):
            return None
        stop = max(float(row.recent_swing_high) + 0.12 * atr, live + 0.85 * atr)
    risk = abs(live - stop)
    if not math.isfinite(risk) or not (0.65 * atr <= risk <= 2.2 * atr):
        return None
    if (is_long and stop >= live) or (not is_long and stop <= live):
        return None
    score = min(10, 6 + int(adx >= 18) + int(vol >= 1.5) +
                int(len(triggers) >= 2) + int(efficiency >= 0.22))
    setup = ({"breakout": "Breakout", "ema": "EMA Trend Change",
              "rsi_macd": "RSI + MACD Confirmation"}.get(style_id)
             if not triggers else
             ("Breakout" if "Breakout" in triggers else
              "Liquidity Sweep" if "Liquidity Sweep" in triggers else
              "Trend Continuation"))
    return Signal(
        symbol="", side=side, entry=live, stop_loss=stop,
        tp1=live + (1.5 * risk if is_long else -1.5 * risk),
        tp2=live + (2.5 * risk if is_long else -2.5 * risk),
        score=score, reasons=[f"{e.engine}: {e.reason}" for e in evidence
                              if e.confirmed],
        timestamp=datetime.now(timezone.utc).isoformat(), atr=atr,
        risk_distance_pct=risk / live * 100, setup=setup,
    )


def evaluate(symbol, candles, btc_trend, eth_trend, min_score=7, current_price=None,
             btc_return_6_pct=None, eth_return_6_pct=None, strategy_mode=None):
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
    try:
        asset_return_6_pct = 100 * (float(frames["4h"].close.iloc[-1]) / float(frames["4h"].close.iloc[-7]) - 1)
    except (ValueError, TypeError, ZeroDivisionError, IndexError):
        return None
    for side in ("LONG", "SHORT"):
        wanted = "BULLISH" if side == "LONG" else "BEARISH"
        opposite = "BEARISH" if side == "LONG" else "BULLISH"
        engine_evidence = evaluate_engines(candles["15m"], side)
        strong_15m = sum(
            e.confirmed for e in engine_evidence
            if e.engine in ("Liquidity Sweep", "Breakout", "Trend Continuation")
        ) >= 2
        h1_direction = trend(h1)
        h4_direction = trend(h4)
        # 4H alignment stays mandatory. A neutral 1H may be overruled by
        # two independently confirmed closed-candle engines and 4H strength.
        if (h4_direction != wanted or trend(d1) == opposite or
                (h1_direction != wanted and not (
                    h1_direction == "NEUTRAL" and strong_15m and
                    float(h4.adx) >= 18
                ))):
            continue
        selected = (select_mode(candles["15m"], side, strategy_mode, "crypto")
                    if strategy_mode is not None else None)
        if strategy_mode is not None and selected is None:
            continue
        candidate = _candidate(fast, side, current_price)
        if candidate is None:
            candidate = _independent_candidate(
                fast, side, current_price, engine_evidence,
                style_id=selected[0] if selected else None,
            )
        if candidate is None:
            continue
        if btc_trend == wanted and eth_trend == wanted:
            candidate.score = min(10, candidate.score + 1)
            candidate.reasons.append("BTC and ETH support the direction")
        allowed, independent = direction_policy(
            side, btc_trend, eth_trend, candidate.score, asset_return_6_pct,
            btc_return_6_pct, eth_return_6_pct, h4.adx,
        )
        if not allowed or candidate.score < min_score:
            continue
        if independent:
            candidate.counter_macro = True
            candidate.reasons.append("Strong independent 4H trend despite BTC/ETH opposition")
        # A selected style must independently confirm this otherwise safe
        # core candidate; do not bypass HTF, quote, macro or anti-chase gates.
        # None retains original research/backtest semantics; the live bot
        # passes a selected 15m style which was checked before entry creation.
        if selected is not None:
            candidate.strategy_name = selected[1]
            candidate.reasons.append("Selected style: " + selected[1])
        candidate.symbol = symbol
        candidate.htf_trends = (
            f"15m:{trend(fast)} | 1H:{h1_direction} | "
            f"4H:{h4_direction} | 1D:{trend(d1)}"
        )
        candidate.macro_trends = f"BTC:{btc_trend} | ETH:{eth_trend}"
        patterns = candle_patterns(candles['15m'])
        aligned = ('Bullish Engulfing', 'Bullish Rejection') if side == 'LONG' else ('Bearish Engulfing', 'Bearish Rejection')
        candidate.reasons.extend(p for p in patterns if p in aligned)
        # Independent triggers can create an entry; explanatory evidence is
        # appended to the legacy candidate only, avoiding duplicate reasons.
        if candidate.setup in ("Pullback", "Breakout retest"):
            candidate.reasons.extend(
                f"{e.engine}: {e.reason}" for e in engine_evidence if e.confirmed
            )
        if h1_direction != wanted:
            candidate.reasons.append("1H neutral; strong 4H + 15m exception")
        try:
            candidate.candle_close_ms = int(float(candles["15m"].timestamp.iloc[-1])) + 900_000
        except (TypeError, ValueError, AttributeError):
            candidate.candle_close_ms = 0
        candidates.append(candidate)
    return max(candidates, key=lambda item: item.score) if candidates else None
