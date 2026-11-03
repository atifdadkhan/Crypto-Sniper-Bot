import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from backtesting.multi_strategy import (
    STRATEGIES, enrich_for_engines, contiguous_segments, metrics, _signal
)


def make_frame(n=180):
    close = 100 + np.arange(n) * 0.08 + np.sin(np.arange(n) / 4) * 0.2
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC"),
        "open": close - 0.03,
        "high": close + 0.15,
        "low": close - 0.15,
        "close": close,
        "volume": np.full(n, 1000.0),
    })


def test_all_five_strategy_engines_are_registered():
    assert STRATEGIES == (
        "trend_pullback", "breakout_retest", "momentum_continuation",
        "range_reversal", "volatility_expansion"
    )


def test_engine_features_include_macd_and_shared_indicators():
    enriched = enrich_for_engines(make_frame())
    assert {"macd_line", "macd_signal", "atr", "adx", "rsi",
            "breakout_retest_long", "breakout_retest_short"} <= set(enriched.columns)
    assert np.isfinite(enriched.iloc[-1].macd_line)


def test_signal_risk_bounds_reject_invalid_stop():
    row = pd.Series({"close": 100.0, "atr": 1.0})
    assert _signal(row, "LONG", "test", 100.0) is None
    assert _signal(row, "LONG", "test", 97.0) is None  # risk > 2 ATR


def test_metrics_calculate_win_rate_after_costs():
    trades = pd.DataFrame({"net_r": [1.0, -0.5, 0.25, -0.25],
                           "side": ["LONG", "SHORT", "LONG", "SHORT"]})
    result = metrics(trades)
    assert result["trades"] == 4
    assert result["win_rate_pct"] == 50.0
    assert result["net_r"] == 0.5
    assert result["long_trades"] == 2 and result["short_trades"] == 2


def test_gap_segmentation_does_not_stitch_missing_candles():
    frame = make_frame(5)
    frame.loc[3:, "timestamp"] += pd.Timedelta(minutes=15)
    parts = contiguous_segments(frame, "15m")
    assert [len(part) for part in parts] == [3, 2]
