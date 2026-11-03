import pandas as pd

from backtesting.full_market_runner import derive_complete


def _frame(start, periods, freq):
    stamps = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    values = [100.0 + i for i in range(periods)]
    return pd.DataFrame({
        "timestamp": stamps,
        "open": values,
        "high": [v + 2 for v in values],
        "low": [v - 2 for v in values],
        "close": [v + 1 for v in values],
        "volume": [10.0] * periods,
    })


def test_30m_derivation_keeps_only_complete_15m_buckets():
    source = _frame("2026-01-01T00:00:00Z", 5, "15min")
    result = derive_complete(source, "30min", 2)
    assert len(result) == 2
    assert result.iloc[0].timestamp == pd.Timestamp("2026-01-01T00:00:00Z")
    assert result.iloc[1].timestamp == pd.Timestamp("2026-01-01T00:30:00Z")
    assert result.iloc[0].open == 100.0
    assert result.iloc[0].close == 102.0
    assert result.iloc[0].volume == 20.0


def test_2h_derivation_does_not_fill_missing_source_candles():
    source = _frame("2026-01-01T00:00:00Z", 4, "1h")
    source = source.drop(index=1).reset_index(drop=True)
    result = derive_complete(source, "2h", 2)
    assert len(result) == 1
    assert result.iloc[0].timestamp == pd.Timestamp("2026-01-01T02:00:00Z")


def test_2h_derivation_aggregates_only_full_buckets():
    source = _frame("2026-01-01T00:00:00Z", 4, "1h")
    result = derive_complete(source, "2h", 2)
    assert len(result) == 2
    assert result.iloc[0].high == 103.0
    assert result.iloc[0].low == 98.0
