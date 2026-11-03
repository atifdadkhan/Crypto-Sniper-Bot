"""Run the preserved strategy engines across every validated downloaded market.

Input data is public Bitget linear USDT perpetual OHLCV. No orders are sent.
30m and 2h are derived only from complete 15m/1h source buckets.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.data_loader import load_file, validate_timeframe
from backtesting.multi_strategy import (
    STRATEGIES, INTERVAL_MS, run_engine_segment, metrics, contiguous_segments,
)

BASE_TIMEFRAMES = ("15m", "1h", "4h", "1d")
ALL_TIMEFRAMES = ("15m", "30m", "1h", "2h", "4h", "1d")
DERIVATIONS = {"30m": ("15m", "30min", 2), "2h": ("1h", "2h", 2)}


def derive_complete(frame, rule, source_count):
    """Aggregate only buckets containing every expected source candle."""
    source = frame.copy()
    source["timestamp"] = pd.to_datetime(source["timestamp"], utc=True, errors="raise")
    indexed = source.sort_values("timestamp").drop_duplicates("timestamp").set_index("timestamp")
    grouped = indexed.resample(rule, origin="epoch", label="left", closed="left")
    out = grouped.agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    })
    counts = grouped["close"].count()
    out = out[counts == source_count].dropna().reset_index()
    return out


def load_market_timeframes(folder):
    frames, statuses = {}, {}
    for tf in BASE_TIMEFRAMES:
        path = folder / f"{tf}.csv"
        if not path.exists():
            statuses[tf] = {"status": "DATA INSUFFICIENT", "reason": "file missing"}
            continue
        try:
            frame = load_file(path)
            validate_timeframe(frame, tf, folder.name, allow_gaps=True)
            frames[tf] = frame
            statuses[tf] = {"status": "VALID", "candles": len(frame)}
        except Exception as exc:
            statuses[tf] = {"status": "DATA INSUFFICIENT", "reason": str(exc)}

    for tf, (source_tf, rule, count) in DERIVATIONS.items():
        if source_tf not in frames:
            statuses[tf] = {"status": "DATA INSUFFICIENT", "reason": f"source {source_tf} unavailable"}
            continue
        try:
            frame = derive_complete(frames[source_tf], rule, count)
            validate_timeframe(frame, tf, folder.name, allow_gaps=True)
            if len(frame) < 120:
                raise ValueError(f"only {len(frame)} complete derived candles")
            frames[tf] = frame
            statuses[tf] = {
                "status": "VALID", "candles": len(frame),
                "derived_from": source_tf, "complete_source_bars_per_candle": count,
            }
        except Exception as exc:
            statuses[tf] = {"status": "DATA INSUFFICIENT", "reason": str(exc)}
    return frames, statuses


def main():
    parser = argparse.ArgumentParser(description="Full-market Bitget USDT perpetual strategy research")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--fee-slippage-pct", type=float, default=0.12)
    parser.add_argument("--minimum-trades", type=int, default=30)
    args = parser.parse_args()
    if not np.isfinite(args.fee_slippage_pct) or args.fee_slippage_pct < 0:
        parser.error("fee-slippage-pct must be finite and non-negative")
    if args.minimum_trades < 1:
        parser.error("minimum-trades must be >= 1")

    root = Path(args.data_dir)
    manifest_path = root / "download_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("DATA INSUFFICIENT: download_manifest.json is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    market_type = str(manifest.get("market_type", "")).lower()
    if "usdt" not in market_type or "perpetual" not in market_type:
        raise RuntimeError(f"Refusing unexpected market dataset: {market_type!r}")

    summary, trade_frames = {}, []
    market_folders = sorted(p for p in root.iterdir() if p.is_dir() and p.name.endswith("USDT"))
    for folder in market_folders:
        symbol = folder.name
        frames, tf_status = load_market_timeframes(folder)
        summary[symbol] = {"data": tf_status, "strategies": {}}
        for tf in ALL_TIMEFRAMES:
            if tf not in frames:
                continue
            frame = frames[tf]
            segments = contiguous_segments(frame, tf)
            for engine in STRATEGIES:
                pieces, segments_tested = [], 0
                for segment in segments:
                    if len(segment) < 120:
                        continue
                    segments_tested += 1
                    result = run_engine_segment(
                        symbol, tf, segment, engine, args.fee_slippage_pct
                    )
                    if not result.empty:
                        pieces.append(result)
                trades = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
                result_metrics = metrics(trades)
                result_metrics["sample_status"] = (
                    "SAMPLE_SUFFICIENT_FOR_SCREENING"
                    if result_metrics.get("trades", 0) >= args.minimum_trades
                    else "DATA INSUFFICIENT"
                )
                result_metrics["segments_tested"] = segments_tested
                summary[symbol]["strategies"].setdefault(tf, {})[engine] = result_metrics
                if not trades.empty:
                    trade_frames.append(trades)

    output = Path("backtest_results")
    output.mkdir(parents=True, exist_ok=True)
    trades_all = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    trades_all.to_csv(output / "full_market_strategy_trades.csv", index=False)
    (output / "full_market_strategy_summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8"
    )

    rows = []
    for symbol, data in summary.items():
        for tf, engines in data["strategies"].items():
            for engine, result in engines.items():
                rows.append({
                    "symbol": symbol, "timeframe": tf, "strategy": engine,
                    "trades": result.get("trades", 0),
                    "sample_status": result.get("sample_status"),
                    "win_rate_pct": result.get("win_rate_pct"),
                    "profit_factor": result.get("profit_factor"),
                    "net_r": result.get("net_r"),
                    "max_drawdown_r": result.get("max_drawdown_r"),
                })
    leaderboard = pd.DataFrame(rows)
    if not leaderboard.empty:
        leaderboard = leaderboard.sort_values(
            ["sample_status", "profit_factor", "net_r"],
            ascending=[True, False, False], na_position="last",
        )
    leaderboard.to_csv(output / "full_market_strategy_leaderboard.csv", index=False)
    print(f"Markets discovered in data directory: {len(market_folders)}")
    print(f"Markets with strategy rows: {len(summary)}")
    print(f"Trade rows: {len(trades_all)}")
    print(leaderboard.head(100).to_string(index=False) if not leaderboard.empty else "No qualifying strategy trades.")
    print("Research only: estimated fees/slippage included; historical funding is not yet applied.")


if __name__ == "__main__":
    main()
