"""Public API smoke check for Bitget signal generation; no private keys or orders.

Executed independently of Telegram. Exits nonzero if active BTC/ETH feed,
closed candles, market discovery or quotes are incompatible with the scanner.
"""
import asyncio
import logging

from exchange import MarketData, macro_snapshot
from strategy import evaluate

logging.basicConfig(level=logging.WARNING)


async def main():
    data = MarketData()
    try:
        await data.initialize()
        symbols = await data.top_symbols(top_n=15, min_quote_volume=500_000)
        assert symbols, "No liquid crypto USDT perpetual contracts discovered"
        print(f"Eligible market sample: {symbols[:5]}")
        btc, eth = await asyncio.gather(
            macro_snapshot(data.exchange, "BTC/USDT:USDT"),
            macro_snapshot(data.exchange, "ETH/USDT:USDT"),
        )
        assert btc["trend"] != "UNKNOWN", "BTC 4h macro data invalid"
        assert eth["trend"] != "UNKNOWN", "ETH 4h macro data invalid"
        print(f"BTC 4h: {btc['trend']} / ETH 4h: {eth['trend']}")
        for symbol in ("BTC/USDT:USDT", "ETH/USDT:USDT"):
            candles = await data.multi_timeframe(symbol, asyncio.Semaphore(1))
            quote = await data.live_price(symbol)
            assert quote > 0
            print(symbol, "quote:", round(quote, 5),
                  "closed bars:", {tf: len(rows) for tf, rows in candles.items()})
            result = evaluate(
                symbol, candles, btc["trend"], eth["trend"],
                current_price=quote, btc_return_6_pct=btc["return_6_pct"],
                eth_return_6_pct=eth["return_6_pct"],
            )
            # A quiet market legitimately has no qualifying setup.
            print(symbol, "setup:", "QUALIFIED" if result else "none (normal)")
        print("PASS: public Bitget data/quote integration; Telegram not tested")
    finally:
        await data.close()


if __name__ == "__main__":
    asyncio.run(main())
