from datetime import datetime, timezone

import pandas as pd
import strategy


def frames():
    frame = pd.DataFrame({
        "timestamp": [1_790_000_000_000 + i * 900_000 for i in range(12)],
        "close": [100.0 + i for i in range(12)],
        "ema21": [95.0] * 12,
        "ema50": [90.0] * 12,
        "ema21_prev3": [94.0] * 12,
        "adx": [27.0] * 12,
    })
    return {tf: frame.copy() for tf in ("15m", "1h", "4h", "1d")}


def mk_candidate(row, side, live_price, score=8):
    if side == "SHORT":
        return None
    return strategy.Signal(
        symbol="", side=side, entry=110.0, stop_loss=109.0,
        tp1=111.5, tp2=112.5, score=score, reasons=["test"],
        timestamp=datetime.now(timezone.utc).isoformat(),
        atr=1.0, risk_distance_pct=1.0, setup="Pullback",
    )


def test_live_counter_macro_needs_relative_strength(monkeypatch):
    monkeypatch.setattr(strategy, "enrich", lambda frame: frame.copy())
    monkeypatch.setattr(strategy, "_candidate", mk_candidate)
    inputs = frames()
    signal = strategy.evaluate(
        "ALT/USDT:USDT", inputs, "BEARISH", "BEARISH",
        current_price=111, btc_return_6_pct=-3.0, eth_return_6_pct=-2.0,
    )
    assert signal is not None
    assert signal.side == "LONG" and signal.counter_macro
    assert signal.candle_close_ms > 0
    assert strategy.evaluate(
        "ALT/USDT:USDT", inputs, "BEARISH", "BEARISH",
        current_price=111, btc_return_6_pct=None, eth_return_6_pct=-2.0,
    ) is None


def test_macro_score_bonus_counted_before_threshold(monkeypatch):
    monkeypatch.setattr(strategy, "enrich", lambda frame: frame.copy())
    monkeypatch.setattr(strategy, "_candidate",
                        lambda row, side, price: mk_candidate(row, side, price, score=9))
    assert strategy.evaluate(
        "ALT/USDT:USDT", frames(), "BULLISH", "BULLISH",
        min_score=10, current_price=111,
    ).score == 10
