"""Download validated Bitget linear USDT perpetual OHLCV using public endpoints only."""
import argparse
import json
import time
from pathlib import Path

import ccxt
import pandas as pd

from backtesting.data_loader import normalise_ohlcv, validate_timeframe

TIMEFRAMES = ("15m", "1h", "4h", "1d")
# Common tokenised equities, indices and commodities are deliberately excluded.
NON_CRYPTO_BASES = {
    "AAPL", "AMZN", "GOOGL", "GOOG", "META", "MSFT", "NVDA", "TSLA",
    "COIN", "MSTR", "NFLX", "QQQ", "SPY", "IWM", "DIA", "XAU", "XAG",
    "GOLD", "SILVER", "PAXG", "XAUT", "WTI", "BRENT", "OIL",
}


def discover_usdt_swaps(exchange, min_quote_volume=500_000.0, max_symbols=0):
    """Discover active linear USDT-settled crypto swaps meeting a volume floor."""
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()
    selected = []
    for symbol, market in markets.items():
        if not market.get("active", True) or not market.get("swap"):
            continue
        if market.get("linear") is not True:
            continue
        if market.get("quote") != "USDT" or market.get("settle") not in (None, "USDT"):
            continue
        base = str(market.get("base") or "").upper()
        if not base or base in NON_CRYPTO_BASES:
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
        benchmark = base in {"BTC", "ETH"}
        if benchmark or quote_volume >= min_quote_volume:
            selected.append((symbol, quote_volume, benchmark))
    selected.sort(key=lambda x: (not x[2], -x[1], x[0]))
    if max_symbols > 0:
        benchmarks = [x for x in selected if x[2]]
        others = [x for x in selected if not x[2]]
        selected = (benchmarks + others)[:max_symbols]
    return selected


def download_one(exchange, symbol: str, timeframe: str, days: int, out_dir: Path):
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    if not exchange.has.get("fetchOHLCV"):
        raise RuntimeError("Bitget CCXT adapter does not report fetchOHLCV support")
    now_ms = exchange.milliseconds()
    since = now_ms - days * 86_400_000
    cursor = since
    step = exchange.parse_timeframe(timeframe) * 1000
    rows, last_seen = [], None

    while cursor < now_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        newest = int(batch[-1][0])
        if last_seen is not None and newest <= last_seen:
            raise RuntimeError(f"Non-advancing OHLCV pagination for {symbol} {timeframe}")
        last_seen, cursor = newest, newest + step
        if cursor < now_ms:
            time.sleep(max(float(exchange.rateLimit), 100.0) / 1000.0)

    if not rows:
        raise RuntimeError(f"No OHLCV data returned for {symbol} {timeframe}")
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame[frame["timestamp"] + pd.to_timedelta(step, unit="ms") <= pd.Timestamp(now_ms, unit="ms", tz="UTC")]
    frame = normalise_ohlcv(frame)
    validate_timeframe(frame, timeframe, symbol)
    # Ensure the downloaded history begins close to the requested start. New listings
    # naturally have shorter history, but a partial response for an older market fails.
    expected_start = pd.Timestamp(since, unit="ms", tz="UTC")
    actual_start = frame.timestamp.iloc[0]
    if actual_start > expected_start + pd.Timedelta(days=2):
        market_created = exchange.market(symbol).get("created")
        if market_created is None:
            raise RuntimeError(
                f"DATA INSUFFICIENT: first candle {actual_start} is over 2 days later than requested start "
                f"{expected_start}; listing age could not be verified"
            )
        created = pd.Timestamp(market_created, unit="ms", tz="UTC")
        if created + pd.Timedelta(days=2) < actual_start:
            raise RuntimeError(f"DATA INSUFFICIENT: unexpected history truncation for {symbol} {timeframe}")

    folder_name = str(exchange.market(symbol).get("base") or symbol.split("/")[0]).upper()
    folder = out_dir / f"{folder_name}USDT"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{timeframe}.csv"
    frame.to_csv(path, index=False)
    print(f"{symbol} {timeframe}: {len(frame)} validated candles -> {path}")
    return {"candles": len(frame), "first_timestamp": str(frame.timestamp.iloc[0]),
            "last_timestamp": str(frame.timestamp.iloc[-1])}


def main():
    parser = argparse.ArgumentParser(description="Download eligible Bitget USDT perpetual crypto markets")
    parser.add_argument("--symbols", default="auto", help="auto or comma-separated CCXT symbols")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--out", default="data")
    parser.add_argument("--min-quote-volume", type=float, default=500_000)
    parser.add_argument("--max-symbols", type=int, default=0)
    args = parser.parse_args()
    if not 30 <= args.days <= 2000:
        parser.error("--days must be between 30 and 2000")
    if args.min_quote_volume < 0 or args.max_symbols < 0:
        parser.error("volume floor and max symbols cannot be negative")

    exchange = ccxt.bitget({"enableRateLimit": True, "timeout": 30000,
                            "options": {"defaultType": "swap"}})
    out_dir = Path(args.out)
    manifest = {
        "exchange": "Bitget", "market_type": "linear USDT-settled perpetual swaps",
        "days_requested": args.days, "timeframes": list(TIMEFRAMES),
        "min_24h_quote_volume_usdt": args.min_quote_volume, "max_symbols": args.max_symbols,
        "universe_note": "Current active market universe; not point-in-time historical listings. "
                         "Common tokenised equities, indices and commodities are excluded by base-symbol denylist.",
        "downloaded": [], "failed": [],
    }
    try:
        if args.symbols.strip().lower() == "auto":
            markets = discover_usdt_swaps(exchange, args.min_quote_volume, args.max_symbols)
            symbols = [x[0] for x in markets]
            manifest["discovered_markets"] = [
                {"symbol": s, "quote_volume_24h_usdt": round(v, 2), "macro_benchmark": b}
                for s, v, b in markets
            ]
        else:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        print(f"Discovered {len(symbols)} markets. Downloading closed {', '.join(TIMEFRAMES)} candles.")
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
            raise RuntimeError("No complete datasets downloaded; inspect DATA INSUFFICIENT reasons in manifest.")
    finally:
        close = getattr(exchange, "close", None)
        if close:
            close()


if __name__ == "__main__":
    main()
