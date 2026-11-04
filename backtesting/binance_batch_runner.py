"""Run preserved strategy engines on one validated Binance batch; spot is LONG-only."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.full_market_runner import ALL_TIMEFRAMES, load_market_timeframes
from backtesting.multi_strategy import (STRATEGIES, INTERVAL_MS, contiguous_segments,
    run_engine_segment, enrich_for_engines, _strategy_signal, metrics)
from backtesting.backtest import simulate


def run_spot_long_segment(symbol, timeframe, frame, engine, fee_slippage_pct):
    """Spot-only simulation: never generates or simulates short entries."""
    bars = enrich_for_engines(frame)
    trades = []
    next_entry_index = 100
    max_hold = max(1, 86_400_000 // INTERVAL_MS[timeframe])
    for i in range(100, len(bars) - 1):
        if i < next_entry_index:
            continue
        signal = _strategy_signal(bars.iloc[i], "LONG", engine)
        if signal is None:
            continue
        signal.symbol = symbol
        signal.timestamp = str(pd.Timestamp(bars.iloc[i].timestamp) +
                               pd.Timedelta(milliseconds=INTERVAL_MS[timeframe]))
        trade = simulate(signal, bars, i + 1, max_hold, fee_slippage_pct, 0.0)
        if trade is None:
            continue
        trade["strategy"] = engine
        trade["timeframe"] = timeframe
        trades.append(trade)
        exits = np.flatnonzero(pd.DatetimeIndex(bars.timestamp) == pd.Timestamp(trade["exit_time"]))
        next_entry_index = int(exits[0]) + 1 if len(exits) else i + 2
    return pd.DataFrame(trades)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", required=True)
    p.add_argument("--market-type", choices=("spot", "usdt_perpetual"), required=True)
    p.add_argument("--fee-slippage-pct", type=float, default=0.12)
    p.add_argument("--minimum-trades", type=int, default=30)
    p.add_argument("--output-dir", default="backtest_results")
    a = p.parse_args()
    if a.fee_slippage_pct < 0 or a.minimum_trades < 1:
        p.error("cost must be non-negative and minimum-trades >= 1")
    root, output = Path(a.data_dir), Path(a.output_dir)
    manifest_path = root / "download_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("DATA INSUFFICIENT: download manifest missing")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("exchange") != "Binance" or manifest.get("market_type") != a.market_type:
        raise RuntimeError("Refusing mismatched exchange/market dataset")
    summary, trade_frames = {}, []
    folders = sorted(x for x in root.iterdir() if x.is_dir() and x.name.endswith("USDT"))
    for folder in folders:
        frames, statuses = load_market_timeframes(folder)
        summary[folder.name] = {"data": statuses, "strategies": {}}
        for tf in ALL_TIMEFRAMES:
            if tf not in frames:
                continue
            engines = {}
            for engine in STRATEGIES:
                pieces, tested = [], 0
                for segment in contiguous_segments(frames[tf], tf):
                    if len(segment) < 120:
                        continue
                    tested += 1
                    if a.market_type == "spot":
                        trades = run_spot_long_segment(folder.name, tf, segment, engine, a.fee_slippage_pct)
                    else:
                        trades = run_engine_segment(folder.name, tf, segment, engine, a.fee_slippage_pct)
                    if not trades.empty:
                        pieces.append(trades)
                combined = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
                m = metrics(combined)
                m["segments_tested"] = tested
                m["sample_status"] = "SAMPLE_SUFFICIENT_FOR_SCREENING" if m["trades"] >= a.minimum_trades else "DATA INSUFFICIENT"
                engines[engine] = m
                if not combined.empty:
                    trade_frames.append(combined)
            summary[folder.name]["strategies"][tf] = engines
    output.mkdir(parents=True, exist_ok=True)
    all_trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    all_trades.to_csv(output / "binance_strategy_trades.csv", index=False)
    (output / "binance_strategy_summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False))
    print(f"Binance {a.market_type}: {len(folders)} symbols; {len(all_trades)} trade rows")
    print("Research-only. Spot is LONG-only. Perpetual funding is not included; do not treat results as live-ready.")


if __name__ == "__main__":
    main()
