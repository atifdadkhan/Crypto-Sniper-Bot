"""Offline integration checks for centralized Bitget futures live signal service."""
import asyncio
import json
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from bitget_orderflow_journal import BitgetPaperJournal
from central_bitget_strategies import (
    BITGET_LIVE_ENGINES, SPOT_STRATEGY_COUNT, attach_hour_context,
    closed_hour_context, live_registry, normalized_candle_signal, unified_candle_alert,
)
from delivery import AlertOutbox
from strategy import Signal


def closed_hours():
    now = int(time.time() // 3600 * 3_600_000)
    return pd.DataFrame([
        dict(timestamp=now - (40 - i) * 3_600_000,
             open=99 + i * .1, high=100 + i * .1, low=98 + i * .1,
             close=99 + i * .1, volume=1000)
        for i in range(40)
    ])


def valid_candle_signal():
    return Signal(
        symbol="BTC/USDT:USDT", side="LONG", entry=103, stop_loss=101,
        tp1=105, tp2=107, score=8,
        reasons=["Validated Bitget closed-candle setup"],
        timestamp="2026-11-11T00:00:00Z", atr=2, risk_distance_pct=1.95,
        setup="Pullback", candle_close_ms=int((time.time() - 2) * 1000),
        strategy_name="15m Trend Pullback",
    )


def test_all_bitget_engines_registered_separately_from_binance_spot():
    r = live_registry("both", True, True)
    assert set(r["live_engine_ids"]) == set(BITGET_LIVE_ENGINES)
    assert len(r["live_engine_ids"]) == 5
    assert r["market_type"] == "USDT perpetual futures"
    assert r["venue"] == "Bitget"
    assert r["execution"] == "SIGNALS_ONLY"
    assert r["spot_research_only"] == SPOT_STRATEGY_COUNT == 12
    assert live_registry("cvd", True, False)["live_engine_ids"] == [
        "CVD_DIVERGENCE", "CORE_15M"
    ]


def test_1h_context_and_unified_candle_format():
    s = valid_candle_signal()
    attach_hour_context(s, closed_hours())
    assert s.market_condition == "STRONG"
    assert len(s.closed_1h) == 40
    message = unified_candle_alert(s, str)
    assert "BITGET FUTURES" in message
    assert "15m Trend Pullback" in message
    assert "Entry Price Range" in message
    assert "TP1:" in message and "TP2:" in message
    assert "Moonshot" in message and "No auto orders" in message


def test_stale_or_missing_candle_context_remains_cautious():
    assert closed_hour_context(None, "SHORT") == ("CAUTIOUS", ())
    s = valid_candle_signal()
    attach_hour_context(s, closed_hours().tail(12))
    assert s.market_condition == "CAUTIOUS"
    assert len(s.closed_1h) == 0
    alert = unified_candle_alert(s, str)
    assert "TP2: OFF" in alert


def test_same_signal_id_across_delivery_retries(tmp_path):
    s = valid_candle_signal()
    attach_hour_context(s, closed_hours())
    record = s.to_dict()
    journal = BitgetPaperJournal(tmp_path / "central.sqlite3")
    a = normalized_candle_signal(record, int(time.time() * 1000), "CORE_15M|BTC|LONG|bar")
    b = normalized_candle_signal(record, int(time.time() * 1000) + 2500,
                                 "CORE_15M|BTC|LONG|bar")
    assert a.engine_id == "CORE_15M"
    assert journal.record_delivered(a) == journal.record_delivered(b)
    assert journal.conn.execute("SELECT COUNT(*) FROM paper_signals").fetchone()[0] == 1
    assert journal.conn.execute("SELECT COUNT(*) FROM paper_events").fetchone()[0] == 1
    data = json.loads(journal.conn.execute("SELECT data FROM paper_signals").fetchone()[0])
    assert len(data["pre_signal_closed_1h"]) == 40
    journal.close()


def test_4h_signal_is_single_original_target_not_invented_multiple_targets():
    record = {
        "symbol": "ETH/USDT:USDT", "strategy_id": "RS_4H",
        "strategy_name": "4H Relative Strength Breakout",
        "side": "SHORT", "entry": 100, "stop_loss": 103,
        "tp1": 91, "tp2": 91, "reasons": ["4h confirmed"],
    }
    s = normalized_candle_signal(record, int(time.time() * 1000))
    alert = __import__("bitget_orderflow_journal").english_alert(s, str)
    assert "4H Relative Strength Breakout" in alert
    assert "TP1:</b> <code>91" in alert and "TP2: OFF" in alert
    record["market_condition"] = "STRONG"
    strong_single = normalized_candle_signal(record, int(time.time() * 1000))
    single = __import__("bitget_orderflow_journal").trade_plan(strong_single)
    assert single["condition"] == "STRONG"
    assert not single["moonshot"] and single["tp1_fraction"] == 1.0
    assert single["tp2"] is None


def test_telegram_confirmed_candle_delivery_writes_shared_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_TOKEN", "1234:CI_DUMMY_NOT_REAL")
    monkeypatch.setenv("ADMIN_ID", "1234")
    import main as app

    monkeypatch.setattr(app, "UNIFIED_LIVE", True)
    monkeypatch.setattr(app, "history", [])
    monkeypatch.setattr(app, "data_dir", tmp_path)
    monkeypatch.setattr(app, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(app, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(app, "bitget_paper_journal", None)
    monkeypatch.setattr(app, "state", {"cooldowns": {}, "scan_cursor": 0})
    monkeypatch.setattr(app, "delivery_lock", asyncio.Lock())
    box = AlertOutbox(tmp_path / "telegram_outbox.json")
    monkeypatch.setattr(app, "outbox", box)

    s = valid_candle_signal()
    attach_hour_context(s, closed_hours())
    assert box.add(s, unified_candle_alert(s, str))

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    bot = FakeBot()
    assert asyncio.run(app._deliver_pending(bot, 1234)) == 1
    assert len(bot.sent) == 1
    assert "Entry Price Range" in bot.sent[0]["text"]
    assert len(app.history) == 1
    assert app.history[0]["engine_id"] == "CORE_15M"
    assert app.history[0]["paper_signal_id"]
    assert app.history[0]["delivery_id"]
    assert len(box.items) == 0
    assert (tmp_path / "history.json").is_file()

    j = app.bitget_paper_journal
    assert j.outcome_counts()["open"] == 1
    assert len(json.loads(j.conn.execute("SELECT data FROM paper_signals").fetchone()[0])
               ["pre_signal_closed_1h"]) == 40
    j.close()
    monkeypatch.setattr(app, "bitget_paper_journal", None)


def test_unified_startup_has_stream_and_single_scheduler(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "1234:CI_DUMMY_NOT_REAL")
    monkeypatch.setenv("ADMIN_ID", "1234")
    import main as app
    monkeypatch.setattr(app, "UNIFIED_LIVE", True)
    monkeypatch.setattr(app, "ORDERFLOW_ONLY", True)

    jobs = []

    class FakeJobs:
        def run_repeating(self, callback, **kwargs):
            jobs.append(kwargs["name"])
        def run_once(self, callback, **kwargs):
            jobs.append(kwargs["name"])

    class FakeBot:
        async def set_my_commands(self, commands):
            return

    async def fake_initialize():
        app.client.markets = {"BTC/USDT:USDT": {"active": True}}

    async def fake_close():
        return

    async def idle_orderflow(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(app.client, "initialize", fake_initialize)
    monkeypatch.setattr(app.client, "close", fake_close)
    monkeypatch.setattr(app, "run_orderflow", idle_orderflow)

    async def check():
        application = SimpleNamespace(bot=FakeBot(), job_queue=FakeJobs())
        await app.post_init(application)
        assert app.orderflow_task is not None
        assert not app.orderflow_task.done()
        await app.post_shutdown(application)

    asyncio.run(check())
    assert "startup_scan" in jobs and "market_scan" in jobs
    assert "bitget_paper_outcome_refresh" in jobs
    assert jobs.count("retry_telegram_alerts") == 1
