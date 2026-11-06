from types import SimpleNamespace

from independent_engines import EngineEvidence
from strategy import _independent_candidate


def row():
    return SimpleNamespace(
        close=110.0, atr=2.0, adx=25.0, vol_ratio=1.8,
        range=1.5, body_atr=0.50, efficiency=0.30,
        rsi=58.0, ema21_extension=0.40, ema9=109.0,
        ema21=107.0, ema50=100.0,
        recent_swing_low=107.0, recent_swing_high=113.0,
    )


def evidence(name):
    return EngineEvidence(name, "LONG", True, "closed candle confirmed")


def test_breakout_can_generate_signal_without_legacy_pullback():
    signal = _independent_candidate(
        row(), "LONG", 110.2, [evidence("Breakout"),
                              evidence("Volume Pressure")]
    )
    assert signal is not None
    assert signal.setup == "Breakout"
    assert signal.entry == 110.2
    assert signal.stop_loss < signal.entry < signal.tp1 < signal.tp2
    assert signal.score >= 7


def test_volume_only_does_not_create_signal():
    assert _independent_candidate(
        row(), "LONG", 110.2, [evidence("Volume Pressure")]
    ) is None


def test_chased_quote_is_not_actionable():
    assert _independent_candidate(
        row(), "LONG", 112.0, [evidence("Breakout")]
    ) is None
