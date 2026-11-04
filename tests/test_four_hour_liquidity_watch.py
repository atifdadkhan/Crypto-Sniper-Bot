"""Verify full-universe 15m scan survives liquid 4H always-on watch."""
import asyncio
from types import SimpleNamespace


def test_repeating_liquid_watch_never_changes_original_core_batch(monkeypatch):
    import importlib
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:CI_DUMMY_NOT_REAL")
    monkeypatch.setenv("ADMIN_ID", "123456")
    main = importlib.import_module("main")
    assigned = main.scan_assignments(["C", "D"], ["A", "B"])
    assert assigned == [
        ("C", True, False), ("D", True, False),
        ("A", False, True), ("B", False, True),
    ]
    assert main.scan_assignments(["A", "C"], ["A", "B"]) == [
        ("A", True, True), ("C", True, False), ("B", False, True)
    ]
    assert main.scan_assignments(["A", "A"], ["A", "A"]) == [
        ("A", True, True)
    ]
    assert main.scan_assignments(["C"], []) == [("C", True, False)]


def test_four_hour_watch_only_does_not_fetch_15m_or_evaluate_original(monkeypatch):
    import importlib
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:CI_DUMMY_NOT_REAL")
    monkeypatch.setenv("ADMIN_ID", "123456")
    main = importlib.import_module("main")
    monkeypatch.setattr(main, "settings", SimpleNamespace(
        rs_engine_enabled=True, rs_signal_ttl_seconds=3600,
        exchange_timeout_ms=20000, min_score=7,
    ))
    class FakeClient:
        def __init__(self):
            self.four_hour_requests = 0
        async def multi_timeframe(self, *_args):
            raise AssertionError("Original 15m fetch must NOT run for 4H-only job")
        async def candles(self, symbol, timeframe):
            assert timeframe == "4h"
            self.four_hour_requests += 1
            return object()
        async def live_price(self, symbol):
            return 100.
        async def extended_four_hour(self, symbol, recent):
            return object()
    client=FakeClient()
    monkeypatch.setattr(main,"client",client)
    monkeypatch.setattr(main,"evaluate",
                        lambda *a,**kw: (_ for _ in ()).throw(
                            AssertionError("Original 15m strategy must not be evaluated")))
    monkeypatch.setattr(main,"precheck_recent_breakout",lambda *a,**kw:False)
    monkeypatch.setattr(main,"precheck_slow_momentum",lambda *a,**kw:True)
    slow=object()
    monkeypatch.setattr(main,"evaluate_slow_momentum",lambda *a,**kw:slow)
    benchmark={"trend":"UNKNOWN","return_6_pct":None,
               "return_7d_pct":None,"candle_close_ms":None}
    result=asyncio.run(main._scan_one(
        "TOP/USDT:USDT", asyncio.Semaphore(1), benchmark, benchmark,
        core_enabled=False, four_hour_enabled=True
    ))
    assert result[1] == [slow]
    assert client.four_hour_requests == 1


def test_original_core_checks_are_unmodified_when_outside_4h_watch(monkeypatch):
    import importlib
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:CI_DUMMY_NOT_REAL")
    monkeypatch.setenv("ADMIN_ID", "123456")
    main = importlib.import_module("main")
    monkeypatch.setattr(main, "settings", SimpleNamespace(
        rs_engine_enabled=True, exchange_timeout_ms=20000,
        min_score=7, rs_signal_ttl_seconds=3600,
    ))
    class Fake:
        async def multi_timeframe(self, symbol, sem):
            return {"4h": object()}
        async def live_price(self, symbol):
            return 100.
        async def candles(self, *_):
            raise AssertionError("4H-only extra data fetch must not run")
    monkeypatch.setattr(main,"client",Fake())
    core=object()
    monkeypatch.setattr(main,"evaluate",lambda *a,**kw:core)
    monkeypatch.setattr(main,"precheck_slow_momentum",
                        lambda *a,**kw: (_ for _ in ()).throw(
                            AssertionError("No 4H setup evaluation for outside watch")))
    b={"trend":"BULLISH","return_6_pct":1.0,
       "return_7d_pct":None,"candle_close_ms":None}
    result=asyncio.run(main._scan_one(
        "NON_TOP/USDT:USDT",asyncio.Semaphore(1),b,b,
        core_enabled=True,four_hour_enabled=False))
    assert result[1]==[core]
    assert not result[2]
