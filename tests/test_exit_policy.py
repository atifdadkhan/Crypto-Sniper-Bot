"""Deterministic exit-study tests. No network, exchange or Telegram access."""
from dataclasses import dataclass
import pandas as pd
import pytest
from backtesting.exit_policy import ExitPolicy, POLICIES, simulate_exit


@dataclass
class Sig:
    side: str = "LONG"
    stop_loss: float = 99.0


def bars(*ohlc):
    return pd.DataFrame([
        dict(timestamp=pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=15 * i),
             open=o, high=h, low=l, close=c)
        for i, (o, h, l, c) in enumerate(ohlc)
    ])


def test_predeclared_policies_are_unique_and_valid():
    assert len(POLICIES) == 8
    assert len({p.name for p in POLICIES}) == len(POLICIES)
    with pytest.raises(ValueError):
        ExitPolicy("INVALID", 3.0, 2.0)


def test_conservative_stop_first_and_cost():
    data = bars((100, 101, 100, 100), (100, 105, 98, 101))
    result = simulate_exit(Sig(), data, 1)
    assert result["exit_reason"] == "STOP_LOSS"
    assert result["gross_r"] == pytest.approx(-1.0)
    assert result["cost_r"] == pytest.approx(.12)
    assert result["net_r"] == pytest.approx(-1.12)


def test_gap_through_stop_is_worse_than_stop():
    data = bars((100, 100, 100, 100), (98, 99, 97, 98))
    assert simulate_exit(Sig(), data, 1) is None
    data = bars((100, 100, 100, 100), (100, 100, 99.5, 99.8),
                (97, 98, 96, 97.5))
    assert simulate_exit(Sig(), data, 1)["gross_r"] == pytest.approx(-3.0)


def test_profit_targets_and_partial_exit():
    data = bars((100, 100, 100, 100), (100, 102, 100, 101),
                (101, 103, 101, 102))
    result = simulate_exit(Sig(), data, 1)
    assert result["exit_reason"] == "TP2"
    assert result["gross_r"] == pytest.approx(2.0)


def test_breakeven_stop_only_after_tp1_and_no_retroactive_intrabar_fill():
    data = bars((100, 100, 100, 100), (100, 101.6, 100, 101.5),
                (101.5, 102, 98.9, 100.3))
    policy = ExitPolicy("BE", 1.5, 2.5, 96, True)
    result = simulate_exit(Sig(), data, 1, policy)
    assert result["exit_reason"] == "STOP_AFTER_TP1"
    assert result["gross_r"] == pytest.approx(.75)
    baseline = simulate_exit(Sig(), data, 1)
    assert baseline["gross_r"] == pytest.approx(.25)


def test_short_and_assumed_funding_are_costed():
    data = bars((100, 100, 100, 100), (100, 100, 97, 98),
                (98, 98, 97, 97))
    result = simulate_exit(Sig("SHORT", 101.0), data, 1,
                           funding_bps_per_8h=8)
    assert result["gross_r"] == pytest.approx(2.0)
    assert result["net_r"] < result["gross_r"]


def test_reject_invalid_cost_and_side():
    data = bars((100, 100, 100, 100))
    with pytest.raises(ValueError):
        simulate_exit(Sig(), data, 0, round_trip_cost_pct=-.01)
    with pytest.raises(ValueError):
        simulate_exit(Sig("BUY"), data, 0)
