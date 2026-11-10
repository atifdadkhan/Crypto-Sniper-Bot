"""Free CCXT Pro Bitget trade/book streams feeding independent strategy engines.

Streaming a bounded liquid watch is necessary on a shared Render instance;
full exchange discovery remains dynamic, with no fixed hard-coded coin list.
No exchange keys, private APIs or broker order execution are involved.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import ccxt.pro as ccxtpro

from orderflow_engines import Tape

log = logging.getLogger("trendhive.orderflow")


async def stream_market(exchange, symbol, tape, publish, selected, market_data, status, paper=None):
    """Reconnect trade and L2 channels separately; never alert on stale books."""
    async def consume_trades():
        backoff = 1
        while True:
            try:
                events = await exchange.watch_trades(symbol, limit=100)
                backoff = 1
                for trade in events or []:
                    if not tape.add_trade(trade):
                        continue
                    status["last_trade_at"] = time.time()
                    # Each engine is evaluated independently and named in the alert.
                    for name, fn in (("cvd", tape.cvd_divergence),
                                     ("wick", tape.wick_hunt)):
                        if selected() not in ("both", name):
                            continue
                        signal = fn()
                        if signal is not None:
                            try:
                                await publish(signal)
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                # Telegram failures must not burn the signal's cooldown.
                                tape.last_alert.pop(signal.engine_id + signal.side, None)
                                log.warning("%s %s signal delivery failed", symbol, signal.engine_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                tape.book = None
                tape.reset_flow()
                status["last_trade_at"] = None
                status["last_book_at"] = None
                log.warning("%s trades websocket %s; CVD reset before retry", symbol, type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def consume_book():
        backoff = 1
        while True:
            try:
                book = await exchange.watch_order_book(symbol, limit=5)
                tape.set_book(book)
                if tape.book is not None:
                    status["last_book_at"] = time.time()
                    if paper is not None:
                        bids, asks = tape.book
                        paper.quote(symbol, bids[0][0], asks[0][0])
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                tape.book = None
                log.warning("%s book websocket %s; retrying", symbol, type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def update_swings():
        while True:
            try:
                # Exchange module guarantees closed, ordered 1h bars.
                candles = await asyncio.wait_for(market_data.candles(symbol, "1h"), timeout=35)
                tape.set_swings(candles)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s 1h swing refresh %s", symbol, type(exc).__name__)
            await asyncio.sleep(1800)

    tasks = [asyncio.create_task(consume_trades()),
             asyncio.create_task(consume_book()),
             asyncio.create_task(update_swings())]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_orderflow(market_data, settings, selected, publish, status, paper=None):
    """Live rolling 24/7 public WS subscriptions, safe reconnection and refresh."""
    backoff = 3
    while True:
        pro = None
        try:
            if not market_data.markets:
                await market_data.initialize()
            limit = max(1, min(12, int(os.getenv("ORDERFLOW_TOP_N", "8"))))
            ranked = await market_data.top_symbols(top_n=limit, min_quote_volume=settings.min_quote_volume)
            if not ranked:
                raise RuntimeError("No eligible high-volume Bitget swap symbols")
            symbols = list(dict.fromkeys(["BTC/USDT:USDT"] + ranked))[:limit]
            if paper is not None:
                # Keep monitoring existing paper positions after rank rotation.
                symbols = list(dict.fromkeys(symbols + paper.open_symbols()))
            pro = ccxtpro.bitget({
                "enableRateLimit": True,
                "timeout": 20000,
                "newUpdates": True,
                "options": {"defaultType": "swap"},
            })
            await pro.load_markets()
            symbols = [s for s in symbols if s in pro.markets and pro.markets[s].get("active") is not False]
            if not symbols:
                raise RuntimeError("No orderflow symbols supported by Bitget")
            status.update(connected=False, symbols=symbols, error=None,
                          last_trade_at=None, last_book_at=None)
            log.info("Orderflow streams starting for %d liquid USDT futures", len(symbols))
            tasks = []
            for symbol in symbols:
                tape = Tape(symbol)
                tasks.append(asyncio.create_task(
                    stream_market(pro, symbol, tape, publish, selected, market_data, status, paper)))
            status.update(connected=True, started=time.time())
            backoff = 3
            try:
                # Periodic universe refresh reconnects every 4 hours.
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=4 * 3600)
            except asyncio.TimeoutError:
                log.info("Rotating orderflow liquid market universe")
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            status.update(connected=False, error=type(exc).__name__)
            log.warning("Orderflow stream restart: %s", type(exc).__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 90)
        finally:
            if pro is not None:
                try:
                    await pro.close()
                except Exception:
                    pass
