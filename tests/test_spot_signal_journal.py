"""English output and immutable paper-signal journal safeguards."""
import pandas as pd

from spot_signal_journal import (
    SignalJournal, build_buy_plan, format_signal, market_condition,
)


def history(n=80):
    timestamps = pd.date_range("2026-09-01", periods=n, freq="15min", tz="UTC")
    prices = [100+i*0.1 for i in range(n)]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": prices, "high": [x+0.2 for x in prices],
        "low": [x-0.2 for x in prices], "close": prices,
        "volume": [1000]*(n-1)+[3000],
    })


def sample_signal(frame):
    return {
        "symbol": "BTCUSDT", "timeframe": "15m",
        "strategy": "keltner_trend",
        "strategy_name": "Keltner Channel Trend",
        "action": "BUY", "price": float(frame.close.iloc[-1]),
        "signal_candle_utc": str(frame.timestamp.iloc[-1]),
        "candle_close_utc": str(frame.timestamp.iloc[-1]+pd.Timedelta(minutes=15)),
        "trade_plan": build_buy_plan(frame),
    }


def test_strong_market_gives_tp1_tp2_and_moonshot():
    frame = history()
    plan = build_buy_plan(frame)
    assert market_condition(frame) == "STRONG"
    assert plan["tp2"] > plan["tp1"] > plan["entry_price_high"]
    card = format_signal(sample_signal(frame))
    assert all(x in card for x in (
        "BUY", "Strategy", "Entry Price Range", "TP1", "TP2", "Moonshot", "Stop Loss"))


def test_cautious_market_only_tp1():
    frame = history()
    frame.loc[len(frame)-1, "volume"] = 1
    plan = build_buy_plan(frame)
    assert plan["plan_mode"] == "TP1_ONLY"
    assert plan["tp2"] is None
    assert "Disabled (TP1 only)" in format_signal(sample_signal(frame))


def test_sqlite_idempotence_candle_snapshots_and_stop_first(tmp_path):
    frame = history()
    frame.loc[len(frame)-1, "volume"] = 1
    signal = sample_signal(frame)
    journal = SignalJournal(tmp_path/"journal.sqlite3")
    sid = journal.record(signal, frame)
    assert sid == journal.record(signal, frame)
    assert journal.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
    plan = signal["trade_plan"]
    next_bar = pd.DataFrame([{
        "timestamp": frame.timestamp.iloc[-1]+pd.Timedelta(minutes=15),
        "open": plan["entry_price_high"],
        "high": plan["tp1"]+0.3,
        "low": plan["stop_loss"]-0.1,
        "close": plan["stop_loss"], "volume": 1000,
    }])
    full = pd.concat([frame, next_bar], ignore_index=True)
    journal.advance("BTCUSDT", "15m", full)
    paper = journal.export(tmp_path/"out.csv")
    assert paper[0]["status"] == "CLOSED"
    assert paper[0]["outcome"] == "LOSS"
    assert paper[0]["exit_reason"] == "STOP_LOSS_OR_TRAIL"
    assert paper[0]["outcome_type"] == "PAPER_ESTIMATE_NOT_ACTUAL_FILL"
    assert journal.conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE event_type='TRADE_CLOSED'"
    ).fetchone()[0] == 1
    assert len(__import__("json").loads(journal.conn.execute(
        "SELECT original_candles_json FROM signals WHERE signal_id=?", (sid,)
    ).fetchone()[0])) == 40
    journal.advance("BTCUSDT", "15m", full)
    assert journal.conn.execute(
        "SELECT COUNT(*) FROM signal_events WHERE event_type='TRADE_CLOSED'"
    ).fetchone()[0] == 1
    journal.close()


def test_cautious_tp1_can_close_profitably(tmp_path):
    frame = history()
    frame.loc[len(frame)-1, "volume"] = 1
    signal = sample_signal(frame)
    p = signal["trade_plan"]
    journal = SignalJournal(tmp_path/"journal.sqlite3")
    journal.record(signal, frame)
    next_bar = pd.DataFrame([{
        "timestamp": frame.timestamp.iloc[-1]+pd.Timedelta(minutes=15),
        "open": p["entry_price_high"], "high": p["tp1"]+0.2,
        "low": p["entry_price_low"], "close": p["tp1"], "volume": 1000,
    }])
    journal.advance("BTCUSDT", "15m", pd.concat([frame,next_bar], ignore_index=True))
    outcome = journal.export(tmp_path/"out.csv")[0]
    assert outcome["status"] == "CLOSED"
    assert outcome["outcome"] == "WIN"
    assert outcome["exit_reason"] == "TP1_ONLY"
    journal.close()


def test_exit_signal_cancels_unfilled_entry(tmp_path):
    frame = history()
    signal = sample_signal(frame)
    journal = SignalJournal(tmp_path/"journal.sqlite3")
    journal.record(signal, frame)
    sell = {**signal, "action": "EXIT_TO_USDT", "price": 100.0,
            "candle_close_utc": str(frame.timestamp.iloc[-1]+pd.Timedelta(minutes=30))}
    journal.record(sell, frame)
    records = journal.export(tmp_path/"out.csv")
    buys = [row for row in records if row["action"] == "BUY"]
    assert buys[0]["status"] == "CANCELLED"
    journal.close()
