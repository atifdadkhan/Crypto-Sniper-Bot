import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import pytest
from indicators import enrich, trend
from strategy import evaluate, _candidate
from formatting import signal_text


def make_frame(n=180, direction=1, base=100):
    rows = []
    price = float(base)
    for i in range(n):
        price += direction * (0.15 + (i % 5) * 0.01)
        op = price - direction * 0.04
        rows.append([i * 900000, op, price + 0.12, price - 0.12, price, 1200 + i * 2])
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


def test_enrich_has_quant_features():
    out = enrich(make_frame())
    assert {"ema9", "ema21", "ema50", "rsi", "atr", "adx", "efficiency",
            "ema21_extension", "range_high", "range_low"} <= set(out.columns)
    assert out.iloc[-1].atr > 0
    assert 0 <= out.iloc[-1].efficiency <= 1


def test_insufficient_candles_rejected():
    with pytest.raises(ValueError, match="DATA INSUFFICIENT"):
        enrich(make_frame(30))


def test_bad_ohlc_rows_are_removed():
    df = make_frame()
    df.loc[10, "high"] = 1
    out = enrich(df)
    assert len(out) > 0


def test_trend_identification():
    assert trend(enrich(make_frame()).iloc[-1]) == "BULLISH"
    assert trend(enrich(make_frame(direction=-1, base=200)).iloc[-1]) == "BEARISH"


def test_missing_timeframe_returns_no_signal():
    assert evaluate("BTC/USDT:USDT", {"15m": make_frame()}, "BULLISH", "BULLISH") is None


def test_extended_monotonic_move_is_not_chased():
    frames = {tf: make_frame(direction=1, base=100) for tf in ("15m", "1h", "4h", "1d")}
    # A one-way rally with no pullback/retest must not be treated as a fresh entry.
    assert evaluate("TEST/USDT:USDT", frames, "BULLISH", "BULLISH", current_price=frames["15m"].close.iloc[-1]) is None


def test_live_price_far_from_signal_cancels_entry():
    frames = {tf: make_frame(direction=1, base=100) for tf in ("15m", "1h", "4h", "1d")}
    assert evaluate("TEST/USDT:USDT", frames, "BULLISH", "BULLISH", current_price=200) is None


def test_signal_message_is_short_and_nontechnical():
    class S:
        side, symbol, score, entry, stop_loss, tp1, tp2 = "LONG", "BTC/USDT", 8, 100, 98, 103, 105
        setup = "Pullback"
    msg = signal_text(S())
    assert "Entry:" in msg and "Stop Loss:" in msg and "TP1:" in msg and "TP2:" in msg
    assert "ATR" not in msg and "RSI" not in msg and "EMA" not in msg
    assert len(msg.splitlines()) <= 10


def test_breakout_retest_is_not_assumed_on_first_impulse():
    frame = make_frame(direction=1, base=100)
    out = enrich(frame)
    # A monotonic run has no pullback/retest and must not be relabelled as one.
    assert not bool(out.iloc[-1].breakout_retest_long)
    assert not bool(out.iloc[-1].breakout_retest_short)


def test_live_quote_required_for_signal_evaluation():
    frames = {tf: make_frame(direction=1, base=100) for tf in ("15m", "1h", "4h", "1d")}
    assert evaluate("TEST/USDT:USDT", frames, "BULLISH", "BULLISH", current_price=None) is None


def test_candidate_uses_live_quote_as_entry_and_recalculates_targets():
    row = pd.Series({
        "close": 100.0, "atr": 1.0, "ema9": 99.8, "ema21": 99.0,
        "ema50": 98.0, "ema21_prev3": 98.8, "ema21_extension": 0.5,
        "rsi": 58.0, "adx": 25.0, "range": 0.8, "body_atr": 0.3,
        "close_location": 0.75, "vol_ratio": 1.5, "efficiency": 0.4,
        "range_low": 95.0, "range_high": 101.0, "open": 99.7,
        "lower_wick": 0.15, "body": 0.3, "pullback_touch_long": True,
        "breakout_retest_long": False, "recent_swing_low": 99.0,
    })
    signal = _candidate(row, "LONG", 100.05)
    assert signal is not None
    assert signal.entry == 100.05
    assert signal.tp1 > signal.entry and signal.tp2 > signal.tp1


def test_candidate_rejects_long_at_recent_range_top():
    row = pd.Series({
        "close": 100.0, "atr": 1.0, "ema9": 99.8, "ema21": 99.0,
        "ema50": 98.0, "ema21_prev3": 98.8, "ema21_extension": 0.5,
        "rsi": 58.0, "adx": 25.0, "range": 0.8, "body_atr": 0.3,
        "close_location": 0.75, "vol_ratio": 1.5, "efficiency": 0.4,
        "range_low": 95.0, "range_high": 100.2, "open": 99.7,
        "lower_wick": 0.15, "body": 0.3, "pullback_touch_long": True,
        "breakout_retest_long": False, "recent_swing_low": 99.0,
    })
    assert _candidate(row, "LONG", 100.05) is None
