"""Deterministic 4H trend-continuation risk, data and signal tests."""
import asyncio
from types import SimpleNamespace

import pandas as pd
import pytest

from formatting import signal_text
from slow_momentum import (
    STRATEGY_ID, evaluate_slow_momentum, precheck_slow_momentum,
)
from relative_strength import TF_MS


def synthetic(direction=1, n=350):
    start = pd.Timestamp("2026-01-01T00:00:00Z").value // 1_000_000
    data = []
    for i in range(n):
        mid = 150 + direction * .075 * i
        if i == n - 1:
            mid += direction * 4
        data.append([start + i * TF_MS, mid - direction * .02,
                     mid + .15, mid - .15, mid, 100.0])
    return pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])


@pytest.mark.parametrize("direction,side", [(1,"LONG"),(-1,"SHORT")])
def test_slow_momentum_closed_breakout(direction, side):
    d = synthetic(direction)
    assert precheck_slow_momentum(d.tail(140))
    sig = evaluate_slow_momentum("TEST/USDT:USDT",d,float(d.close.iloc[-1]))
    assert sig is not None
    assert sig.strategy_id == STRATEGY_ID
    assert sig.side == side
    assert sig.score == 0  # No misleading claim of 9/10 win probability.
    assert sig.ttl_seconds == 3600
    assert sig.hold_review_days == 10
    risk = abs(sig.entry - sig.stop_loss)
    assert risk == pytest.approx(2.5 * sig.atr)
    assert abs(sig.tp2 - sig.entry) == pytest.approx(3 * risk)
    assert "4H Slow Momentum Breakout" in signal_text(sig)
    assert "Target (3R)" in signal_text(sig)


def test_no_breakout_or_weak_volume_means_no_slow_signal():
    d = synthetic()
    d.loc[len(d)-1, "close"] = d.close.iloc[-2]
    d.loc[len(d)-1, "open"] = d.open.iloc[-2]
    d.loc[len(d)-1, "high"] = d.high.iloc[-2]
    d.loc[len(d)-1, "low"] = d.low.iloc[-2]
    assert not precheck_slow_momentum(d)
    d = synthetic()
    d.loc[len(d)-1, "volume"] = 1.0
    assert not precheck_slow_momentum(d)


def test_missing_history_and_gap_fail_closed():
    d=synthetic()
    with pytest.raises(ValueError,match="DATA INSUFFICIENT"):
        evaluate_slow_momentum("TEST", d.tail(100), 175)
    with pytest.raises(ValueError,match="contiguous"):
        evaluate_slow_momentum("TEST",d.drop(index=100),175)


def test_live_price_anti_chase_and_empty_quote():
    d = synthetic()
    assert evaluate_slow_momentum("TEST",d,10_000) is None
    assert evaluate_slow_momentum("TEST",d,None) is None


def test_market_scan_slow_momentum_fallback_only(monkeypatch):
    import importlib
    monkeypatch.setenv("TELEGRAM_TOKEN","123456:CI_DUMMY_NOT_REAL")
    monkeypatch.setenv("ADMIN_ID","123456")
    app=importlib.import_module("main")
    monkeypatch.setattr(app,"settings",SimpleNamespace(min_score=7,
        exchange_timeout_ms=20000,rs_engine_enabled=True,rs_signal_ttl_seconds=3600))
    core, slow=object(),object()
    class FakeClient:
        async def multi_timeframe(self, symbol, semaphore):
            return {"4h": object()}
        async def live_price(self,symbol):
            return 100.
        async def extended_four_hour(self,symbol,recent):
            return object()
    monkeypatch.setattr(app,"client",FakeClient())
    monkeypatch.setattr(app,"evaluate",lambda *a,**kw:core)
    monkeypatch.setattr(app,"precheck_recent_breakout",lambda *a,**kw:False)
    monkeypatch.setattr(app,"precheck_slow_momentum",lambda *a,**kw:True)
    monkeypatch.setattr(app,"evaluate_slow_momentum",lambda *a,**kw:slow)
    benchmark={"trend":"UNKNOWN","return_6_pct":None,
               "return_7d_pct":None,"candle_close_ms":None}
    result=asyncio.run(app._scan_one("TEST",asyncio.Semaphore(1),benchmark,benchmark))
    assert result[1] == [core,slow] and not result[2]


def test_slow_vs_rs_precedence(monkeypatch):
    import importlib
    monkeypatch.setenv("TELEGRAM_TOKEN","123456:CI_DUMMY_NOT_REAL")
    monkeypatch.setenv("ADMIN_ID","123456")
    app=importlib.import_module("main")
    monkeypatch.setattr(app,"settings",SimpleNamespace(min_score=7,
        exchange_timeout_ms=20000,rs_engine_enabled=True,rs_signal_ttl_seconds=3600))
    core,rs=object(),object()
    class FakeClient:
        async def multi_timeframe(self,symbol,semaphore):
            return {"4h":object()}
        async def live_price(self,symbol):
            return 100.
        async def extended_four_hour(self,symbol,recent):
            return object()
    monkeypatch.setattr(app,"client",FakeClient())
    monkeypatch.setattr(app,"evaluate",lambda *a,**kw:core)
    monkeypatch.setattr(app,"precheck_recent_breakout",lambda *a,**kw:True)
    monkeypatch.setattr(app,"precheck_slow_momentum",lambda *a,**kw:True)
    monkeypatch.setattr(app,"evaluate_relative_strength",lambda *a,**kw:rs)
    monkeypatch.setattr(app,"evaluate_slow_momentum",
                        lambda *a,**kw:pytest.fail("Secondary signal must not duplicate primary"))
    b={"trend":"BULLISH","return_6_pct":1.0,"return_7d_pct":1.0,"candle_close_ms":123}
    output=asyncio.run(app._scan_one("TEST",asyncio.Semaphore(1),b,b))
    assert output[1]==[core,rs]
