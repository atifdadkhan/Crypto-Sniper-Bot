from types import SimpleNamespace

from backtesting.strategy_chemistry_runner import combine_candidates


def test_pair_combination_requires_both_named_engines():
    row = SimpleNamespace(close=100.0, atr=2.0)
    candidates = {"trend_pullback": SimpleNamespace(stop_loss=98.5)}
    assert combine_candidates(
        row, "LONG", "pair", candidates,
        required=("trend_pullback", "momentum_continuation"), min_votes=2,
    ) is None


def test_pair_combination_uses_median_stop_and_validates_risk():
    row = SimpleNamespace(close=100.0, atr=2.0)
    candidates = {
        "trend_pullback": SimpleNamespace(stop_loss=98.4),
        "momentum_continuation": SimpleNamespace(stop_loss=98.8),
    }
    result = combine_candidates(
        row, "LONG", "pair", candidates,
        required=("trend_pullback", "momentum_continuation"), min_votes=2,
    )
    assert result is not None
    assert result.side == "LONG"
    assert result.stop_loss == 98.6
    assert result.setup == "pair"


def test_consensus_requires_minimum_independent_votes():
    row = SimpleNamespace(close=100.0, atr=2.0)
    candidates = {
        "trend_pullback": SimpleNamespace(stop_loss=98.5),
        "momentum_continuation": SimpleNamespace(stop_loss=98.7),
    }
    assert combine_candidates(
        row, "LONG", "three_votes", candidates, required=(), min_votes=3,
    ) is None
