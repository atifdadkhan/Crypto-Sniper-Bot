"""Validation for the read-only exit research runner."""
import pandas as pd
import pytest

from backtesting.exit_policy import POLICIES
from backtesting.exit_study import _utc, metrics, run_study


def test_research_is_predeclared_and_not_a_live_trading_engine():
    assert len(POLICIES) == 8
    assert POLICIES[0].name == "BASELINE"
    assert POLICIES[0].tp1_r == 1.5
    assert POLICIES[0].tp2_r == 2.5


def test_metrics_are_net_of_cost_r():
    rows = [{"net_r": 1.0}, {"net_r": -0.5}, {"net_r": 0.5}]
    result = metrics(rows)
    assert result["trades"] == 3
    assert result["profit_factor"] == 3.0
    assert result["net_r"] == 1.0
    assert result["max_drawdown_r"] == -0.5


def test_utc_date_inputs_are_consistent():
    assert _utc("2026-07-01").tzinfo is not None
    assert _utc("2026-07-01T05:00:00+05:00") == pd.Timestamp("2026-07-01T00:00:00Z")


def test_invalid_date_order_rejected_without_network(tmp_path):
    with pytest.raises(ValueError, match="Date order"):
        run_study(data_dir=tmp_path, out_dir=tmp_path / "out",
                  test_start="2026-11-01", train_end="2026-07-01")


def test_missing_btc_eth_rejected_without_fake_data(tmp_path):
    with pytest.raises(ValueError, match="DATA INSUFFICIENT"):
        run_study(data_dir=tmp_path, out_dir=tmp_path / "out")
