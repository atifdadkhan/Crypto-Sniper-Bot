import asyncio
import time

import pytest

from exchange import MarketData, macro_snapshot


class FakeExchange:
    def __init__(self, candles=None, quote=None):
        self.source = candles or []
        self.quote = quote or {}

    async def fetch_ohlcv(self, symbol, timeframe="15m", since=None, limit=140):
        return self.source

    async def fetch_ticker(self, symbol):
        return self.quote

    async def close(self):
        return None


def make_candles(interval=900_000, count=140, price=100):
    now_ms = int(time.time() * 1000)
    current_open = now_ms // interval * interval
    return [[current_open - (count - i - 1) * interval,
             price - .1, price + 1, price - 1, price + .1, 100.0]
            for i in range(count)]


def test_single_missing_candle_is_not_silently_accepted():
    bot = MarketData()
    rows = make_candles()
    bot.exchange = FakeExchange(rows)
    clean = asyncio.run(bot.candles("TEST/USDT:USDT", "15m"))
    assert len(clean) == len(rows) - 1
    # A 30-minute gap used to pass the old 2.5x-gap tolerance.
    rows.pop(-10)
    with pytest.raises(ValueError, match="missing/duplicate"):
        asyncio.run(bot.candles("TEST/USDT:USDT", "15m"))


def test_invalid_ohlc_geometry_is_rejected():
    bot = MarketData()
    rows = make_candles()
    rows[-10][2] = 1.0  # High below open.
    bot.exchange = FakeExchange(rows)
    # Indicator code will reject bad rows, but live fetch should reject early.
    with pytest.raises(ValueError, match="DATA INSUFFICIENT|invalid"):
        asyncio.run(bot.candles("TEST/USDT:USDT", "15m"))


def test_unverifiable_and_wide_quote_rejected():
    bot = MarketData()
    bot.exchange = FakeExchange(quote={"last": 100.0})
    with pytest.raises(ValueError, match="UNVERIFIED"):
        asyncio.run(bot.live_price("TEST/USDT:USDT"))
    bot.exchange.quote = {"last": 100.0, "bid": 99.0, "ask": 101.0}
    with pytest.raises(ValueError, match="WIDE SPREAD"):
        asyncio.run(bot.live_price("TEST/USDT:USDT"))
    bot.exchange.quote = {"last": 100.0, "bid": 99.98, "ask": 100.02}
    assert asyncio.run(bot.live_price("TEST/USDT:USDT")) == 100.0


def test_macro_snapshot_with_closed_candles():
    rows = make_candles(interval=14_400_000, count=100)
    # Advance 4h closes so 6-bar returns are nonzero.
    for i, row in enumerate(rows):
        p = 60 + .4 * i
        row[1:5] = [p - .2, p + 1, p - 1, p]
    result = asyncio.run(macro_snapshot(FakeExchange(rows), "BTC/USDT:USDT"))
    assert result["trend"] in {"BULLISH", "BEARISH", "NEUTRAL"}
    assert result["return_6_pct"] > 0



def test_daily_api_page_limit_uses_older_real_candles():
    class Paged(FakeExchange):
        def __init__(self, candles):
            super().__init__(candles)
        async def fetch_ohlcv(self, symbol, timeframe="1d", since=None, limit=140):
            if since is None:
                return self.source[-100:]
            return self.source[-200:-100]

    rows = make_candles(interval=86_400_000, count=200)
    bot = MarketData()
    bot.exchange = Paged(rows)
    result = asyncio.run(bot.candles("BTC/USDT:USDT", "1d"))
    assert len(result) == 199
    assert result.timestamp.iloc[-1] == rows[-2][0]
