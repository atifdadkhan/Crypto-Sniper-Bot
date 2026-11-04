"""Dual-engine correctness and fail-closed tests, offline only."""
import asyncio
from dataclasses import dataclass
import math

import pandas as pd
import pytest

from delivery import AlertOutbox
from exchange import MarketData
from formatting import signal_text
from relative_strength import (
    TF_MS, MIN_HISTORY, evaluate_relative_strength, precheck_recent_breakout,
)


def history(direction="LONG", length=340):
    base = pd.Timestamp("2026-01-01T00:00:00Z").value // 1_000_000
    rows = []
    sign = 1 if direction == "LONG" else -1
    for i in range(length):
        price = (100 if sign == 1 else 200) + sign * 0.08 * i
        if i == length - 1:
            price += sign * 5
        rows.append([base + i * TF_MS, price - sign * .03,
                     price + .14, price - .14, price, 120.0])
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])


def macro(df, btc=0.2, eth=0.5):
    stamp = int(df.timestamp.iloc[-1]) + TF_MS
    return ({"trend": "BEARISH", "return_7d_pct": btc, "candle_close_ms": stamp},
            {"trend": "BEARISH", "return_7d_pct": eth, "candle_close_ms": stamp})


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_relative_strength_matches_direction_and_3r_target(side):
    df = history(side)
    btc, eth = macro(df)
    assert precheck_recent_breakout(df.tail(140), btc, eth)
    quote = float(df.close.iloc[-1])
    s = evaluate_relative_strength("TEST/USDT:USDT", df, quote, btc, eth)
    assert s is not None, f"synthetic {side} setup rejected"
    assert s.side == side
    assert s.strategy_id == "RS_4H"
    assert s.score == 0  # No invented win-probability or misleading 10-point score
    assert s.ttl_seconds == 3600
    assert s.candle_close_ms == int(df.timestamp.iloc[-1]) + TF_MS
    risk = abs(s.entry - s.stop_loss)
    assert risk == pytest.approx(2 * s.atr)
    assert abs(s.tp2 - s.entry) == pytest.approx(3 * risk)


def test_wrong_macro_candle_and_missing_benchmark_block_signal():
    df = history()
    btc, eth = macro(df)
    btc["candle_close_ms"] += TF_MS
    assert not precheck_recent_breakout(df.tail(140), btc, eth)
    assert evaluate_relative_strength("TEST", df, float(df.close.iloc[-1]), btc, eth) is None
    btc, eth = macro(df)
    btc["return_7d_pct"] = None
    assert evaluate_relative_strength("TEST", df, float(df.close.iloc[-1]), btc, eth) is None


def test_bad_4h_gaps_and_spoofed_candle_rejected():
    df = history()
    btc, eth = macro(df)
    with pytest.raises(ValueError, match="contiguous"):
        evaluate_relative_strength("TEST", df.drop(index=75), float(df.close.iloc[-1]), btc, eth)
    bad = df.copy()
    bad.loc[40, "high"] = 1
    with pytest.raises(ValueError, match="geometry"):
        evaluate_relative_strength("TEST", bad, float(df.close.iloc[-1]), btc, eth)
    with pytest.raises(ValueError, match="320"):
        evaluate_relative_strength("TEST", df.tail(MIN_HISTORY - 1), float(df.close.iloc[-1]), btc, eth)


def test_no_chasing_old_price_and_reject_nonfinite_quote():
    df = history()
    btc, eth = macro(df)
    assert evaluate_relative_strength("TEST", df, 100000, btc, eth) is None
    assert evaluate_relative_strength("TEST", df, float("nan"), btc, eth) is None


class PagedExchange:
    def __init__(self, df):
        self.rows = df.values.tolist()
        self.calls = 0

    async def fetch_ohlcv(self, symbol, timeframe="4h", since=None, limit=100):
        self.calls += 1
        rows = self.rows if since is None else [r for r in self.rows if r[0] >= since]
        return rows[:limit]


def test_older_4h_history_is_paged_without_synthetic_candles():
    df = history()
    bot = MarketData()
    provider = PagedExchange(df)
    bot.exchange = provider
    recent = df.tail(140).copy().reset_index(drop=True)
    out = asyncio.run(bot.extended_four_hour("TEST", recent))
    assert len(out) == 330
    assert provider.calls >= 2
    assert list(out.timestamp.diff().dropna().unique()) == [TF_MS]


@dataclass
class BasicSignal:
    symbol: str
    side: str
    candle_close_ms: int
    strategy_id: str
    ttl_seconds: int

    def to_dict(self):
        return vars(self).copy()


def test_two_strategies_can_queue_same_symbol_side_and_candle(tmp_path):
    now = 1_800_000_000.0
    q = AlertOutbox(tmp_path / "outbox.json", expiry_seconds=900)
    base = BasicSignal("BTC/USDT:USDT", "LONG", int((now - 200) * 1000), "CORE_15M", 900)
    rs = BasicSignal("BTC/USDT:USDT", "LONG", int((now - 200) * 1000), "RS_4H", 3600)
    assert q.add(base, "15m", now=now)
    assert q.add(rs, "4h", now=now)
    assert len(q.items) == 2
    assert q.items[0]["id"] != q.items[1]["id"]
    assert not q.add(rs, "dupe", now=now)
    assert q.items[0]["expires_at"] == now + 700
    assert q.items[1]["expires_at"] == now + 3400


def test_4h_stale_alerts_rejected(tmp_path):
    now = 1_800_000_000.0
    q = AlertOutbox(tmp_path / "queue.json", expiry_seconds=900)
    stale = BasicSignal("ETH", "SHORT", int((now - 3601) * 1000), "RS_4H", 3600)
    assert not q.add(stale, "stale", now=now)


def test_both_telegram_messages_identify_strategy():
    df = history()
    btc, eth = macro(df)
    s = evaluate_relative_strength("TEST", df, float(df.close.iloc[-1]), btc, eth)
    assert "4H Relative Strength Breakout" in signal_text(s)
    assert "Target (3R)" in signal_text(s)
    assert "TP1" not in signal_text(s)

    class Core:
        side, symbol, score, entry, stop_loss, tp1, tp2 = "LONG", "BTC", 8, 100, 98, 103, 105
        setup = "Pullback"
    assert "15m Trend Pullback" in signal_text(Core())
    assert "TP1" in signal_text(Core())
