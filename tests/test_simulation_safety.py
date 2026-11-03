import pandas as pd
from types import SimpleNamespace

from backtesting.backtest import simulate


def _bars(count=3):
    stamps = pd.date_range("2026-01-01T00:00:00Z", periods=count, freq="15min")
    return pd.DataFrame({
        "timestamp": stamps,
        "open": [100.0] * count,
        "high": [101.0] * count,
        "low": [99.0] * count,
        "close": [100.5] * count,
        "volume": [1000.0] * count,
    })


def test_max_hold_counts_entry_bar_as_first_held_bar():
    signal = SimpleNamespace(
        symbol="BTCUSDT", side="LONG", stop_loss=98.0,
        score=7, setup="Pullback", timestamp="2026-01-01T00:15:00+00:00",
    )
    bars = _bars()
    trade = simulate(signal, bars, entry_i=1, max_hold=1, fee_slippage_pct=0.0)
    assert trade is not None
    assert trade["exit_reason"] == "TIME_EXIT"
    assert trade["exit_time"] == str(bars.iloc[1].timestamp)


def test_max_hold_two_includes_exactly_two_bars():
    signal = SimpleNamespace(
        symbol="BTCUSDT", side="LONG", stop_loss=90.0,
        score=7, setup="Pullback", timestamp="2026-01-01T00:15:00+00:00",
    )
    bars = _bars()
    trade = simulate(signal, bars, entry_i=1, max_hold=2, fee_slippage_pct=0.0)
    assert trade is not None
    assert trade["exit_reason"] == "TIME_EXIT"
    assert trade["exit_time"] == str(bars.iloc[2].timestamp)
