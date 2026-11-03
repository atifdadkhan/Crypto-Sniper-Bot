"""Download validated Bitget linear USDT perpetual OHLCV using public endpoints only."""
import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

from backtesting.data_loader import normalise_ohlcv, validate_timeframe

TIMEFRAMES = ("15m", "1h", "4h", "1d")


def history_fallback_tiers(requested_days, minimum_days):
    """Return descending, explicit lookback tiers without ever crossing the minimum."""
    if requested_days < 30 or minimum_days < 30 or minimum_days > requested_days:
        raise ValueError("history days must satisfy 30 <= minimum_days <= requested_days")
    tiers = {int(requested_days), int(minimum_days)}
    tiers.update(day for day in (365, 180, 90, 60, 30)
                 if minimum_days <= day <= requested_days)
    return sorted(tiers, reverse=True)


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


def discover_usdt_swaps(exchange, min_quote_volume=500_000.0, max_symbols=0, absolute_min_quote_volume=None):
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
        # Prefer Bitget's explicit USDT turnover field for USDT-settled swaps.
        quote_volume = ticker.get("usdtVolume")
        if quote_volume is None:
            info = ticker.get("info") or {}
            quote_volume = info.get("usdtVolume") if isinstance(info, dict) else None
        if quote_volume is None:
            quote_volume = ticker.get("quoteVolume")
        if quote_volume is None:
            base_volume, last = ticker.get("baseVolume"), ticker.get("last")
            quote_volume = (base_volume * last) if base_volume and last else 0
        try:
            quote_volume = float(quote_volume or 0)
        except (TypeError, ValueError):
            quote_volume = 0.0
        benchmark = base in {"BTC", "ETH"}
        hard_floor = min_quote_volume if absolute_min_quote_volume is None else absolute_min_quote_volume
        if benchmark or quote_volume >= hard_floor:
            selected.append((symbol, quote_volume, benchmark))
    selected.sort(key=lambda x: (not x[2], -x[1], x[0]))
    # Keep all ranked candidates. The target count is enforced only after each
    # candidate has passed complete historical coverage validation.
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


def validate_requested_coverage(frame, symbol, timeframe, expected_start, step_ms):
    """Require the requested lookback; recent listings cannot substitute for history."""
    actual_start = frame.timestamp.iloc[0]
    tolerance = pd.to_timedelta(step_ms, unit="ms")
    if actual_start > expected_start + tolerance:
        raise RuntimeError(
            f"DATA INSUFFICIENT: {symbol} {timeframe} begins at {actual_start}; "
            f"requested coverage begins at {expected_start}"
        )


