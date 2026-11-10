"""Resumable public Binance Spot and USDⓈ-M perpetual OHLCV research downloader.

No API keys, private endpoints, or orders. Each invocation handles one market type
and one deterministic symbol batch; incomplete symbols are never marked eligible.
"""
import argparse
import json
import math
import time
from pathlib import Path

import ccxt
import pandas as pd

from backtesting.download_bitget import is_excluded_non_crypto_base

TIMEFRAMES = ("15m", "1h", "4h", "1d")
DAY_MS = 86_400_000
LIMIT = 1000


def exchange_for(kind):
    if kind == "spot":
        return ccxt.binance({"enableRateLimit": True, "timeout": 30000,
                             "options": {"defaultType": "spot"}})
    if kind == "usdt_perpetual":
        return ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})
    raise ValueError("market type must be spot or usdt_perpetual")


def discover(exchange, kind, min_quote_volume):
    markets = exchange.load_markets()
    try:
        tickers = exchange.fetch_tickers()
    except Exception as exc:
        raise RuntimeError("MARKET DISCOVERY INCOMPLETE: ticker snapshot unavailable") from exc
    if not tickers:
        raise RuntimeError("MARKET DISCOVERY INCOMPLETE: empty ticker snapshot")
    selected = []
    for symbol, market in markets.items():
        if not market.get("active", True) or market.get("quote") != "USDT":
            continue
        if kind == "spot":
            if not market.get("spot") or market.get("contract"):
                continue
        else:
            if not market.get("swap") or market.get("linear") is not True or market.get("settle") != "USDT":
                continue
        if is_excluded_non_crypto_base(market.get("base"), market=market):
            continue
        ticker = tickers.get(symbol) or {}
        quote_volume = ticker.get("quoteVolume")
        try:
            quote_volume = float(quote_volume or 0)
        except (TypeError, ValueError):
            quote_volume = 0
        if quote_volume >= min_quote_volume or market.get("base") in ("BTC", "ETH"):
            selected.append((symbol, quote_volume))
    selected.sort(key=lambda x: (x[0].split("/")[0] not in ("BTC", "ETH"), -x[1], x[0]))
    return selected


