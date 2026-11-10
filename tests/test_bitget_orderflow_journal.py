"""Offline tests for the Bitget orderflow alert + closed-candle paper ledger."""
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from bitget_orderflow_journal import BitgetPaperJournal, english_alert, trade_plan, HOUR_MS
from orderflow_engines import Tape


def signal(side="LONG", condition="CAUTIOUS", timestamp=None):
    long = side == "LONG"
    return SimpleNamespace(
        symbol="BTC/USDT:USDT", engine_id="CVD_DIVERGENCE",
        strategy_name="CVD Divergence / Test", side=side,
        entry=100, stop_loss=98 if long else 102,
        tp1=102 if long else 98, tp2=104 if long else 96,
        timestamp=4 * HOUR_MS + HOUR_MS // 2 if timestamp is None else timestamp,
        reason="confirmed trade/book", market_condition=condition,
        closed_1h=((3 * HOUR_MS, 100, 101, 99, 100, 123),),
    )


def candles(*bars):
    """Each bar: hour, open, high, low, close; all are completed 1h bars."""
    return pd.DataFrame([
        {"timestamp": hour * HOUR_MS, "open": op, "high": high,
         "low": low, "close": close, "volume": 100}
        for hour, op, high, low, close in bars
    ])


def test_alert_cautious_tp1_only_and_safe_html():
    s = signal()
    s.symbol = "<BTC>&/USDT:USDT"
    body = english_alert(s, lambda x: str(x))
    assert "&lt;BTC&gt;&amp;" in body
    assert "Entry Price Range" in body
    assert "Stop Loss" in body
    assert "TP2: OFF" in body and "Moonshot: OFF" in body
    assert "TP1:" in body
    assert "<b>&lt;BTC&gt;&amp;</b>" in body
    assert "BUY / LONG" in body
    assert "No auto orders" in body
    assert trade_plan(s)["tp2"] is None


def test_strong_plan_two_targets_and_moonshot():
    p = trade_plan(signal(condition="STRONG"))
    assert p["tp1_fraction"] == 0.5 and p["tp2_fraction"] == 0.3
    assert p["moonshot_fraction"] == 0.2
    assert p["entry_low"] < p["entry"] < p["entry_high"]
    text = english_alert(signal(condition="STRONG"), str)
    assert "Moonshot" in text and "TP2:" in text


def test_plain_language_alert_is_bold_colorful_and_side_aware():
    body = english_alert(signal(side="SHORT", condition="STRONG"), str)
    assert "🚨 <b>BITGET SIGNAL</b>" in body
    assert "<b>BTC</b>" in body
    assert "🔴" in body and "SELL / SHORT" in body
    assert "Strong downward move supports this SELL" in body
    assert "Entry Price Range" in body and "Stop Loss" in body
    assert "Engine:" not in body
    assert "closed 1H trend" not in body
    assert "No auto orders" in body


