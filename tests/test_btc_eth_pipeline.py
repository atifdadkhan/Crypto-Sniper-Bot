import pandas as pd
import pytest

from backtesting.download_btc_eth_independent import history_tiers
from backtesting.backtest_btc_eth_timeframes import (
    split_contiguous, benchmark_trend_at, prepare_benchmark,
)


def test_history_tiers_descend_to_minimum_without_duplicates():
    assert history_tiers(365, 120) == [365, 273, 182, 120]
    assert history_tiers(120, 120) == [120]


def _ohlcv(count=110, start="2026-01-01T00:00:00Z", freq="15min"):
    ts = pd.date_range(start, periods=count, freq=freq, tz="UTC")
    close = pd.Series(range(100, 100 + count), dtype=float)
    return pd.DataFrame({
        "timestamp": ts, "open": close - 0.2, "high": close + 1,
        "low": close - 1, "close": close, "volume": 1000.0,
    })


def test_split_contiguous_keeps_gaps_as_separate_segments():
    frame = _ohlcv(110)
    frame = pd.concat([frame.iloc[:105], frame.iloc[106:]], ignore_index=True)
    parts = split_contiguous(frame, "15m")
    assert [len(part) for part in parts] == [105, 4]


def test_split_contiguous_rejects_off_grid_timestamps():
    frame = _ohlcv(105)
    frame.loc[10, "timestamp"] += pd.Timedelta(minutes=1)
    with pytest.raises(ValueError, match="aligned"):
        split_contiguous(frame, "15m")


def test_benchmark_alignment_uses_completed_candle_time_and_rejects_stale():
    frame = _ohlcv(110)
    prepared = prepare_benchmark(frame, "15m")
    last_close = int((frame.timestamp.iloc[-1] + pd.Timedelta(minutes=15)).value)
    assert benchmark_trend_at(prepared, last_close, 900_000) in {
        "BULLISH", "BEARISH", "NEUTRAL"
    }
    assert benchmark_trend_at(prepared, last_close + 1_800_000, 900_000) is None


def test_short_benchmark_history_is_not_silently_used():
    frame = _ohlcv(99)
    assert prepare_benchmark(frame, "15m") == []
