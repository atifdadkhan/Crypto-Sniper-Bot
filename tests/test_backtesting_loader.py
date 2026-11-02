import pandas as pd
from backtesting.data_loader import normalise_ohlcv

def test_normalise_unix_milliseconds():
    base = 1760000000000
    frame = pd.DataFrame({"Timestamp": [base + i * 900000 for i in range(100)],
                          "Open": [100.0] * 100, "High": [102.0] * 100,
                          "Low": [99.0] * 100, "Close": [101.0] * 100,
                          "Volume": [10.0] * 100})
    out = normalise_ohlcv(frame)
    assert len(out) == 100
    assert str(out.timestamp.dt.tz) == "UTC"
    assert list(out.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
