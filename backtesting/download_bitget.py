"""Discover and download Bitget USDT perpetual OHLCV history via public endpoints.
No API credentials, private endpoints, or order permissions are used.
"""
import argparse
import json
import time
from pathlib import Path
import ccxt
import pandas as pd

TIMEFRAMES = ("15m", "1h", "4h", "1d")


def discover_usdt_swaps(exchange, min_quote_volume=500_000.0, max_symbols=0):
    """Return active linear USDT swaps meeting the 24h quote-volume floor.

    BTC and ETH are always included as macro benchmarks, even if their ticker
    volume is unavailable. max_symbols=0 means no artificial symbol cap.
    """
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()
    selected = []
    for symbol, market in markets.items():
        if not market.get("active", True) or not market.get("swap"):
            continue
        if market.get("quote") != "USDT" or market.get("linear") is False:
            continue
        ticker = tickers.get(symbol) or {}
        quote_volume = ticker.get("quoteVolume")
        if quote_volume is None:
            base_volume, last = ticker.get("baseVolume"), ticker.get("last")
            quote_volume = (base_volume * last) if base_volume and last else 0
        try:
            quote_volume = float(quote_volume or 0)
        except (TypeError, ValueError):
            quote_volume = 0.0
        base = market.get("base")
        benchmark = base in {"BTC", "ETH"}
        if benchmark or quote_volume >= min_quote_volume:
            selected.append((symbol, quote_volume, benchmark))
    selected.sort(key=lambda x: (not x[2], -x[1], x[0]))
    if max_symbols > 0:
        # Preserve both macro benchmarks within the cap.
        benchmarks = [x for x in selected if x[2]]
        others = [x for x in selected if not x[2]]
        selected = (benchmarks + others)[:max_symbols]
    return selected


def download_one(exchange, symbol: str, timeframe: str, days: int, out_dir: Path):
    if not exchange.has.get("fetchOHLCV"):
        raise RuntimeError("Bitget CCXT adapter does not report fetchOHLCV support")
    since = exchange.milliseconds() - days * 86_400_000
    cursor, step, rows, last_seen = since, exchange.parse_timeframe(timeframe) * 1000, [], None
    while cursor < exchange.milliseconds():
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        newest = int(batch[-1][0])
        if last_seen is not None and newest <= last_seen:
            raise RuntimeError(f"Non-advancing OHLCV pagination for {symbol} {timeframe}")
        last_seen, cursor = newest, newest + step
        if cursor >= exchange.milliseconds():
            break
        time.sleep(max(exchange.rateLimit, 100) / 1000)
    if not rows:
        raise RuntimeError(f"No OHLCV data returned for {symbol} {timeframe}")
    frame = pd.DataFrame(rows, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    frame = frame.drop_duplicates("timestamp_ms").sort_values("timestamp_ms")
    now_ms = exchange.milliseconds()
    frame = frame[frame["timestamp_ms"] + step <= now_ms]
    frame["timestamp"] = pd.to_datetime(frame.pop("timestamp_ms"), unit="ms", utc=True)
    frame = frame[["timestamp", "open", "high", "low", "close", "volume"]]
    folder_name = exchange.market(symbol).get("base", symbol.split("/")[0]).upper()
    folder = out_dir / f"{folder_name}USDT"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{timeframe}.csv"
    frame.to_csv(path, index=False)
    print(f"{symbol} {timeframe}: {len(frame)} candles -> {path}")
    return len(frame)


def main():
    parser = argparse.ArgumentParser(description="Download all eligible Bitget USDT perpetual markets")
    parser.add_argument("--symbols", default="auto", help="auto or comma-separated CCXT symbols")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--out", default="data")
    parser.add_argument("--min-quote-volume", type=float, default=500_000,
                         help="Minimum 24h quote volume in USDT; BTC/ETH benchmarks always included")
    parser.add_argument("--max-symbols", type=int, default=0,
                         help="Optional cap; 0 downloads every eligible pair")
    args = parser.parse_args()
    if not 30 <= args.days <= 2000:
        parser.error("--days must be between 30 and 2000")
    if args.min_quote_volume < 0 or args.max_symbols < 0:
        parser.error("volume floor and max symbols cannot be negative")
    exchange = ccxt.bitget({"enableRateLimit": True, "timeout": 30000,
                            "options": {"defaultType": "swap"}})
    out_dir = Path(args.out)
    manifest = {"exchange": "Bitget", "market_type": "USDT perpetual swaps",
                "days_requested": args.days, "timeframes": list(TIMEFRAMES),
                "min_24h_quote_volume_usdt": args.min_quote_volume,
                "max_symbols": args.max_symbols, "downloaded": [], "failed": []}
    try:
        if args.symbols.strip().lower() == "auto":
            markets = discover_usdt_swaps(exchange, args.min_quote_volume, args.max_symbols)
            symbols = [x[0] for x in markets]
            manifest["discovered_markets"] = [
                {"symbol": s, "quote_volume_24h_usdt": round(v, 2), "macro_benchmark": b}
                for s, v, b in markets]
        else:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        print(f"Discovered {len(symbols)} eligible markets. Downloading {', '.join(TIMEFRAMES)}.")
        for idx, symbol in enumerate(symbols, 1):
            print(f"[{idx}/{len(symbols)}] {symbol}")
            result = {"symbol": symbol, "candles": {}}
            try:
                for tf in TIMEFRAMES:
                    result["candles"][tf] = download_one(exchange, symbol, tf, args.days, out_dir)
                manifest["downloaded"].append(result)
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
                manifest["failed"].append(result)
                print(f"DATA INSUFFICIENT / DOWNLOAD FAILED: {symbol}: {exc}")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "download_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"Download complete: {len(manifest['downloaded'])} complete, {len(manifest['failed'])} failed.")
        if not manifest["downloaded"]:
            raise RuntimeError("No complete market datasets downloaded")
    finally:
        close = getattr(exchange, "close", None)
        if close:
            close()


if __name__ == "__main__":
    main()
