import pandas as pd
from candle_intelligence import candle_patterns, quality_label


def test_bullish_engulfing():
    df = pd.DataFrame([
        {"open": 10, "close": 9, "high": 10.2, "low": 8.8},
        {"open": 8.9, "close": 10.1, "high": 10.3, "low": 8.7},
    ])
    assert "Bullish Engulfing" in candle_patterns(df)


def test_bearish_engulfing():
    df = pd.DataFrame([
        {"open": 9, "close": 10, "high": 10.2, "low": 8.8},
        {"open": 10.1, "close": 8.9, "high": 10.3, "low": 8.7},
    ])
    assert "Bearish Engulfing" in candle_patterns(df)


def test_insufficient_data():
    assert candle_patterns(pd.DataFrame()) == []


def test_quality_is_not_win_rate():
    assert quality_label(9) == "Strong"
    assert quality_label(7) == "Good"
    assert quality_label(5) == "Moderate"
