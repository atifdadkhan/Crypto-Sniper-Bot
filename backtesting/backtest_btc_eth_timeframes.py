"""Independent timeframe research backtest for BTC/ETH.

This is a timeframe-isolated research variant, NOT the live multi-timeframe
15m strategy. It uses only manifest-approved data, aligns BTC/ETH context by
completed-candle timestamps, resets indicators after data gaps, and never
simulates a position across a missing candle.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from indicators import enrich, trend
from strategy import _candidate
from backtesting.backtest import simulate, summarize
from backtesting.data_loader import load_file

TIMEFRAMES = ("15m", "30m", "1h", "2h", "4h", "1d")
TF_MS = {
    "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
    "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}
MAX_HOLD_MS = 86_400_000


def split_contiguous(frame, timeframe):
    """Split a validated OHLCV frame at gaps; never stitch missing bars."""
    if frame.empty:
        return []
    stamps = pd.DatetimeIndex(frame.timestamp)
    if stamps.tz is None:
        raise ValueError("timestamps must be timezone-aware")
    interval = TF_MS[timeframe]
    values = stamps.asi8 // 1_000_000
    if np.any(values % interval):
        raise ValueError(f"timestamps are not aligned to UTC {timeframe} boundaries")
    cuts = (np.flatnonzero(np.diff(values) != interval) + 1).tolist()
    return [part.reset_index(drop=True) for part in np.split(frame, cuts) if len(part)]


def prepare_benchmark(frame, timeframe):
    """Enrich each continuous benchmark segment independently."""
    prepared = []
    for segment in split_contiguous(frame, timeframe):
        if len(segment) < 100:
            continue
        enriched = enrich(segment)
        close_ns = (pd.DatetimeIndex(enriched.timestamp) +
                    pd.to_timedelta(TF_MS[timeframe], unit="ms")).asi8
        prepared.extend((int(ns), row) for ns, (_, row) in zip(close_ns, enriched.iterrows()))
    prepared.sort(key=lambda item: item[0])
    return prepared


def benchmark_trend_at(prepared, at_ns, max_age_ms):
    closes = [item[0] for item in prepared]
    idx = int(np.searchsorted(closes, at_ns, side="right") - 1)
    if idx < 0:
        return None
    close_ns, row = prepared[idx]
    # Do not silently reuse stale context after a missing benchmark candle.
    if at_ns - close_ns > max_age_ms:
        return None
    return trend(row)


def run_segment(symbol, timeframe, frame, btc_prepared, eth_prepared,
                fee, funding, max_hold_bars):
    enriched = enrich(frame)
    trades, next_signal = [], 100
    interval = TF_MS[timeframe]
    for i in range(100, len(enriched) - 1):
        if i < next_signal:
            continue
        row = enriched.iloc[i]
        signal_close_ns = int(pd.Timestamp(row.timestamp).value + interval * 1_000_000)
        btc_trend = benchmark_trend_at(btc_prepared, signal_close_ns, interval)
        eth_trend = benchmark_trend_at(eth_prepared, signal_close_ns, interval)
        if btc_trend is None or eth_trend is None:
            continue

        candidates = []
        for side in ("LONG", "SHORT"):
            opposite = "BEARISH" if side == "LONG" else "BULLISH"
            if btc_trend == opposite or eth_trend == opposite:
                continue
            sig = _candidate(row, side, float(row.close))
            if sig is None or sig.score < 7:
                continue
            sig.symbol = symbol
            sig.timestamp = str(pd.Timestamp(signal_close_ns, unit="ns", tz="UTC"))
            candidates.append(sig)
        if not candidates:
            continue

        sig = max(candidates, key=lambda item: item.score)
        sim_sig = SimpleNamespace(
            symbol=symbol, side=sig.side, stop_loss=sig.stop_loss,
            score=sig.score, setup=sig.setup, timestamp=sig.timestamp,
        )
        trade = simulate(
            sim_sig, enriched, i + 1, max_hold_bars, fee, funding
        )
        if trade is not None:
            trade["timeframe"] = timeframe
            trades.append(trade)
            matches = np.flatnonzero(
                pd.DatetimeIndex(enriched.timestamp) == pd.Timestamp(trade["exit_time"])
            )
            next_signal = int(matches[0]) + 1 if len(matches) else i + 2
    return pd.DataFrame(trades)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--fee-slippage-pct", type=float, default=0.12)
    parser.add_argument("--funding-bps-per-8h", type=float, default=0.0)
    args = parser.parse_args()
    if args.fee_slippage_pct < 0 or args.funding_bps_per_8h < 0:
        parser.error("fee/slippage and funding assumptions must be non-negative")

    root = Path(args.data_dir)
    output = Path("backtest_results")
    output.mkdir(exist_ok=True)
    manifest_path = root / "timeframe_manifest.json"
    report, all_trades = {}, []
    if not manifest_path.exists():
        raise RuntimeError("DATA INSUFFICIENT: timeframe_manifest.json is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    statuses = manifest.get("timeframes", {})

    for tf in TIMEFRAMES:
        interval = TF_MS[tf]
        btc_path, eth_path = root / "BTCUSDT" / f"{tf}.csv", root / "ETHUSDT" / f"{tf}.csv"
        btc_status = statuses.get("BTC", {}).get(tf, {}).get("status")
        eth_status = statuses.get("ETH", {}).get(tf, {}).get("status")
        if btc_status != "VALID" or eth_status != "VALID":
            report[tf] = {
                "status": "DATA INSUFFICIENT",
                "reason": f"manifest status BTC={btc_status}, ETH={eth_status}; both benchmarks are required",
            }
            continue
        if not btc_path.exists() or not eth_path.exists():
            report[tf] = {"status": "DATA INSUFFICIENT", "reason": "manifest-approved BTC/ETH file missing"}
            continue

        try:
            btc, eth = load_file(btc_path), load_file(eth_path)
            btc_prepared = prepare_benchmark(btc, tf)
            eth_prepared = prepare_benchmark(eth, tf)
            if not btc_prepared or not eth_prepared:
                raise ValueError("benchmark has no continuous segment with 100 candles")
        except Exception as exc:
            report[tf] = {"status": "DATA INSUFFICIENT", "reason": f"benchmark: {type(exc).__name__}: {exc}"}
            continue

        tf_report = {"status": "RESEARCH_ONLY", "strategy_profile": "same-timeframe candidate + timestamp-aligned BTC/ETH context",
                     "max_hold_hours": 24, "symbols": {}}
        tf_trades = []
        max_hold_bars = max(1, MAX_HOLD_MS // interval)
        for symbol in ("BTCUSDT", "ETHUSDT"):
            path = root / symbol / f"{tf}.csv"
            symbol_status = statuses.get(symbol.replace("USDT", ""), {}).get(tf, {}).get("status")
            if symbol_status != "VALID" or not path.exists():
                tf_report["symbols"][symbol] = {
                    "status": "DATA INSUFFICIENT",
                    "reason": f"manifest status={symbol_status}; timeframe file unavailable",
                }
                continue
            try:
                frame = load_file(path)
                segments = split_contiguous(frame, tf)
                segment_trades = []
                eligible_segments = 0
                for segment in segments:
                    if len(segment) < 101:
                        continue
                    eligible_segments += 1
                    result = run_segment(
                        symbol, tf, segment, btc_prepared, eth_prepared,
                        args.fee_slippage_pct, args.funding_bps_per_8h, max_hold_bars,
                    )
                    if not result.empty:
                        segment_trades.append(result)
                trades = pd.concat(segment_trades, ignore_index=True) if segment_trades else pd.DataFrame()
                metrics = summarize(trades)
                tf_report["symbols"][symbol] = {
                    "status": "RESEARCH_ONLY" if eligible_segments else "DATA INSUFFICIENT",
                    "continuous_segments_tested": eligible_segments,
                    **metrics,
                }
                if not trades.empty:
                    tf_trades.append(trades)
            except Exception as exc:
                tf_report["symbols"][symbol] = {
                    "status": "DATA INSUFFICIENT",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
        report[tf] = tf_report
        if tf_trades:
            all_trades.extend(tf_trades)

    combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    combined.to_csv(output / "trades_by_timeframe.csv", index=False)
    (output / "summary_by_timeframe.json").write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
    )
    lines = [
        "# BTC/ETH Independent Timeframe Research Backtest", "",
        "**Important:** This is a timeframe-isolated research variant, not the live 15m + 1h/4h/1d multi-timeframe strategy.",
        "Only manifest-approved data is used. Indicators reset after gaps; simulations never cross missing candles.",
        "BTC/ETH context is selected by completed-candle timestamps and rejected when stale.",
        "30m derives only from complete 15m buckets; 2h derives only from complete 1h buckets.",
        "Costs are estimates. Funding is a sensitivity assumption, not historical funding data.",
        "No exchange orders are submitted.", "",
    ]
    for tf, result in report.items():
        lines.extend([f"## {tf}", ""])
        if result.get("status") == "DATA INSUFFICIENT":
            lines.append(f"DATA INSUFFICIENT: {result.get('reason')}")
        else:
            lines.append(f"Profile: {result['strategy_profile']}; max hold: {result['max_hold_hours']}h")
            for symbol, metrics in result["symbols"].items():
                lines.append(f"- **{symbol}:** {json.dumps(metrics, sort_keys=True)}")
        lines.append("")
    report_text = "\n".join(lines)
    (output / "report_by_timeframe.md").write_text(report_text, encoding="utf-8")
    print(report_text)


if __name__ == "__main__":
    main()
