from types import SimpleNamespace

import pandas as pd

from backtesting.backtest import simulate, summarize


def _bars(rows):
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    return pd.DataFrame([
        {"timestamp": start + pd.Timedelta(minutes=15 * i), **row}
        for i, row in enumerate(rows)
    ])


def _signal(side="LONG", stop=98.0):
    return SimpleNamespace(
        symbol="BTCUSDT", side=side, stop_loss=stop, score=8, setup="Pullback",
        timestamp="2026-01-01T00:15:00+00:00",
    )


def test_entry_gap_beyond_stop_skips_trade():
    bars = _bars([{"open": 97.0, "high": 99.0, "low": 96.0, "close": 98.0}])
    assert simulate(_signal(), bars, 0) is None


def test_stop_gap_fills_at_worse_open():
    bars = _bars([
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        {"open": 96.0, "high": 97.0, "low": 95.0, "close": 96.0},
    ])
    trade = simulate(_signal(), bars, 0, fee_slippage_pct=0, max_hold=1)
    assert trade is not None
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["exit"] == 96.0
    assert trade["gross_r"] == -2.0


def test_same_bar_stop_and_target_assumes_stop_first():
    bars = _bars([
        {"open": 100.0, "high": 105.0, "low": 97.0, "close": 101.0},
    ])
    trade = simulate(_signal(), bars, 0, fee_slippage_pct=0)
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["gross_r"] == -1.0


def test_round_trip_cost_converted_to_r():
    bars = _bars([
        {"open": 100.0, "high": 103.0, "low": 99.0, "close": 102.0},
    ])
    trade = simulate(_signal(), bars, 0, fee_slippage_pct=0.12)
    assert trade["estimated_cost_pct"] == 0.12
    assert trade["estimated_cost_r"] > 0
    assert trade["net_r"] < trade["gross_r"]



def test_drawdown_includes_initial_equity_baseline():
    trades = pd.DataFrame({
        "net_r": [-1.0, 0.5],
        "tp1_hit": [False, True],
        "side": ["LONG", "SHORT"],
    })
    metrics = summarize(trades)
    assert metrics["max_drawdown_r"] == -1.0
