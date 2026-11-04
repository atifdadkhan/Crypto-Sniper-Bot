"""Research fixed, pre-registered combinations of the existing strategy engines.

Runs only on restored Bitget public OHLCV artifacts. Completed candles only;
entries at the next candle open; estimated costs included; no orders are sent.
Each contiguous data segment is split chronologically: first 60% in-sample,
last 40% untouched out-of-sample. This is a screening holdout, not live approval.
"""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from backtesting.backtest import simulate
from backtesting.full_market_runner import ALL_TIMEFRAMES, load_market_timeframes
from backtesting.multi_strategy import (
    STRATEGIES, INTERVAL_MS, _signal, _strategy_signal, contiguous_segments,
    enrich_for_engines, metrics,
)

# Fixed combinations are declared before evaluation; no parameter optimisation.
COMBINATIONS = {
    "trend_pullback_plus_momentum": {
        "required": ("trend_pullback", "momentum_continuation"),
        "min_votes": 2,
    },
    "trend_pullback_plus_breakout_retest": {
        "required": ("trend_pullback", "breakout_retest"),
        "min_votes": 2,
    },
    "breakout_retest_plus_momentum": {
        "required": ("breakout_retest", "momentum_continuation"),
        "min_votes": 2,
    },
    "breakout_retest_plus_volatility_expansion": {
        "required": ("breakout_retest", "volatility_expansion"),
        "min_votes": 2,
    },
    "two_of_five_consensus": {
        "required": (),
        "min_votes": 2,
    },
    "three_of_five_consensus": {
        "required": (),
        "min_votes": 3,
    },
}
BASELINE = "range_reversal"


def combine_candidates(row, side, name, candidates, required=(), min_votes=2):
    """Combine independent same-side engine votes and validate shared risk."""
    if any(engine not in candidates for engine in required):
        return None
    if len(candidates) < min_votes:
        return None
    stops = np.asarray([float(sig.stop_loss) for sig in candidates.values()], dtype=float)
    if not np.isfinite(stops).all():
        return None
    # Median structural invalidation avoids choosing the widest stop by default.
    stop = float(np.median(stops))
    score = min(10, 7 + max(0, len(candidates) - min_votes))
    signal = _signal(row, side, name, stop, score)
    return signal


def signals_for_row(row):
    by_side = {"LONG": {}, "SHORT": {}}
    for side in ("LONG", "SHORT"):
        for engine in STRATEGIES:
            candidate = _strategy_signal(row, side, engine)
            if candidate is not None:
                by_side[side][engine] = candidate
    return by_side


