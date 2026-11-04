import asyncio
import math
import time
import ccxt.async_support as ccxt
import pandas as pd

from backtesting.download_bitget import is_excluded_non_crypto_base

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
                   and m.get("quote") == "USDT" and m.get("settle") in (None, "USDT")
                   and not is_excluded_non_crypto_base(m.get("base"), market=m)]
        if not symbols:
            return []
        tickers = await self.exchange.fetch_tickers(symbols)
        ranked = []
        for symbol, ticker in tickers.items():
            last = ticker.get("last")
            quote_volume = ticker.get("usdtVolume")
            if quote_volume is None:
                info = ticker.get("info") or {}
                quote_volume = info.get("usdtVolume") if isinstance(info, dict) else None
            if quote_volume is None:
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
        interval = TIMEFRAMES[timeframe]
        if raw and len(raw) < 101:
            # Bitget may cap a daily request to 100 rows and historical
            # requests to 90 calendar days. Fetch a nearby earlier page;
            # never invent the missing 100th closed candle.
            try:
                first_open = min(int(row[0]) for row in raw)
                older = await self.exchange.fetch_ohlcv(
                    symbol, timeframe=timeframe,
                    since=first_open - min(75 * 86_400_000, 100 * interval), limit=100,
                )
                raw = sorted({int(row[0]): row for row in [*(older or []), *raw]}.values(),
                             key=lambda row: int(row[0]))
            except (TypeError, ValueError, IndexError):
                pass
        if not raw or len(raw) < 100:
            raise ValueError(f"DATA INSUFFICIENT: {symbol} {timeframe} has too few candles")
        # Select by actual candle close timestamp. Providers sometimes omit the
        # still-forming candle; blindly dropping [-1] then loses a valid bar.
        now_ms = int(time.time() * 1000)
        interval = TIMEFRAMES[timeframe]
        try:
            closed = [bar for bar in raw if int(bar[0]) + interval <= now_ms - 5_000]
        except (ValueError, TypeError, IndexError):
            raise ValueError(f"DATA INSUFFICIENT: invalid candle timestamps for {symbol}")
        if len(closed) < 100:
            raise ValueError(f"DATA INSUFFICIENT: {symbol} {timeframe} closed candles")
        df = pd.DataFrame(closed, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        if len(df) < 100:
            raise ValueError(f"DATA INSUFFICIENT: duplicate {timeframe} candles for {symbol}")
        # Reject stale feeds and major holes in the recent candle sequence.
        last_ts = int(df.timestamp.iloc[-1])
        if last_ts + interval > now_ms + 30_000 or now_ms - (last_ts + interval) > interval * 1.2:
            raise ValueError(f"STALE DATA: {symbol} {timeframe} last closed candle is old")
        recent_ts = df.timestamp.tail(100).astype("int64").diff().dropna()
        if (recent_ts != interval).any():
            bad = recent_ts[recent_ts != interval]
            sample = [(int(idx), int(gap)) for idx, gap in bad.head(3).items()]
            raise ValueError(f"DATA INSUFFICIENT: missing/duplicate {timeframe} candles for {symbol}; gaps_ms={sample}")
        numeric = df[["open", "high", "low", "close", "volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        if numeric.isna().any().any() or not numeric.map(math.isfinite).all().all():
            raise ValueError(f"DATA INSUFFICIENT: invalid numeric OHLCV for {symbol}")
        if ((numeric[["open", "high", "low", "close"]] <= 0).any().any()
                or (numeric["volume"] < 0).any()):
            raise ValueError(f"DATA INSUFFICIENT: invalid OHLCV values for {symbol}")
        if ((numeric["high"] < numeric[["open", "low", "close"]].max(axis=1)) |
                (numeric["low"] > numeric[["open", "high", "close"]].min(axis=1))).any():
            raise ValueError(f"DATA INSUFFICIENT: invalid OHLC geometry for {symbol}")
        df[numeric.columns] = numeric
        return df.reset_index(drop=True)


    async def extended_four_hour(self, symbol, recent, min_bars=330):
        """Page real completed 4H candles for EMA300 warmup; never fabricate bars.

        Called only when the cheaper latest-4H breakout/relative-strength
        precheck passes. Normal 15m scanner's candle history is unchanged.
        """
        interval = TIMEFRAMES["4h"]
        if recent is None or len(recent) < 100:
            raise ValueError(f"DATA INSUFFICIENT: {symbol} missing recent 4H bars")
        newest_close = int(recent.timestamp.iloc[-1]) + interval
        now_ms = int(time.time() * 1000)
        if newest_close > now_ms - 5000 or now_ms - newest_close > interval * 1.2:
            raise ValueError(f"STALE DATA: {symbol} 4H")
        rows = {
            int(row.timestamp): [int(row.timestamp), float(row.open), float(row.high),
                                 float(row.low), float(row.close), float(row.volume)]
            for row in recent.itertuples(index=False)
        }
        # Bitget historical API page limits vary. Use small overlapping pages
        # anchored immediately BEFORE the earliest collected timestamp.
        for _ in range(6):
            if len(rows) >= min_bars:
                break
            first = min(rows)
            requested_since = first - 90 * interval
            older = await self.exchange.fetch_ohlcv(
                symbol, timeframe="4h", since=requested_since, limit=100
            )
            if not older:
                break
            for bar in older:
                if len(bar) < 6:
                    continue
                ts = int(bar[0])
                if ts + interval > now_ms - 5000 or ts > max(rows):
                    continue
                rows[ts] = bar[:6]
            if min(rows) >= first:
                break
        if len(rows) < min_bars:
            raise ValueError(f"DATA INSUFFICIENT: {symbol} 4H EMA300 warmup ({len(rows)} bars)")
        frame = pd.DataFrame(
            [rows[ts] for ts in sorted(rows)[-min_bars:]],
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        times = pd.to_numeric(frame.timestamp, errors="coerce")
        if times.isna().any() or (times.diff().dropna() != interval).any():
            raise ValueError(f"DATA INSUFFICIENT: {symbol} 4H historical gap")
        if int(times.iloc[-1]) + interval != newest_close:
            raise ValueError(f"DATA INSUFFICIENT: {symbol} 4H history shifted")
        vals = frame[["open", "high", "low", "close", "volume"]].apply(
            pd.to_numeric, errors="coerce"
        )
        if vals.isna().any().any() or not vals.map(math.isfinite).all().all():
            raise ValueError(f"DATA INSUFFICIENT: invalid {symbol} 4H history")
        if ((vals[["open", "high", "low", "close"]] <= 0).any().any()
                or (vals.volume < 0).any()
                or (vals.high < vals[["open", "low", "close"]].max(axis=1)).any()
                or (vals.low > vals[["open", "high", "close"]].min(axis=1)).any()):
            raise ValueError(f"DATA INSUFFICIENT: invalid {symbol} 4H OHLC geometry")
        frame[vals.columns] = vals
        return frame.reset_index(drop=True)


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
    unknown = {"trend": "UNKNOWN", "return_6_pct": None,
               "return_7d_pct": None, "candle_close_ms": None}
    raw = await client.fetch_ohlcv(symbol, timeframe="4h", limit=100)
    if not raw or len(raw) < 60:
        return unknown
    interval = TIMEFRAMES["4h"]
    now = int(time.time() * 1000)
    try:
        closed = [bar for bar in raw if int(bar[0]) + interval <= now - 5_000]
    except (ValueError, TypeError, IndexError):
        return unknown
    df = pd.DataFrame(closed, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.sort_values("timestamp").drop_duplicates("timestamp")
    if len(df) < 60:
        return unknown
    stamps = df.timestamp.tail(60).astype("int64")
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
            "return_6_pct": 100 * (float(close.iloc[-1]) / float(close.iloc[-7]) - 1),
            "return_7d_pct": 100 * (float(close.iloc[-1]) / float(close.iloc[-43]) - 1),
            "candle_close_ms": last_open + interval}


async def macro_trend(client, symbol):
    """Compatibility wrapper for consumers needing only the macro direction."""
    return (await macro_snapshot(client, symbol))["trend"]
