import asyncio
import math
import time
import ccxt.async_support as ccxt
import pandas as pd

TIMEFRAMES = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


class MarketData:
    """Public Bitget market data only. No API credentials or order endpoints."""
    def __init__(self, timeout_ms=20000, rate_limit=True):
        self.exchange = ccxt.bitget({
            "enableRateLimit": rate_limit,
            "timeout": timeout_ms,
            "options": {"defaultType": "swap"},
        })
        self.markets = {}

    async def initialize(self):
        self.markets = await self.exchange.load_markets()

    async def close(self):
        await self.exchange.close()

    async def top_symbols(self, top_n=20, min_quote_volume=500000):
        symbols = [s for s, m in self.markets.items()
                   if m.get("active") is not False and m.get("swap") and m.get("linear")
                   and m.get("quote") == "USDT" and m.get("settle") == "USDT"]
        if not symbols:
            return []
        tickers = await self.exchange.fetch_tickers(symbols)
        ranked = []
        for symbol, ticker in tickers.items():
            last = ticker.get("last")
            quote_volume = ticker.get("quoteVolume")
            if quote_volume is None and last is not None and ticker.get("baseVolume") is not None:
                quote_volume = float(last) * float(ticker["baseVolume"])
            try:
                quote_volume = float(quote_volume)
            except (TypeError, ValueError):
                continue
            if math.isfinite(quote_volume) and quote_volume >= min_quote_volume:
                ranked.append((symbol, quote_volume))
        ranked.sort(key=lambda item: item[1], reverse=True)
        # TOP_N=0 means scan every active USDT linear perpetual market that
        # passes the liquidity floor; otherwise scan the top N dynamically.
        return [s for s, _ in ranked] if top_n == 0 else [s for s, _ in ranked[:top_n]]

    async def candles(self, symbol, timeframe, limit=140):
        raw = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not raw or len(raw) < 102:
            raise ValueError(f"DATA INSUFFICIENT: {symbol} {timeframe} has too few candles")
        # Ignore the still-forming candle; never generate a signal from an open candle.
        closed = raw[:-1]
        if len(closed) < 100:
            raise ValueError(f"DATA INSUFFICIENT: {symbol} {timeframe} closed candles")
        df = pd.DataFrame(closed, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        if len(df) < 100:
            raise ValueError(f"DATA INSUFFICIENT: duplicate {timeframe} candles for {symbol}")
        # Reject stale feeds and major holes in the recent candle sequence.
        now_ms = int(time.time() * 1000)
        interval = TIMEFRAMES[timeframe]
        last_ts = int(df.timestamp.iloc[-1])
        if last_ts + interval > now_ms + 30_000 or now_ms - (last_ts + interval) > interval * 1.2:
            raise ValueError(f"STALE DATA: {symbol} {timeframe} last closed candle is old")
        recent_ts = df.timestamp.tail(100).astype("int64").diff().dropna()
        if (recent_ts != interval).any():
            raise ValueError(f"DATA INSUFFICIENT: candle gaps in {symbol} {timeframe}")
        return df.reset_index(drop=True)


    async def live_price(self, symbol):
        """Return a fresh last price; reject stale quotes and unusually wide spreads."""
        ticker = await self.exchange.fetch_ticker(symbol)
        if not ticker:
            raise ValueError(f"No live quote for {symbol}")
        last = ticker.get("last")
        try:
            last = float(last)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid live quote for {symbol}")
        if not math.isfinite(last) or last <= 0:
            raise ValueError(f"Invalid live quote for {symbol}")
        stamp = ticker.get("timestamp")
        if stamp is not None:
            try:
                age_ms = int(time.time() * 1000) - int(stamp)
            except (TypeError, ValueError):
                raise ValueError(f"INVALID QUOTE TIMESTAMP: {symbol}")
            if age_ms > 90_000 or age_ms < -10_000:
                raise ValueError(f"STALE DATA: live quote for {symbol}")
        bid, ask = ticker.get("bid"), ticker.get("ask")
        try:
            bid, ask = float(bid), float(ask)
            spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        except (ValueError, TypeError, ZeroDivisionError):
            bid, ask, spread_pct = 0.0, 0.0, float("inf")
        if bid > 0 and ask >= bid and spread_pct > 0.15:
            raise ValueError(f"WIDE SPREAD: {symbol} spread {spread_pct:.3f}%")
        # CCXT permits a missing ticker timestamp; require a valid book quote then.
        if stamp is None and (bid <= 0 or ask < bid or not math.isfinite(spread_pct)):
            raise ValueError(f"UNVERIFIED QUOTE: {symbol} no timestamp or valid bid/ask")
        return last

    async def multi_timeframe(self, symbol, semaphore):
        async with semaphore:
            tasks = [self.candles(symbol, tf) for tf in TIMEFRAMES]
            values = await asyncio.gather(*tasks)
            return dict(zip(TIMEFRAMES, values))


async def macro_snapshot(client, symbol):
    """Closed 4h BTC/ETH trend and 6-bar return for relative-strength gating."""
    unknown = {"trend": "UNKNOWN", "return_6_pct": None}
    raw = await client.fetch_ohlcv(symbol, timeframe="4h", limit=100)
    if not raw or len(raw) < 62:
        return unknown
    df = pd.DataFrame(raw[:-1], columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp")
    if len(df) < 60:
        return unknown
    stamps = df.timestamp.tail(60).astype("int64")
    interval = TIMEFRAMES["4h"]
    now = int(time.time() * 1000)
    last_open = int(stamps.iloc[-1])
    if (stamps.diff().dropna() != interval).any():
        return unknown
    if last_open + interval > now + 30_000 or now - (last_open + interval) > interval * 1.2:
        return unknown
    close = pd.to_numeric(df.close, errors="coerce")
    if close.isna().any() or (close <= 0).any():
        return unknown
    e9 = close.ewm(span=9, adjust=False).mean().iloc[-1]
    e21 = close.ewm(span=21, adjust=False).mean().iloc[-1]
    e50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    trend_value = ("BULLISH" if close.iloc[-1] > e9 > e21 > e50 else
                   "BEARISH" if close.iloc[-1] < e9 < e21 < e50 else "NEUTRAL")
    return {"trend": trend_value,
            "return_6_pct": 100 * (float(close.iloc[-1]) / float(close.iloc[-7]) - 1)}


async def macro_trend(client, symbol):
    """Compatibility wrapper for consumers needing only the macro direction."""
    return (await macro_snapshot(client, symbol))["trend"]