def _simulate_window(symbol, timeframe, bars, strategy_name, start_index,
                     fee_slippage_pct, sample_period):
    """One position at a time; no entry can use the unfinished current bar."""
    trades = []
    view = bars.reset_index(drop=True)
    next_entry = max(100, int(start_index))
    max_hold = max(1, 86_400_000 // INTERVAL_MS[timeframe])
    for i in range(next_entry, len(view) - 1):
        row = view.iloc[i]
        votes = signals_for_row(row)
        selected = None
        for side in ("LONG", "SHORT"):
            if strategy_name == BASELINE:
                candidate = votes[side].get(BASELINE)
                # A stricter range-regime guard is fixed in advance.
                if candidate is not None and float(row.adx) <= 25 and float(row.vol_ratio) >= 0.65:
                    selected = candidate
                    break
                continue
            spec = COMBINATIONS[strategy_name]
            combined = combine_candidates(
                row, side, strategy_name, votes[side],
                required=spec["required"], min_votes=spec["min_votes"],
            )
            if combined is not None:
                selected = combined
                break
        if selected is None:
            continue
        selected.symbol = symbol
        selected.timestamp = str(
            pd.Timestamp(row.timestamp)
            + pd.Timedelta(milliseconds=INTERVAL_MS[timeframe])
        )
        trade = simulate(selected, view, i + 1, max_hold, fee_slippage_pct, 0.0)
        if trade is None:
            continue
        trade["strategy"] = strategy_name
        trade["timeframe"] = timeframe
        trade["sample_period"] = sample_period
        trades.append(trade)
        exits = np.flatnonzero(
            pd.DatetimeIndex(view.timestamp) == pd.Timestamp(trade["exit_time"])
        )
        next_entry = int(exits[0]) + 1 if len(exits) else i + 2
    return pd.DataFrame(trades)


def _empty_metrics():
    return {
        "status": "DATA INSUFFICIENT", "trades": 0, "win_rate_pct": None,
        "profit_factor": None, "net_r": 0.0, "max_drawdown_r": 0.0,
    }


def evaluate_segment(symbol, timeframe, raw_segment, strategy_name, fee_slippage_pct):
    bars = enrich_for_engines(raw_segment)
    if len(bars) < 200:
        return None, None, "DATA INSUFFICIENT: fewer than 200 indicator-ready candles"
    split = int(len(bars) * 0.60)
    if split < 120 or (len(bars) - split) < 40:
        return None, None, "DATA INSUFFICIENT: insufficient chronological holdout"
    train = _simulate_window(
        symbol, timeframe, bars.iloc[:split].copy(), strategy_name, 100,
        fee_slippage_pct, "IN_SAMPLE",
    )
    test = _simulate_window(
        symbol, timeframe, bars.copy(), strategy_name, split,
        fee_slippage_pct, "OUT_OF_SAMPLE",
    )
    return train, test, None


def classify(oos, minimum_oos_trades):
    if oos.get("trades", 0) < minimum_oos_trades:
        return "DATA INSUFFICIENT"
    pf = oos.get("profit_factor")
    if pf is not None and pf >= 1.20 and oos.get("net_r", 0) > 0:
        return "PROMISING — NEEDS WALK-FORWARD"
    return "EXPERIMENTAL — NOT APPROVED"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--fee-slippage-pct", type=float, default=0.12)
    parser.add_argument("--minimum-oos-trades", type=int, default=20)
    parser.add_argument("--output-dir", default="backtest_results/strategy_chemistry")
    args = parser.parse_args()
    if not np.isfinite(args.fee_slippage_pct) or args.fee_slippage_pct < 0:
        parser.error("fee-slippage-pct must be finite and non-negative")
    if args.minimum_oos_trades < 1:
        parser.error("minimum-oos-trades must be >= 1")

    root = Path(args.data_dir)
    manifest_path = root / "download_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("DATA INSUFFICIENT: merged download_manifest.json missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("exchange", "")).lower() != "bitget":
        raise RuntimeError("Refusing non-Bitget dataset")
    if "perpetual" not in str(manifest.get("market_type", "")).lower():
        raise RuntimeError("Refusing non-perpetual dataset")

    strategies = [BASELINE, *COMBINATIONS]
    summary, all_trades = {}, []
    folders = sorted(p for p in root.iterdir() if p.is_dir() and p.name.endswith("USDT"))
    for folder in folders:
        frames, data_status = load_market_timeframes(folder)
        summary[folder.name] = {"data": data_status, "strategies": {}}
        for timeframe in ALL_TIMEFRAMES:
            if timeframe not in frames:
                continue
            for strategy_name in strategies:
                train_parts, test_parts, tested_segments, insufficient = [], [], 0, []
                for segment in contiguous_segments(frames[timeframe], timeframe):
                    if len(segment) < 200:
                        continue
                    train, test, reason = evaluate_segment(
                        folder.name, timeframe, segment, strategy_name,
                        args.fee_slippage_pct,
                    )
                    if reason:
                        insufficient.append(reason)
                        continue
                    tested_segments += 1
                    if train is not None and not train.empty:
                        train_parts.append(train)
                    if test is not None and not test.empty:
                        test_parts.append(test)
                train_trades = pd.concat(train_parts, ignore_index=True) if train_parts else pd.DataFrame()
                test_trades = pd.concat(test_parts, ignore_index=True) if test_parts else pd.DataFrame()
                train_metrics = metrics(train_trades) if not train_trades.empty else _empty_metrics()
                test_metrics = metrics(test_trades) if not test_trades.empty else _empty_metrics()
                record = {
                    "segments_tested": tested_segments,
                    "in_sample": train_metrics,
                    "out_of_sample": test_metrics,
                    "research_status": classify(test_metrics, args.minimum_oos_trades),
                    "notes": insufficient[:5],
                }
                summary[folder.name]["strategies"].setdefault(timeframe, {})[strategy_name] = record
                if not train_trades.empty:
                    all_trades.append(train_trades)
                if not test_trades.empty:
                    all_trades.append(test_trades)

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    trades_all = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trades_all.to_csv(output / "strategy_chemistry_trades.csv", index=False)
    (output / "strategy_chemistry_summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8"
    )

    rows = []
    for symbol, data in summary.items():
        for timeframe, combinations in data["strategies"].items():
            for strategy_name, record in combinations.items():
                oos = record["out_of_sample"]
                ins = record["in_sample"]
                rows.append({
                    "symbol": symbol, "timeframe": timeframe, "strategy": strategy_name,
                    "research_status": record["research_status"],
                    "in_sample_trades": ins.get("trades", 0),
                    "oos_trades": oos.get("trades", 0),
                    "oos_win_rate_pct": oos.get("win_rate_pct"),
                    "oos_profit_factor": oos.get("profit_factor"),
                    "oos_net_r": oos.get("net_r"),
                    "oos_max_drawdown_r": oos.get("max_drawdown_r"),
                    "segments_tested": record["segments_tested"],
                })
    leaderboard = pd.DataFrame(rows)
    if not leaderboard.empty:
        leaderboard = leaderboard.sort_values(
            ["research_status", "oos_profit_factor", "oos_net_r"],
            ascending=[True, False, False], na_position="last",
        )
    leaderboard.to_csv(output / "strategy_chemistry_leaderboard.csv", index=False)
    report = [
        "# A&T TrendHive — Strategy Chemistry Research", "",
        f"Markets: {len(folders)}",
        f"Round-trip fee + slippage assumption: {args.fee_slippage_pct:.4f}% notional",
        f"Minimum OOS trades for screening: {args.minimum_oos_trades}",
        "Each contiguous segment: first 60% in-sample, final 40% out-of-sample.",
        "Entries use the next candle open after a completed signal candle.",
        "Fixed combinations; no parameter optimisation. Results are not a probability forecast.",
        "Historical funding is not included; perpetual results remain experimental.",
        "No exchange orders are sent. No strategy is auto-approved for live Telegram signals.", "",
        "| Symbol | TF | Strategy | OOS trades | OOS PF | OOS Net R | OOS Max DD R | Status |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        report.append(
            f"| {row['symbol']} | {row['timeframe']} | {row['strategy']} | "
            f"{row['oos_trades']} | {row['oos_profit_factor']} | {row['oos_net_r']} | "
            f"{row['oos_max_drawdown_r']} | {row['research_status']} |"
        )
    (output / "strategy_chemistry_report.md").write_text(
        "\n".join(report) + "\n", encoding="utf-8"
    )
    print(f"Markets analysed: {len(folders)}")
    print(f"Trade rows: {len(trades_all)}")
    print(leaderboard.head(100).to_string(index=False) if not leaderboard.empty else "No qualifying trades")
    print("RESEARCH ONLY — no live approval; historical funding excluded.")


if __name__ == "__main__":
    main()
