"""Download validated Bitget linear USDT perpetual OHLCV using public endpoints only."""
import argparse
import json
import time
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

from backtesting.data_loader import normalise_ohlcv, validate_timeframe

TIMEFRAMES = ("15m", "1h", "4h", "1d")
# Bitget lists some tokenised equities, indices and commodities as linear swaps.
# This denylist is intentionally conservative and must be maintained as listings change.
NON_CRYPTO_BASES = {
    "AAPL", "AMZN", "GOOGL", "GOOG", "META", "MSFT", "NVDA", "TSLA", "TSL",
    "COIN", "MSTR", "NFLX", "QQQ", "SPY", "IWM", "DIA", "SOXL", "SOXS",
    "SNDK", "SKHY", "SKHYNIX", "SNXX", "MU", "INTC", "AMD", "AVGO", "ORCL",
    "CRM", "PLTR", "BABA", "JD", "PDD", "NIO", "TSM", "ARM", "SMCI",
    "XAU", "XAG", "GOLD", "SILVER", "PAXG", "XAUT", "WTI", "BRENT", "OIL",
    "CL", "BZ", "HG", "NG", "COPPER", "NATGAS", "USOIL", "UKOIL",
    "SPX", "NDX", "DJI", "US30", "US100", "US500", "DXY", "EUR", "JPY",
    "KORU", "SPCX", "MRVL", "CRCL", "DRAM", "OPENAI", "TQQQ",
}


def is_excluded_non_crypto_base(base):
    """Heuristic exclusion for known tokenised non-crypto underlyings."""
    value = str(base or "").strip().upper()
    if not value:
        return True
    return value in NON_CRYPTO_BASES


def discover_usdt_swaps(exchange, min_quote_volume=500_000.0, max_symbols=0):
    """Discover active linear USDT-settled swaps, excluding known non-crypto underlyings."""
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
        if is_excluded_non_crypto_base(base):
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


def _fetch_exact_history_candle(exchange, symbol, timeframe, timestamp_ms, step_ms):
    """Use Bitget's dedicated history-candles endpoint as an exact-gap fallback."""
    market = exchange.market(symbol)
    granularity = {"15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}[timeframe]
    params = {
        "symbol": market["id"],
        "productType": "USDT-FUTURES",
        "granularity": granularity,
        "startTime": str(timestamp_ms),
        "endTime": str(timestamp_ms + step_ms - 1),
        "limit": "10",
    }
    response = exchange.publicMixGetV2MixMarketHistoryCandles(params)
    rows = response.get("data", []) if isinstance(response, dict) else []
    return [row[:6] for row in rows if row and int(row[0]) == int(timestamp_ms) and len(row) >= 6]


def recover_missing_candles(exchange, symbol, timeframe, frame, step_ms, max_attempts=2):
    """Recover exact missing opens via current and historical endpoints; never synthesize OHLCV."""
    frame = frame.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
    diagnostics = []
    for attempt in range(1, max_attempts + 1):
        values = pd.DatetimeIndex(frame["timestamp"]).asi8 // 1_000_000
        if len(values) < 2:
            break
        missing = []
        for left, right in zip(values[:-1], values[1:]):
            if right - left > step_ms:
                missing.extend(range(int(left + step_ms), int(right), int(step_ms)))
        if not missing:
            return frame
        additions = []
        for timestamp_ms in missing:
            recovered = []
            try:
                batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=timestamp_ms, limit=10)
                recovered = [row for row in (batch or []) if int(row[0]) == timestamp_ms]
                diagnostics.append({
                    "timestamp_ms": timestamp_ms, "attempt": attempt,
                    "source": "fetch_ohlcv", "rows_at_exact_timestamp": len(recovered),
                })
            except Exception as exc:
                diagnostics.append({
                    "timestamp_ms": timestamp_ms, "attempt": attempt,
                    "source": "fetch_ohlcv", "error": str(exc),
                })
            if not recovered:
                try:
                    recovered = _fetch_exact_history_candle(exchange, symbol, timeframe, timestamp_ms, step_ms)
                    diagnostics.append({
                        "timestamp_ms": timestamp_ms, "attempt": attempt,
                        "source": "history-candles", "rows_at_exact_timestamp": len(recovered),
                    })
                except Exception as exc:
                    diagnostics.append({
                        "timestamp_ms": timestamp_ms, "attempt": attempt,
                        "source": "history-candles", "error": str(exc),
                    })
            additions.extend(recovered)
            time.sleep(max(float(getattr(exchange, "rateLimit", 100)), 100.0) / 1000.0)
        if not additions:
            # A provider may transiently omit a candle. Retry the exact timestamp
            # on the next attempt before declaring the gap unrecoverable.
            if attempt < max_attempts:
                time.sleep(max(float(getattr(exchange, "rateLimit", 100)), 250.0) / 1000.0)
                continue
            break
        extra = pd.DataFrame(additions, columns=["timestamp", "open", "high", "low", "close", "volume"])
        extra["timestamp"] = pd.to_datetime(extra["timestamp"], unit="ms", utc=True)
        frame = pd.concat([frame, extra], ignore_index=True)
        frame = normalise_ohlcv(frame, min_candles=1)
    if diagnostics:
        print(f"Gap recovery diagnostics for {symbol} {timeframe}: "
              + json.dumps(diagnostics, separators=(",", ":")))
    return frame


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
    before = len(frame)
    frame = recover_missing_candles(exchange, symbol, timeframe, frame, step)
    if len(frame) > before:
        print(f"Recovered {len(frame) - before} missing {timeframe} candle(s) for {symbol}")
    try:
        # Preserve strict validation for all timeframes except 15m. The backtest
        # can safely use continuous 15m segments without ever trading across a gap.
        validate_timeframe(frame, timeframe, symbol,
                           allow_gaps=(timeframe == "15m"))
    except ValueError:
        raise

    expected_start = pd.Timestamp(since, unit="ms", tz="UTC")
    actual_start = frame.timestamp.iloc[0]
    actual_last_close = frame.timestamp.iloc[-1] + pd.to_timedelta(step, unit="ms")
    if pd.Timestamp(now_ms, unit="ms", tz="UTC") - actual_last_close > pd.to_timedelta(step * 2, unit="ms"):
        raise RuntimeError(f"DATA INSUFFICIENT: latest {timeframe} candle is stale for {symbol}: {actual_last_close}")
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
    gaps = int((pd.DatetimeIndex(frame.timestamp).asi8[1:] -
                pd.DatetimeIndex(frame.timestamp).asi8[:-1] != step * 1_000_000).sum())
    status = "validated" if gaps == 0 else f"validated with {gaps} gap(s); backtest will segment"
    print(f"{symbol} {timeframe}: {len(frame)} candles {status} -> {path}")
    return {"candles": len(frame), "gap_count": gaps,
            "first_timestamp": str(frame.timestamp.iloc[0]),
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
                         "Known tokenised equities, indices and commodities are excluded using a maintained "
                         "base-symbol denylist. This is a heuristic, not a guarantee that every underlying is crypto.",
        "non_crypto_filter": "heuristic_base_symbol_denylist",
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
