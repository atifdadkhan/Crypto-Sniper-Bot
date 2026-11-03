import numpy as np
import pandas as pd
import pytest

from backtesting.data_loader import normalise_ohlcv, validate_timeframe


def _frame(n=100, start=1760000400000, step=900000):
    stamps = [start + i * step for i in range(n)]
    return pd.DataFrame({
        "timestamp": stamps, "open": [100.0] * n, "high": [102.0] * n,
        "low": [99.0] * n, "close": [101.0] * n, "volume": [10.0] * n,
    })


def test_normalise_unix_milliseconds():
    out = normalise_ohlcv(_frame())
    assert len(out) == 100
    assert str(out.timestamp.dt.tz) == "UTC"
    assert list(out.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_rejects_non_finite_and_invalid_ohlc():
    frame = _frame()
    frame.loc[0, "high"] = np.inf
    frame.loc[1, "low"] = 105.0
    out = normalise_ohlcv(frame, min_candles=1)
    assert len(out) == 98


def test_timeframe_validation_accepts_contiguous_utc_candles():
    out = normalise_ohlcv(_frame(start=1760000400000))
    validate_timeframe(out, "15m", "TESTUSDT")


def test_timeframe_validation_rejects_gap():
    frame = _frame()
    frame.loc[51:, "timestamp"] += 900000
    out = normalise_ohlcv(frame)
    with pytest.raises(ValueError, match="timestamp gap"):
        validate_timeframe(out, "15m", "TESTUSDT")


def test_timeframe_validation_rejects_misaligned_open_time():
    frame = _frame(start=1760000400001)
    out = normalise_ohlcv(frame)
    with pytest.raises(ValueError, match="off the UTC timeframe boundary"):
        validate_timeframe(out, "15m", "TESTUSDT")
