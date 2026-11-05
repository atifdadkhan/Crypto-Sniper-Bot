"""Single coin analysis is read-only, fail-closed and uses existing engines."""
import asyncio
import importlib
from types import SimpleNamespace

import pytest

from single_coin import normalize_coin, signal_is_current, format_analysis


@pytest.mark.parametrize(("raw", "symbol"), [
    ("btc", "BTC/USDT:USDT"),
    ("BTCUSDT", "BTC/USDT:USDT"),
    ("sol/usdt", "SOL/USDT:USDT"),
    ("ETH/USDT:USDT", "ETH/USDT:USDT"),
    ("1000pepe", "1000PEPE/USDT:USDT"),
    ("btc usdt", None),
    ("BTC/USDC", None),
    ("DOGE/USDT:USDC", None),
    ("BTC;DROP", None),
    ("", None),
    ("X", None),
    ("A" * 80, None),
])
def test_parse_only_canonical_usdt_swaps(raw, symbol):
    assert normalize_coin(raw) == symbol


def fake_signal(strategy_id="CORE_15M", now=1_800_000_000):
    return SimpleNamespace(
        symbol="BTC/USDT:USDT", side="LONG", entry=100.0, stop_loss=98.0,
        tp1=103.0, tp2=105.0, setup="Pullback", score=8,
        strategy_id=strategy_id, candle_close_ms=int((now-60)*1000),
        ttl_seconds=3600 if strategy_id != "CORE_15M" else 900,
        counter_macro=False,
    )


def test_signal_has_same_close_timestamp_expiry_guard_as_alert_queue():
    now = 1_800_000_000
    s = fake_signal(now=now)
    assert signal_is_current(s, 900, now=now)
    s.candle_close_ms = int((now-901)*1000)
    assert not signal_is_current(s, 900, now=now)
    s.candle_close_ms = int((now+61)*1000)
    assert not signal_is_current(s, 900, now=now)
    s.candle_close_ms = 0
    assert not signal_is_current(s, 900, now=now)
    s = fake_signal("RS_4H", now=now)
    s.candle_close_ms = int((now-1800)*1000)
    assert signal_is_current(s, 900, now=now)


def test_report_reuses_engine_labels_and_only_valid_levels():
    now = 1_800_000_000
    btc, eth = {"trend": "BULLISH"}, {"trend": "NEUTRAL"}
    core, rs = fake_signal(now=now), fake_signal("RS_4H", now=now)
    out = format_analysis("BTC/USDT:USDT", btc, eth, [core, rs], {},
                          four_hour_eligible=True, now=now)
    assert "15m Trend Pullback" in out
    assert "4H Relative Strength Breakout" in out
    assert "8/10 (not a win probability)" in out
    assert "Target (3R)" in out
    assert "TP1:" in out and "TP2:" in out
    assert "not queue" in out and "No guaranteed profit" in out


def test_absent_setup_never_invents_entry_and_4h_outside_top20_is_skipped():
    out = format_analysis("OTHER/USDT:USDT", {"trend": "UNKNOWN"},
                          {"trend": "NEUTRAL"}, [], {},
                          four_hour_eligible=False)
    assert "WAIT — no qualifying setup" in out
    assert "outside live top-20" in out
    assert "Entry" not in out and "Stop Loss" not in out


def test_core_data_failure_does_not_hide_independent_4h_signal():
    now = 1_800_000_000
    out = format_analysis("BTC/USDT:USDT", {"trend": "BULLISH"},
                          {"trend": "BEARISH"}, [fake_signal("SLOW_4H", now=now)],
                          {"CORE_15M": "ValueError"}, four_hour_eligible=True,
                          now=now)
    assert "DATA UNAVAILABLE (ValueError)" in out
    assert "4H Slow Momentum Breakout" in out


def test_expired_setup_is_wait_not_trade_levels():
    now = 1_800_000_000
    s = fake_signal(now=now)
    s.candle_close_ms = int((now-1000)*1000)
    out = format_analysis("BTC/USDT:USDT", {}, {}, [s], {},
                          four_hour_eligible=False, now=now)
    assert "setup expired" in out
    assert "Entry (live quote)" not in out


def test_manual_run_uses_exact_existing_scanner_and_respects_4h_watch(monkeypatch):
    # Dummy Telegram environment ONLY: do not call Telegram or any exchange.
    monkeypatch.setenv("TELEGRAM_TOKEN", "1234:CI_DUMMY_NOT_REAL")
    monkeypatch.setenv("ADMIN_ID", "1234")
    app = importlib.import_module("main")
    monkeypatch.setattr(app, "settings", SimpleNamespace(
        top_n=0, min_quote_volume=500000, rs_engine_enabled=True,
        four_hour_top_n=2, signal_ttl_seconds=900, exchange_timeout_ms=20000,
    ))

    class Stub:
        markets = {"BTC/USDT:USDT": {}, "ETH/USDT:USDT": {}, "SOL/USDT:USDT": {}}
        exchange = object()
        async def top_symbols(self, top_n, min_quote_volume):
            assert (top_n, min_quote_volume) == (0, 500000)
            return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]

    monkeypatch.setattr(app, "client", Stub())

    async def macro(exchange, symbol):
        return {"trend": "NEUTRAL", "return_6_pct": 0,
                "return_7d_pct": 0, "candle_close_ms": 123}
    calls = []
    async def scanner(symbol, sem, btc, eth, core_enabled, four_hour_enabled):
        calls.append((symbol, core_enabled, four_hour_enabled))
        return (symbol, [], {})
    monkeypatch.setattr(app, "macro_snapshot", macro)
    monkeypatch.setattr(app, "_scan_one", scanner)
    result = asyncio.run(app.run_coin_analysis("SOL/USDT:USDT"))
    assert calls == [("SOL/USDT:USDT", True, False)]
    assert "outside live top-20" in result
    result = asyncio.run(app.run_coin_analysis("BTC/USDT:USDT"))
    assert calls[-1] == ("BTC/USDT:USDT", True, True)
    assert "WAIT" in result


def test_unlisted_market_fails_closed_without_scanning(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "1234:CI_DUMMY_NOT_REAL")
    monkeypatch.setenv("ADMIN_ID", "1234")
    app = importlib.import_module("main")
    monkeypatch.setattr(app, "settings", SimpleNamespace(
        top_n=0, min_quote_volume=500000, rs_engine_enabled=True,
        four_hour_top_n=20, signal_ttl_seconds=900, exchange_timeout_ms=20000,
    ))
    class Stub:
        markets = {"BTC/USDT:USDT": {}}
        async def top_symbols(self, *args):
            return ["BTC/USDT:USDT"]
    monkeypatch.setattr(app, "client", Stub())
    async def unexpected(*args, **kwargs):
        pytest.fail("Unlisted market must not reach engine evaluation")
    monkeypatch.setattr(app, "_scan_one", unexpected)
    text = asyncio.run(app.run_coin_analysis("XYZ/USDT:USDT"))
    assert "not eligible" in text