def fetch_history(exchange, symbol, timeframe, days, min_history_days=7):
    """Fetch closed candles; accept shorter listings only above a declared floor."""
    step_ms = int(exchange.parse_timeframe(timeframe) * 1000)
    now_ms = exchange.milliseconds()
    end_ms = (now_ms // step_ms) * step_ms
    requested_start = end_ms - days * DAY_MS
    cursor = requested_start - (requested_start % step_ms)
    rows = {}
    while cursor < end_ms:
        page = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=LIMIT)
        if not page:
            break
        advanced = False
        for row in page:
            stamp = int(row[0])
            if cursor <= stamp < end_ms:
                rows[stamp] = row[:6]
                advanced = True
        next_cursor = int(page[-1][0]) + step_ms
        if next_cursor <= cursor or not advanced:
            break
        cursor = next_cursor
        time.sleep(max(exchange.rateLimit, 50) / 1000)
    if not rows:
        raise RuntimeError("DATA INSUFFICIENT: provider returned no candles")
    frame = pd.DataFrame([rows[k] for k in sorted(rows)],
                         columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame[frame["timestamp"] + pd.to_timedelta(step_ms, unit="ms")
                  <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]
    frame = frame.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    if frame.empty:
        raise RuntimeError("DATA INSUFFICIENT: no completed candles")
    actual_days = (end_ms - int(frame.timestamp.iloc[0].timestamp() * 1000)) / DAY_MS
    if actual_days < min_history_days:
        raise RuntimeError(
            f"DATA INSUFFICIENT: only {actual_days:.1f}d history; "
            f"minimum is {min_history_days}d"
        )
    age = pd.Timestamp(end_ms, unit="ms", tz="UTC") - (
        frame.timestamp.iloc[-1] + pd.to_timedelta(step_ms, unit="ms"))
    if age > pd.to_timedelta(step_ms * 2, unit="ms"):
        raise RuntimeError("DATA INSUFFICIENT: latest candle is stale")
    frame.attrs["actual_history_days"] = round(float(actual_days), 2)
    return frame


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("discover", "download"))
    p.add_argument("--market-type", choices=("spot", "usdt_perpetual"), required=True)
    p.add_argument("--symbols", default="")
    p.add_argument("--batch-size", type=int, default=10)
    p.add_argument("--min-quote-volume", type=float, default=1_000_000)
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--min-history-days", type=int, default=7)
    p.add_argument("--out", default="data")
    p.add_argument("--batch-id", default="batch")
    a = p.parse_args()
    if not 120 <= a.days <= 2000:
        p.error("--days must be between 120 and 2000")
    if a.batch_size < 1 or a.min_quote_volume < 0:
        p.error("batch-size must be positive and volume floor non-negative")
    if not 1 <= a.min_history_days <= a.days:
        p.error("--min-history-days must be between 1 and --days")
    ex = exchange_for(a.market_type)
    try:
        ex.load_markets()
        if a.command == "discover":
            universe = discover(ex, a.market_type, a.min_quote_volume)
            batches = [ [x[0] for x in universe[i:i+a.batch_size]]
                        for i in range(0, len(universe), a.batch_size) ]
            print(json.dumps({"market_type": a.market_type, "universe_size": len(universe),
                              "batches": batches}))
            return
        symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]
        if not symbols:
            p.error("download requires --symbols")
        root = Path(a.out)
        root.mkdir(parents=True, exist_ok=True)
        manifest = {"exchange": "Binance", "market_type": a.market_type,
                    "days_requested": a.days, "min_history_days": a.min_history_days,
                    "timeframes": list(TIMEFRAMES),
                    "symbols_requested": symbols, "downloaded": [], "failed": [],
                    "data_policy": "provider OHLCV only; no synthetic candles; gaps split backtests"}
        for symbol in symbols:
            result = {"symbol": symbol, "timeframes": {}}
            try:
                market = ex.market(symbol)
                base = str(market.get("base") or symbol.split("/")[0]).upper()
                folder = root / f"{base}USDT"
                staging = root / f".{base}USDT.partial"
                if staging.exists():
                    import shutil
                    shutil.rmtree(staging)
                staging.mkdir(parents=True)
                successful_timeframes = []
                for tf in TIMEFRAMES:
                    try:
                        frame = fetch_history(ex, symbol, tf, a.days, a.min_history_days)
                        frame.to_csv(staging / f"{tf}.csv", index=False)
                        delta = pd.DatetimeIndex(frame.timestamp).asi8
                        step = int(ex.parse_timeframe(tf) * 1_000_000_000)
                        gaps = int((delta[1:] - delta[:-1] != step).sum())
                        result["timeframes"][tf] = {"status": "VALID", "candles": len(frame),
                            "gap_count": gaps, "first_timestamp": str(frame.timestamp.iloc[0]),
                            "last_timestamp": str(frame.timestamp.iloc[-1]),
                            "actual_history_days": frame.attrs.get("actual_history_days")}
                        successful_timeframes.append(tf)
                    except Exception as exc:
                        result["timeframes"][tf] = {
                            "status": "DATA INSUFFICIENT",
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                        print(f"DATA INSUFFICIENT {symbol} {tf}: {exc}")
                if not successful_timeframes:
                    raise RuntimeError("no timeframe met the declared history/freshness floor")
                if folder.exists():
                    import shutil
                    shutil.rmtree(folder)
                staging.rename(folder)
                result["status"] = ("VALID" if len(successful_timeframes) == len(TIMEFRAMES)
                                    else "PARTIAL")
                result["successful_timeframes"] = successful_timeframes
                manifest["downloaded"].append(result)
                print(f"{result['status']} {symbol}: {successful_timeframes}")
            except Exception as exc:
                if staging.exists():
                    import shutil
                    shutil.rmtree(staging, ignore_errors=True)
                manifest["failed"].append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"})
                print(f"DATA INSUFFICIENT {symbol}: {exc}")
        manifest["eligible_count"] = len(manifest["downloaded"])
        (root / "download_manifest.json").write_text(json.dumps(manifest, indent=2))
        if not manifest["downloaded"]:
            raise RuntimeError("No complete symbol datasets in this batch")
    finally:
        close = getattr(ex, "close", None)
        if close:
            close()


if __name__ == "__main__":
    main()