def fetch_historical_ohlcv(exchange, symbol, timeframe, since_ms, until_ms, max_attempts=3):
    """Fetch closed OHLCV in deterministic <=200-candle windows from Bitget history API.

    Bitget's history-candles endpoint supports up to 90 days per request and
    returns at most 200 rows. Fixed timestamp windows avoid relying on CCXT's
    recent-candle pagination, which can silently cap the oldest returned bar.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    market = exchange.market(symbol)
    granularity = {"15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}[timeframe]
    step_ms = int(exchange.parse_timeframe(timeframe) * 1000)
    max_window_ms = min(200 * step_ms, 90 * 86_400_000)
    cursor = (int(since_ms) // step_ms) * step_ms
    until_ms = int(until_ms)
    rows = []
    request_count = 0

    while cursor < until_ms:
        window_end = min(cursor + max_window_ms, until_ms)
        params = {
            "symbol": market["id"],
            "productType": "USDT-FUTURES",
            "granularity": granularity,
            "startTime": str(cursor),
            "endTime": str(window_end - 1),
            "limit": "200",
        }
        last_error = None
        response = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = exchange.publicMixGetV2MixMarketHistoryCandles(params)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < max_attempts:
                    time.sleep(max(float(getattr(exchange, "rateLimit", 100)), 250.0) * attempt / 1000.0)
        if last_error is not None:
            raise RuntimeError(
                f"Bitget history-candles failed for {symbol} {timeframe} "
                f"window {cursor}..{window_end}: {last_error}"
            ) from last_error

        data = response.get("data", []) if isinstance(response, dict) else []
        for row in data or []:
            if not row or len(row) < 6:
                continue
            stamp = int(row[0])
            if cursor <= stamp < window_end:
                rows.append(row[:6])
        request_count += 1
        cursor = window_end
        if cursor < until_ms:
            time.sleep(max(float(getattr(exchange, "rateLimit", 100)), 50.0) / 1000.0)

    if not rows:
        raise RuntimeError(f"No historical OHLCV data returned for {symbol} {timeframe}")
    # Keep diagnostics useful without flooding Actions logs.
    print(f"Bitget history API: {symbol} {timeframe}, {request_count} windows, {len(rows)} raw candles")
    return rows


def download_one(exchange, symbol: str, timeframe: str, days: int, out_dir: Path):
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    now_ms = exchange.milliseconds()
    since = now_ms - days * 86_400_000
    step = exchange.parse_timeframe(timeframe) * 1000

    # Use Bitget's dedicated historical endpoint for the entire lookback.
    # Its request window is capped at 90 days and response page at 200 candles;
    # fetch_historical_ohlcv pages by exact time windows to cover long histories.
    rows = fetch_historical_ohlcv(exchange, symbol, timeframe, since, now_ms)
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame[
        frame["timestamp"] + pd.to_timedelta(step, unit="ms")
        <= pd.Timestamp(now_ms, unit="ms", tz="UTC")
    ]
    frame = normalise_ohlcv(frame)
    before = len(frame)
    frame = recover_missing_candles(exchange, symbol, timeframe, frame, step)
    if len(frame) > before:
        print(f"Recovered {len(frame) - before} missing {timeframe} candle(s) for {symbol}")

    # Only 15m can be segmented safely by the simulator. Higher timeframes stay strict.
    validate_timeframe(frame, timeframe, symbol, allow_gaps=(timeframe == "15m"))

    expected_start = pd.Timestamp((since // step) * step, unit="ms", tz="UTC")
    actual_last_close = frame.timestamp.iloc[-1] + pd.to_timedelta(step, unit="ms")
    if pd.Timestamp(now_ms, unit="ms", tz="UTC") - actual_last_close > pd.to_timedelta(step * 2, unit="ms"):
        raise RuntimeError(f"DATA INSUFFICIENT: latest {timeframe} candle is stale for {symbol}: {actual_last_close}")
    validate_requested_coverage(frame, symbol, timeframe, expected_start, step)

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
    parser.add_argument("--min-quote-volume", type=float, default=500_000,
                        help="Preferred 24h USDT turnover; higher-turnover markets rank first")
    parser.add_argument("--absolute-min-quote-volume", type=float, default=100_000,
                        help="Hard USDT turnover floor; never relaxed below this")
    parser.add_argument("--min-history-days", type=int, default=90,
                        help="Minimum acceptable per-market history when requested lookback is unavailable")
    parser.add_argument("--max-symbols", type=int, default=20,
                        help="Target fully eligible markets; 0 means all eligible markets")
    args = parser.parse_args()
    if not 30 <= args.days <= 2000:
        parser.error("--days must be between 30 and 2000")
    if (args.min_quote_volume < 0 or args.absolute_min_quote_volume < 0
            or args.absolute_min_quote_volume > args.min_quote_volume or args.max_symbols < 0):
        parser.error("volume floors must be non-negative, absolute floor <= preferred floor, and max symbols >= 0")
    if args.min_history_days < 30:
        parser.error("--min-history-days must be >= 30")
    if args.min_history_days > args.days:
        print(f"WARNING: min history {args.min_history_days}d exceeds requested {args.days}d; clamping minimum to {args.days}d")
        args.min_history_days = args.days

    exchange = ccxt.bitget({"enableRateLimit": True, "timeout": 30000,
                            "options": {"defaultType": "swap"}})
    out_dir = Path(args.out)
    manifest = {
        "exchange": "Bitget", "market_type": "linear USDT-settled perpetual swaps",
        "days_requested": args.days, "timeframes": list(TIMEFRAMES),
        "preferred_24h_turnover_usdt": args.min_quote_volume,
        "absolute_min_24h_turnover_usdt": args.absolute_min_quote_volume,
        "requested_history_days": args.days,
        "minimum_history_days": args.min_history_days,
        "eligible_symbols_target": args.max_symbols,
        "selection_policy": "rank by Bitget USDT turnover; use lower-turnover candidates only down to the absolute floor",
        "coverage_policy": "per-market history is reduced only through explicit tiers; no fabricated candles",
        "comparison_warning": "Per-market lookback may differ; compare markets on matched date windows before cross-market conclusions.",
        "universe_note": "Current active market universe; not point-in-time historical listings. "
                         "Known tokenised equities, indices and commodities are excluded using a maintained "
                         "base-symbol denylist. This is a heuristic, not a guarantee that every underlying is crypto.",
        "non_crypto_filter": "heuristic_base_symbol_denylist",
        "downloaded": [], "failed": [],
    }
    try:
        if args.symbols.strip().lower() == "auto":
            markets = discover_usdt_swaps(exchange, args.min_quote_volume, args.max_symbols,
                                           absolute_min_quote_volume=args.absolute_min_quote_volume)
            symbols = [x[0] for x in markets]
            manifest["discovered_markets"] = [
                {"symbol": s, "quote_volume_24h_usdt": round(v, 2), "macro_benchmark": b}
                for s, v, b in markets
            ]
        else:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        out_dir.mkdir(parents=True, exist_ok=True)
        print(
            f"Discovered {len(symbols)} candidates. Target: "
            f"{args.max_symbols if args.max_symbols else 'ALL eligible'} complete datasets; "
            f"lookback: {args.days} days; timeframes: {', '.join(TIMEFRAMES)}."
        )
        for idx, symbol in enumerate(symbols, 1):
            if args.max_symbols > 0 and len(manifest["downloaded"]) >= args.max_symbols:
                break
            print(
                f"Candidate [{idx}/{len(symbols)}] {symbol}; eligible "
                f"{len(manifest['downloaded'])}/{args.max_symbols if args.max_symbols else 'ALL'}"
            )
            result = {"symbol": symbol, "candles": {}, "lookback_days": None}
            tiers = history_fallback_tiers(args.days, args.min_history_days)
            last_error = None
            accepted = False
            for lookback_days in tiers:
                try:
                    # Stage each tier independently; failed attempts never publish partial data.
                    with tempfile.TemporaryDirectory(prefix="bitget_symbol_", dir=out_dir) as tmp_dir:
                        tier_results = {}
                        for tf in TIMEFRAMES:
                            tier_results[tf] = download_one(
                                exchange, symbol, tf, lookback_days, Path(tmp_dir)
                            )
                        folder_name = str(
                            exchange.market(symbol).get("base") or symbol.split("/")[0]
                        ).upper() + "USDT"
                        staged_folder = Path(tmp_dir) / folder_name
                        destination = out_dir / folder_name
                        if destination.exists():
                            shutil.rmtree(destination)
                        shutil.move(str(staged_folder), str(destination))
                    result["candles"] = tier_results
                    result["lookback_days"] = lookback_days
                    accepted = True
                    if lookback_days < args.days:
                        print(f"ADAPTIVE HISTORY: {symbol} accepted at {lookback_days}d (requested {args.days}d)")
                    break
                except Exception as exc:
                    last_error = exc
                    message = str(exc).lower()
                    recoverable = (
                        "data insufficient" in message or "timestamp gap" in message
                        or "stale" in message or "requested coverage" in message
                    )
                    if not recoverable or lookback_days == tiers[-1]:
                        break
                    print(f"RETRY {symbol}: {lookback_days}d unavailable; trying next history tier")
            if accepted:
                manifest["downloaded"].append(result)
                print(f"ELIGIBLE: {symbol} ({len(manifest['downloaded'])}/{args.max_symbols or 'ALL'})")
            else:
                result["error"] = f"{type(last_error).__name__}: {last_error}" if last_error else "No eligible history tier"
                manifest["failed"].append(result)
                print(f"SKIP INELIGIBLE MARKET: {symbol}: {result['error']}")
        manifest["candidate_count"] = len(symbols)
        manifest["candidates_attempted"] = len(manifest["downloaded"]) + len(manifest["failed"])
        manifest["eligible_count"] = len(manifest["downloaded"])
        (out_dir / "download_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(
            f"Download complete: {len(manifest['downloaded'])} eligible, "
            f"{len(manifest['failed'])} skipped from {manifest['candidates_attempted']} attempted."
        )
        if not manifest["downloaded"]:
            raise RuntimeError("No complete datasets downloaded; inspect DATA INSUFFICIENT reasons in manifest.")
        if args.max_symbols > 0 and len(manifest["downloaded"]) < args.max_symbols:
            print(
                f"WARNING: only {len(manifest['downloaded'])}/{args.max_symbols} markets "
                f"qualified above the {args.min_history_days}-day history and "
                f"${args.absolute_min_quote_volume:,.0f} USDT turnover floors. "
                "Proceeding with every validated market; no low-quality data was forced in."
            )
        downloaded_bases = {
            str(exchange.market(item["symbol"]).get("base") or item["symbol"].split("/")[0]).upper()
            for item in manifest["downloaded"]
        }
        if not {"BTC", "ETH"}.issubset(downloaded_bases):
            raise RuntimeError(
                "BTC and ETH benchmark datasets are mandatory for the current strategy; "
                f"validated benchmarks: {sorted(downloaded_bases.intersection({'BTC', 'ETH'}))}. "
                "Inspect download_manifest.json."
            )
    finally:
        close = getattr(exchange, "close", None)
        if close:
            close()


if __name__ == "__main__":
    main()
