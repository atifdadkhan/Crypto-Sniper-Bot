"""Modular Binance public OHLCV research downloader for Spot and USD-M perpetuals.

Research only: public market data, no API keys, no order/trading endpoints.
"""
import argparse
import json
import shutil
import time
from pathlib import Path

import ccxt
import pandas as pd

TIMEFRAMES = ("15m", "1h", "4h", "1d")
TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
MIN_CANDLES = 120


def market_universe(exchange, market_type, min_quote_volume):
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()
    selected = []
    for symbol, market in markets.items():
        if not market.get("active") or market.get("quote") != "USDT":
            continue
        if market_type == "spot":
            if not market.get("spot") or market.get("contract") or market.get("base") == "USDT":
                continue
        else:
            if not (market.get("swap") and market.get("linear") and market.get("settle") == "USDT"):
                continue
        base = str(market.get("base") or "").upper()
        if not base or any(tag in base for tag in ("UP", "DOWN", "BULL", "BEAR")):
            continue
        ticker = tickers.get(symbol) or {}
        quote_volume = ticker.get("quoteVolume")
        if quote_volume is None:
            info = ticker.get("info") or {}
            quote_volume = info.get("quoteVolume") or info.get("turnover")
        try:
            quote_volume = float(quote_volume)
        except (TypeError, ValueError):
            continue
        if quote_volume >= min_quote_volume:
            selected.append((symbol, quote_volume))
    selected.sort(key=lambda item: item[1], reverse=True)
    return selected


def fetch_history(exchange, symbol, timeframe, days):
    step = TF_MS[timeframe]
    now = exchange.milliseconds()
    since = now - days * 86_400_000
    cursor = (since // step) * step
    rows_by_ts = {}
    while cursor < now:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        if not batch:
            break
        newest = 0
        for row in batch:
            stamp = int(row[0])
            if stamp + step <= now:
                rows_by_ts[stamp] = row
                newest = max(newest, stamp)
        if newest <= cursor:
            break
        cursor = newest + step
        time.sleep(max(exchange.rateLimit, 50) / 1000)
    if not rows_by_ts:
        raise RuntimeError("DATA INSUFFICIENT: provider returned no candles")
    frame = pd.DataFrame([rows_by_ts[k] for k in sorted(rows_by_ts)],
                         columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame.pop("timestamp_ms"), unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna().drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    valid = (frame[["open", "high", "low", "close"]] > 0).all(axis=1) & (frame["volume"] >= 0)
    valid &= frame["high"] >= frame[["open", "close", "low"]].max(axis=1)
    valid &= frame["low"] <= frame[["open", "close", "high"]].min(axis=1)
    frame = frame.loc[valid, ["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    if len(frame) < MIN_CANDLES:
        raise RuntimeError(f"DATA INSUFFICIENT: only {len(frame)} {timeframe} candles")
    expected_start = pd.Timestamp((since // step) * step, unit="ms", tz="UTC")
    if frame.timestamp.iloc[0] > expected_start + pd.Timedelta(days=max(3, days * 0.15)):
        raise RuntimeError(f"DATA INSUFFICIENT: requested history coverage not met ({frame.timestamp.iloc[0]})")
    last_close = frame.timestamp.iloc[-1] + pd.to_timedelta(step, unit="ms")
    if pd.Timestamp.now(tz="UTC") - last_close > pd.to_timedelta(step * 2, unit="ms"):
        raise RuntimeError(f"DATA INSUFFICIENT: latest {timeframe} candle is stale ({last_close})")
    stamps = pd.DatetimeIndex(frame.timestamp).asi8
    gaps = int(((stamps[1:] - stamps[:-1]) != step * 1_000_000).sum())
    return frame, gaps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-type", choices=("spot", "usdm"), required=True)
    parser.add_argument("--symbols", required=True, help="Comma-separated CCXT symbols for this batch")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if not 120 <= args.days <= 2000:
        parser.error("--days must be between 120 and 2000")
    exchange = ccxt.binance({"enableRateLimit": True, "timeout": 30000,
                             "options": {"defaultType": "spot" if args.market_type == "spot" else "future"}})
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "exchange": "Binance",
        "market_type": "spot cash" if args.market_type == "spot" else "USDT-M linear perpetual",
        "side_policy": "LONG_ONLY" if args.market_type == "spot" else "LONG_AND_SHORT",
        "days_requested": args.days, "timeframes": list(TIMEFRAMES),
        "symbols_requested": [s.strip() for s in args.symbols.split(",") if s.strip()],
        "downloaded": [], "failed": [],
        "data_policy": "No synthetic candles; gaps are recorded and simulations must segment at gaps.",
    }
    try:
        for symbol in manifest["symbols_requested"]:
            item = {"symbol": symbol, "candles": {}}
            try:
                market = exchange.market(symbol)
                folder_name = str(market.get("base") or symbol.split("/")[0]).upper() + "USDT"
                folder = root / folder_name
                staged = root / f".staging_{folder_name}"
                if staged.exists():
                    shutil.rmtree(staged)
                staged.mkdir(parents=True, exist_ok=True)
                try:
                    for tf in TIMEFRAMES:
                        frame, gaps = fetch_history(exchange, symbol, tf, args.days)
                        frame.to_csv(staged / f"{tf}.csv", index=False)
                        item["candles"][tf] = {
                            "count": len(frame), "gap_count": gaps,
                            "first_timestamp": str(frame.timestamp.iloc[0]),
                            "last_timestamp": str(frame.timestamp.iloc[-1]),
                        }
                    if folder.exists():
                        shutil.rmtree(folder)
                    staged.replace(folder)
                except Exception:
                    shutil.rmtree(staged, ignore_errors=True)
                    raise
                manifest["downloaded"].append(item)
                print(f"VALIDATED {symbol}: all four timeframes")
            except Exception as exc:
                item["error"] = f"{type(exc).__name__}: {exc}"
                manifest["failed"].append(item)
                print(f"DATA INSUFFICIENT {symbol}: {item['error']}")
        manifest["eligible_count"] = len(manifest["downloaded"])
        manifest["failed_count"] = len(manifest["failed"])
        (root / "download_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if not manifest["downloaded"]:
            raise RuntimeError("No complete Binance datasets in this batch; inspect manifest.")
    finally:
        close = getattr(exchange, "close", None)
        if close:
            close()


if __name__ == "__main__":
    main()
