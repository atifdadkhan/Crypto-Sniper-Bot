"""Deterministic smoke tests for closed-bar, manual-signal style filters."""
import pandas as pd
import pytest

from strategy_modes import MODE_LABELS, select_mode, valid_mode


def candles():
    count = 90
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-10-01", periods=count, freq="15min", tz="Asia/Karachi"),
        "open": [100.0] * count,
        "high": [101.0] * count,
        "low": [99.0] * count,
        "close": [100.0] * count,
        "volume": [10000.0] * count,
    })
    return df


def test_breakout_needs_price_and_volume():
    df = candles()
    assert select_mode(df, "LONG", "breakout", "crypto") is None
    df.loc[df.index[-1], ["open", "high", "low", "close", "volume"]] = [100.0, 104.0, 99.0, 103.0, 30000.0]
    assert select_mode(df, "LONG", "breakout", "crypto") == ("breakout", MODE_LABELS["breakout"])
    assert select_mode(df, "LONG", "recommended", "psx") == ("breakout", MODE_LABELS["breakout"])


def test_fail_closed_for_bad_bars_and_unknown_side():
    df = candles()
    df.loc[df.index[-1], "close"] = -1.0
    assert select_mode(df) is None
    assert select_mode(candles(), "SIDEWAYS") is None
    assert select_mode(candles(), "SHORT", market="psx") is None


def test_recommended_is_safe_default():
    assert valid_mode("unsupported") == "recommended"
    assert valid_mode("rsi_macd") == "rsi_macd"
    assert len(MODE_LABELS) == 4


def test_volume_spike_alone_is_not_a_breakout():
    df = candles()
    df.loc[df.index[-1], "volume"] = 50000.0
    assert select_mode(df, "LONG", "breakout", "crypto") is None
