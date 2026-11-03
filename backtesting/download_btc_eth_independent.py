"""Download BTC and ETH independently by timeframe; preserve every valid timeframe.

The existing strict downloader is reused for each timeframe separately, so a
failure in one timeframe cannot discard another timeframe's validated CSV.
30m and 2h are derived only from validated 15m and 1h source candles.
"""
import json
from pathlib import Path

import pandas as pd
import ccxt

from backtesting.download_bitget import download_one

SYMBOLS = ("BTC/USDT:USDT", "ETH/USDT:USDT")
BASE_TIMEFRAMES = ("15m", "1h", "4h", "1d")


def derive_timeframe(source: Path, destination: Path, rule: str, expected_ms: int):
    frame = pd.read_csv(source)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.set_index("timestamp").sort_index()
    out = frame.resample(rule, origin="epoch", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    # Never accept a partial aggregate: every source candle in the bucket must exist.
    counts = frame["close"].resample(rule, origin="epoch", label="left", closed="left").count()
    required = expected_ms // (900_000 if rule == "30min" else 3_600_000)
    out = out[counts.reindex(out.index).fillna(0) == required]
    if len(out) < 100:
        raise ValueError(f"DATA INSUFFICIENT: only {len(out)} complete derived candles")
    result = out.reset_index()
    result.to_csv(destination, index=False)
    return {"candles": len(result), "source": source.name, "derived": True}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--min-history-days", type=int, default=120)
    parser.add_argument("--out", default="data")
    args = parser.parse_args()
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    exchange = ccxt.bitget({"enableRateLimit": True, "timeout": 30000,
                            "options": {"defaultType": "swap"}})
    manifest = {
        "exchange": "Bitget", "market_type": "linear USDT perpetual",
        "symbols_requested": list(SYMBOLS), "timeframes": {},
        "policy": "Each timeframe is downloaded, validated, and retained independently. Invalid timeframes are recorded and excluded from backtesting.",
    }
    try:
        exchange.load_markets()
        for symbol in SYMBOLS:
            base = exchange.market(symbol)["base"].upper()
            folder = root / f"{base}USDT"
            folder.mkdir(parents=True, exist_ok=True)
            manifest["timeframes"][base] = {}
            for tf in BASE_TIMEFRAMES:
                try:
                    result = download_one(exchange, symbol, tf, args.days, root)
                    manifest["timeframes"][base][tf] = {"status": "VALID", **result}
                except Exception as exc:
                    manifest["timeframes"][base][tf] = {
                        "status": "DATA_INSUFFICIENT",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    print(f"TIMEFRAME SKIPPED: {symbol} {tf}: {exc}")
            # Derived intervals are created only from complete validated sources.
            for tf, source_tf, rule, interval in (
                ("30m", "15m", "30min", 1_800_000),
                ("2h", "1h", "2h", 7_200_000),
            ):
                source = folder / f"{source_tf}.csv"
                destination = folder / f"{tf}.csv"
                try:
                    if source.exists():
                        result = derive_timeframe(source, destination, rule, interval)
                        manifest["timeframes"][base][tf] = {"status": "VALID", **result}
                    else:
                        raise ValueError(f"validated source timeframe {source_tf} unavailable")
                except Exception as exc:
                    manifest["timeframes"][base][tf] = {
                        "status": "DATA_INSUFFICIENT",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    print(f"DERIVED TIMEFRAME SKIPPED: {symbol} {tf}: {exc}")
        (root / "timeframe_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(json.dumps(manifest, indent=2))
        valid_count = sum(
            item.get("status") == "VALID"
            for symbol_data in manifest["timeframes"].values()
            for item in symbol_data.values()
        )
        if valid_count == 0:
            raise RuntimeError("No valid BTC/ETH timeframe datasets downloaded")
    finally:
        close = getattr(exchange, "close", None)
        if close:
            close()


if __name__ == "__main__":
    main()
