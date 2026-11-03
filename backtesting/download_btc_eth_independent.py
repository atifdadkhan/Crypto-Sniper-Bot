"""Download BTC/ETH independently, with explicit per-timeframe history fallback.

No synthetic candles are created. Invalid timeframes are recorded independently;
30m and 2h are derived only from complete source-candle buckets.
"""
import argparse
import json
from pathlib import Path

import ccxt
import pandas as pd

from backtesting.download_bitget import download_one

SYMBOLS = ("BTC/USDT:USDT", "ETH/USDT:USDT")
BASE_TIMEFRAMES = ("15m", "1h", "4h", "1d")


def history_tiers(days: int, minimum: int):
    """Return descending, unique history attempts ending at the requested minimum."""
    values = [days, int(days * 0.75), int(days * 0.5), minimum]
    return list(dict.fromkeys(max(minimum, value) for value in values))


def derive_timeframe(source: Path, destination: Path, rule: str, expected_ms: int):
    frame = pd.read_csv(source)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="raise")
    frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    frame = frame.set_index("timestamp")
    out = frame.resample(rule, origin="epoch", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    source_ms = 900_000 if rule == "30min" else 3_600_000
    required = expected_ms // source_ms
    counts = frame["close"].resample(rule, origin="epoch", label="left", closed="left").count()
    out = out[counts.reindex(out.index).fillna(0) == required]
    if len(out) < 100:
        raise ValueError(f"DATA INSUFFICIENT: only {len(out)} complete derived candles")
    result = out.reset_index()
    destination.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(destination, index=False)
    return {
        "candles": int(len(result)),
        "source": source.name,
        "derived": True,
        "first_timestamp": str(result.timestamp.iloc[0]),
        "last_timestamp": str(result.timestamp.iloc[-1]),
    }


def download_with_fallback(exchange, symbol, timeframe, days, minimum, out_dir):
    folder_name = str(exchange.market(symbol).get("base") or symbol.split("/")[0]).upper()
    destination = Path(out_dir) / f"{folder_name}USDT" / f"{timeframe}.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never let a failed fresh download leave an old CSV looking current.
    destination.unlink(missing_ok=True)
    failures = []
    for tier_days in history_tiers(days, minimum):
        destination.unlink(missing_ok=True)
        try:
            result = download_one(exchange, symbol, timeframe, tier_days, Path(out_dir))
            result["history_days_requested"] = tier_days
            result["history_fallback_used"] = tier_days != days
            return result
        except Exception as exc:
            destination.unlink(missing_ok=True)
            failures.append(f"{tier_days}d: {type(exc).__name__}: {exc}")
            print(f"DOWNLOAD TIER FAILED: {symbol} {timeframe} {failures[-1]}")
    raise RuntimeError("All history tiers failed; " + " | ".join(failures))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--min-history-days", type=int, default=120)
    parser.add_argument("--out", default="data")
    args = parser.parse_args()
    if not 120 <= args.min_history_days <= args.days <= 2000:
        parser.error("require 120 <= --min-history-days <= --days <= 2000")

    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    exchange = ccxt.bitget({"enableRateLimit": True, "timeout": 30000,
                            "options": {"defaultType": "swap"}})
    manifest = {
        "schema_version": 2,
        "exchange": "Bitget",
        "market_type": "linear USDT perpetual swaps",
        "symbols_requested": list(SYMBOLS),
        "requested_history_days": args.days,
        "minimum_history_days": args.min_history_days,
        "timeframes": {},
        "policy": "Each symbol/timeframe is isolated. Failed fresh files are deleted. No candle interpolation or forward filling. Backtests consume only manifest VALID entries and split at gaps.",
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
                    result = download_with_fallback(
                        exchange, symbol, tf, args.days, args.min_history_days, root
                    )
                    manifest["timeframes"][base][tf] = {"status": "VALID", **result}
                except Exception as exc:
                    (folder / f"{tf}.csv").unlink(missing_ok=True)
                    manifest["timeframes"][base][tf] = {
                        "status": "DATA_INSUFFICIENT",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    print(f"TIMEFRAME SKIPPED: {symbol} {tf}: {exc}")

            for tf, source_tf, rule, interval in (
                ("30m", "15m", "30min", 1_800_000),
                ("2h", "1h", "2h", 7_200_000),
            ):
                destination = folder / f"{tf}.csv"
                destination.unlink(missing_ok=True)
                try:
                    source_status = manifest["timeframes"][base].get(source_tf, {}).get("status")
                    source = folder / f"{source_tf}.csv"
                    if source_status != "VALID" or not source.exists():
                        raise ValueError(f"validated source timeframe {source_tf} unavailable")
                    result = derive_timeframe(source, destination, rule, interval)
                    manifest["timeframes"][base][tf] = {"status": "VALID", **result}
                except Exception as exc:
                    destination.unlink(missing_ok=True)
                    manifest["timeframes"][base][tf] = {
                        "status": "DATA_INSUFFICIENT",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    print(f"DERIVED TIMEFRAME SKIPPED: {symbol} {tf}: {exc}")

        manifest_path = root / "timeframe_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
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