def test_research_breakdown_groups_engine_coin_side_and_market(tmp_path):
    j = BitgetPaperJournal(tmp_path / "research.sqlite3")
    winner = signal(timestamp=11 * HOUR_MS + HOUR_MS // 2)
    winner.market_condition = "STRONG"
    j.record_delivered(winner)

    loser = signal(side="SHORT", timestamp=12 * HOUR_MS + HOUR_MS // 2)
    loser.engine_id = "WICK_HUNT"
    loser.symbol = "ETH/USDT:USDT"
    loser.market_condition = "CAUTIOUS"
    j.record_delivered(loser)

    with j.conn:
        j.conn.execute(
            "UPDATE paper_signals SET status='CLOSED', net_pct=1.25 WHERE engine_id='CVD_DIVERGENCE'"
        )
        j.conn.execute(
            "UPDATE paper_signals SET status='CLOSED', net_pct=-0.75 WHERE engine_id='WICK_HUNT'"
        )

    scorecard = {x["engine_id"]: x for x in j.performance_by_strategy()}
    assert scorecard["CVD_DIVERGENCE"]["wins"] == 1
    assert scorecard["CVD_DIVERGENCE"]["losses"] == 0
    assert scorecard["CVD_DIVERGENCE"]["avg_net_pct"] == pytest.approx(1.25)
    assert scorecard["WICK_HUNT"]["losses"] == 1

    groups = j.research_breakdown()
    assert any(
        x["engine_id"] == "CVD_DIVERGENCE" and x["symbol"] == "BTC/USDT:USDT"
        and x["side"] == "LONG" and x["condition"] == "STRONG" and x["wins"] == 1
        for x in groups
    )
    assert any(
        x["engine_id"] == "WICK_HUNT" and x["symbol"] == "ETH/USDT:USDT"
        and x["side"] == "SHORT" and x["condition"] == "CAUTIOUS" and x["losses"] == 1
        for x in groups
    )
    j.close()


def test_invalid_geometry_is_rejected():
    s = signal()
    s.tp1 = 97
    with pytest.raises(ValueError):
        trade_plan(s)


def test_cautious_long_win_and_deduplication(tmp_path):
    j = BitgetPaperJournal(tmp_path / "paper.sqlite3")
    s = signal()
    sid = j.record_delivered(s)
    assert j.record_delivered(s) == sid
    assert j.conn.execute("SELECT COUNT(*) FROM paper_signals").fetchone()[0] == 1
    assert j.conn.execute("SELECT COUNT(*) FROM paper_events").fetchone()[0] == 1
    stored = json.loads(j.conn.execute("SELECT data FROM paper_signals").fetchone()[0])
    assert stored["pre_signal_closed_1h"][0][0] == 3 * HOUR_MS
    assert j.open_symbols() == ["BTC/USDT:USDT"]
    j.advance(s.symbol, candles((5, 100, 103, 99, 102)))
    row = j.conn.execute("SELECT * FROM paper_signals").fetchone()
    assert row["status"] == "CLOSED" and row["tp1_hit"] == 1
    assert row["net_pct"] == pytest.approx(1.88)
    assert j.outcome_counts()["wins"] == 1
    j.close()


def test_short_win_and_loss_are_distinct(tmp_path):
    j = BitgetPaperJournal(tmp_path / "paper.sqlite3")
    s = signal(side="SHORT")
    j.record_delivered(s)
    j.advance(s.symbol, candles((5, 100, 101, 97, 98)))
    assert j.outcome_counts()["wins"] == 1

    s2 = signal(side="LONG", timestamp=7 * HOUR_MS + HOUR_MS // 2)
    j.record_delivered(s2)
    j.advance(s2.symbol, candles((8, 100, 106, 97, 101)))
    row = j.conn.execute("SELECT * FROM paper_signals WHERE signal_ms=?",
                         (s2.timestamp,)).fetchone()
    assert row["status"] == "CLOSED" and row["net_pct"] == pytest.approx(-2.12)
    assert row["tp1_hit"] == 0  # conservative stop-first ambiguity
    assert j.outcome_counts()["losses"] == 1
    j.close()


def test_strong_partial_tp1_tp2_then_moonshot_trail(tmp_path):
    j = BitgetPaperJournal(tmp_path / "paper.sqlite3")
    s = signal(condition="STRONG")
    j.record_delivered(s)
    j.advance(s.symbol, candles(
        (5, 100, 102.5, 99.5, 102),
        (6, 102.5, 104.5, 102.1, 104),
        (7, 104, 105, 101.9, 102),
    ))
    row = j.conn.execute("SELECT * FROM paper_signals").fetchone()
    assert row["status"] == "CLOSED"
    assert row["tp1_hit"] == 1 and row["tp2_hit"] == 1
    assert row["net_pct"] == pytest.approx(2.48)
    ev = [x[0] for x in j.conn.execute("SELECT event_type FROM paper_events")]
    assert "PAPER_TP1" in ev and "PAPER_TP2" in ev
    assert "PAPER_WIN" in ev
    j.close()


def test_missing_1h_bar_never_counts_as_profit(tmp_path):
    j = BitgetPaperJournal(tmp_path / "paper.sqlite3")
    s = signal()
    j.record_delivered(s)
    j.advance(s.symbol, candles((6, 100, 110, 99, 109)))
    row = j.conn.execute("SELECT * FROM paper_signals").fetchone()
    assert row["status"] == "UNVERIFIABLE_DATA_GAP"
    assert row["net_pct"] is None
    assert j.outcome_counts()["gaps"] == 1
    j.close()


def test_closed_1h_trend_attached_by_tape():
    t = Tape("BTC/USDT:USDT")
    frame = candles(*[
        (i, 100 + i * .1, 100.5 + i * .1, 99.5 + i * .1, 100 + i * .1)
        for i in range(40)
    ])
    t.set_swings(frame, now=1000)
    assert len(t.closed_1h) == 40
    assert t.trend_closes[-1] > t.trend_closes[-9]
    assert t.closed_1h[0][0] == 0
