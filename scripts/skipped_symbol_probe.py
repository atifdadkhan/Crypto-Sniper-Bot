"""Read-only targeted Bitget data-quality probe for repeatedly skipped symbols.

Runs only public CCXT endpoints in GitHub CI. Prints strictly allowlisted
diagnostic category labels, never raw provider errors, URLs, credentials,
quotes, prices or account data. Does not evaluate signals or place trades.
This probe is informational: an invalid market must remain invalid.
"""
import asyncio

from exchange import MarketData
from scan_diagnostics import safe_failure_reason

SYMBOLS = ("AKE/USDT:USDT", "CLANKER/USDT:USDT", "TRUMP/USDT:USDT")
TIMEFRAMES = ("15m", "1h", "4h", "1d")


async def probe_symbol(data, symbol):
    market = data.markets.get(symbol)
    if not market or market.get("active") is False:
        print(f"{symbol} not-active-or-unlisted")
        return
    for timeframe in TIMEFRAMES:
        try:
            bars = await data.candles(symbol, timeframe)
        except Exception as exc:
            print(f"{symbol} {timeframe}: {type(exc).__name__} reason={safe_failure_reason(exc)}")
        else:
            print(f"{symbol} {timeframe}: valid closed_bars={len(bars)}")
    try:
        await data.live_price(symbol)
    except Exception as exc:
        print(f"{symbol} quote: {type(exc).__name__} reason={safe_failure_reason(exc)}")
    else:
        print(f"{symbol} quote: valid")


async def main():
    data = MarketData()
    try:
        await data.initialize()
        for symbol in SYMBOLS:
            await probe_symbol(data, symbol)
    finally:
        await data.close()


if __name__ == "__main__":
    asyncio.run(main())
