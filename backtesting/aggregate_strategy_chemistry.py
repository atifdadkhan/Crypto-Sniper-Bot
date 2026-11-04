"""Aggregate per-batch strategy chemistry results into global OOS rankings."""
import argparse
import json
from pathlib import Path

import pandas as pd

from backtesting.multi_strategy import metrics


EXPECTED_OFFSETS = list(range(0, 800, 20))


def classify(result, minimum_oos_trades, market_count):
    if result.get("trades", 0) < minimum_oos_trades or market_count < 5:
        return "DATA INSUFFICIENT"
    pf = result.get("profit_factor")
    if pf is not None and pf >= 1.20 and result.get("net_r", 0) > 0:
        return "PROMISING — NEEDS WALK-FORWARD"
    return "EXPERIMENTAL — NOT APPROVED"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", default="batch_results")
    parser.add_argument("--output-dir", default="backtest_results/strategy_chemistry")
    parser.add_argument("--minimum-oos-trades", type=int, default=20)
    parser.add_argument("--source-run-id", default="")
    args = parser.parse_args()
    if args.minimum_oos_trades < 1:
        parser.error("minimum-oos-trades must be >= 1")

    root, output = Path(args.input_dir), Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    summary, trade_frames, offsets = {}, [], []
    for batch in sorted(p for p in root.glob("at-trendhive-chemistry-batch-*") if p.is_dir()):
        offset_path = batch / "batch_offset.json"
        summary_path = batch / "strategy_chemistry_summary.json"
        trades_path = batch / "strategy_chemistry_trades.csv"
        if not offset_path.is_file() or not summary_path.is_file():
            continue
        meta = json.loads(offset_path.read_text(encoding="utf-8"))
        offsets.append(int(meta["offset"]))
        part = json.loads(summary_path.read_text(encoding="utf-8"))
        for symbol, result in part.items():
            if symbol in summary:
                raise RuntimeError(f"Duplicate symbol across chemistry batches: {symbol}")
            summary[symbol] = result
        if trades_path.is_file():
            trades = pd.read_csv(trades_path)
            if not trades.empty:
                trade_frames.append(trades)

    offsets = sorted(set(offsets))
    if not offsets:
        raise RuntimeError("No completed strategy chemistry batch artifacts were restored.")
    missing = sorted(set(EXPECTED_OFFSETS) - set(offsets))
    trades_all = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    trades_all.to_csv(output / "strategy_chemistry_trades.csv", index=False)
    (output / "strategy_chemistry_summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8"
    )

    symbol_rows = []
    for symbol, data in summary.items():
        for timeframe, strategies in data.get("strategies", {}).items():
            for name, record in strategies.items():
                oos, ins = record.get("out_of_sample", {}), record.get("in_sample", {})
                symbol_rows.append({
                    "symbol": symbol, "asset_class": data.get("asset_class", "crypto_or_unverified"),
                    "timeframe": timeframe, "strategy": name,
                    "research_status": record.get("research_status"),
                    "in_sample_trades": ins.get("trades", 0),
                    "oos_trades": oos.get("trades", 0),
                    "oos_win_rate_pct": oos.get("win_rate_pct"),
                    "oos_profit_factor": oos.get("profit_factor"),
                    "oos_net_r": oos.get("net_r"),
                    "oos_max_drawdown_r": oos.get("max_drawdown_r"),
                    "segments_tested": record.get("segments_tested", 0),
                })
    symbol_leaderboard = pd.DataFrame(symbol_rows)
    symbol_leaderboard.to_csv(output / "strategy_chemistry_leaderboard.csv", index=False)

    aggregate_rows = []
    oos_trades = (
        trades_all[trades_all["sample_period"] == "OUT_OF_SAMPLE"]
        if not trades_all.empty and "sample_period" in trades_all.columns
        else pd.DataFrame()
    )
    if not oos_trades.empty:
        for (asset_class, timeframe, strategy), group in oos_trades.groupby(
            ["asset_class", "timeframe", "strategy"], dropna=False
        ):
            result = metrics(group.sort_values("entry_time").reset_index(drop=True))
            aggregate_rows.append({
                "asset_class": asset_class, "timeframe": timeframe, "strategy": strategy,
                "markets": int(group["symbol"].nunique()),
                "research_status": classify(result, args.minimum_oos_trades, int(group["symbol"].nunique())),
                "oos_trades": result.get("trades", 0),
                "oos_win_rate_pct": result.get("win_rate_pct"),
                "oos_profit_factor": result.get("profit_factor"),
                "oos_net_r": result.get("net_r"),
                "oos_max_drawdown_r": result.get("max_drawdown_r"),
            })
    aggregate = pd.DataFrame(aggregate_rows)
    if not aggregate.empty:
        status_order = {
            "PROMISING — NEEDS WALK-FORWARD": 0,
            "EXPERIMENTAL — NOT APPROVED": 1,
            "DATA INSUFFICIENT": 2,
        }
        aggregate["_status_order"] = aggregate["research_status"].map(status_order).fillna(3)
        aggregate = aggregate.sort_values(
            ["_status_order", "oos_profit_factor", "oos_net_r"],
            ascending=[True, False, False], na_position="last",
        ).drop(columns=["_status_order"])
    aggregate.to_csv(output / "strategy_chemistry_rankings.csv", index=False)

    for asset_class, label in (
        ("crypto_or_unverified", "crypto"),
        ("tokenized_equity", "tokenized_equities"),
    ):
        symbol_leaderboard[symbol_leaderboard.get("asset_class", pd.Series(dtype=str)) == asset_class].to_csv(
            output / f"{label}_leaderboard.csv", index=False
        )
        if not aggregate.empty:
            aggregate[aggregate["asset_class"] == asset_class].to_csv(
                output / f"{label}_strategy_rankings.csv", index=False
            )

    report = [
        "# A&T TrendHive — Strategy Chemistry Aggregate", "",
        f"Source Bitget run ID: {args.source_run_id or 'not supplied'}",
        f"Completed chemistry batches: {offsets}",
        f"Missing chemistry batches: {missing}",
        f"Markets analysed: {len(summary)}",
        f"Out-of-sample trade rows: {len(oos_trades)}",
        "Only complete symbols listed as downloaded in each batch manifest are eligible.",
        "Pooled rankings are chronological trade-level R summaries, not capital-weighted portfolio drawdowns.",
        "Fixed strategy combinations; first 60% in-sample and final 40% out-of-sample per contiguous segment.",
        "Aggregate screening requires at least 20 OOS trades across at least 5 markets.",
        "Historical perpetual funding is not included. Results are research-only, not live approval.", "",
        "| Asset class | TF | Strategy | Markets | OOS trades | PF | Net R | Max DD R | Status |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in aggregate_rows:
        report.append(
            f"| {row['asset_class']} | {row['timeframe']} | {row['strategy']} | "
            f"{row['markets']} | {row['oos_trades']} | {row['oos_profit_factor']} | "
            f"{row['oos_net_r']} | {row['oos_max_drawdown_r']} | {row['research_status']} |"
        )
    (output / "strategy_chemistry_aggregate_report.md").write_text(
        "\n".join(report) + "\n", encoding="utf-8"
    )
    print(f"Completed batches: {offsets}")
    print(f"Missing batches: {missing}")
    print(f"Markets analysed: {len(summary)}")
    print(aggregate.to_string(index=False) if not aggregate.empty else "No OOS trades available for ranking.")


if __name__ == "__main__":
    main()
