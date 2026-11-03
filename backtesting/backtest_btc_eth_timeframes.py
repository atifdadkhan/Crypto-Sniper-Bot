"""Independent timeframe backtest for BTC/ETH using only validated files."""
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
HOLD_BARS = 96


def _run_one(symbol, timeframe, frame, btc_frame, eth_frame, fee, funding):
    frame = enrich(frame)
    btc_frame, eth_frame = enrich(btc_frame), enrich(eth_frame)
    trades = []
    next_signal = 100
    for i in range(100, len(frame) - 1):
        if i < next_signal:
            continue
        row = frame.iloc[i]
        btc_row, eth_row = btc_frame.iloc[i], eth_frame.iloc[i]
        btc_trend, eth_trend = trend(btc_row), trend(eth_row)
        candidates = []
        for side in ("LONG", "SHORT"):
            opposite = "BEARISH" if side == "LONG" else "BULLISH"
            wanted = "BULLISH" if side == "LONG" else "BEARISH"
            if btc_trend == opposite or eth_trend == opposite:
                continue
            sig = _candidate(row, side, float(row.close))
            if sig is None or sig.score < 7:
                continue
            sig.symbol = symbol
            sig.timestamp = str(pd.Timestamp(row.timestamp) + pd.Timedelta(
                milliseconds={"15m":900000,"30m":1800000,"1h":3600000,"2h":7200000,"4h":14400000,"1d":86400000}[timeframe]
            ))
            candidates.append(sig)
        if not candidates:
            continue
        sig = max(candidates, key=lambda item: item.score)
        sim_sig = SimpleNamespace(symbol=symbol, side=sig.side, stop_loss=sig.stop_loss,
                                  score=sig.score, setup=sig.setup, timestamp=sig.timestamp)
        trade = simulate(sim_sig, frame, i + 1, HOLD_BARS, fee, funding)
        if trade is not None:
            trade["timeframe"] = timeframe
            trades.append(trade)
            matches = np.flatnonzero(pd.DatetimeIndex(frame.timestamp) == pd.Timestamp(trade["exit_time"]))
            next_signal = int(matches[0]) + 1 if len(matches) else i + 2
    return pd.DataFrame(trades)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--fee-slippage-pct", type=float, default=0.12)
    parser.add_argument("--funding-bps-per-8h", type=float, default=0.0)
    args = parser.parse_args()
    root = Path(args.data_dir)
    output = Path("backtest_results")
    output.mkdir(exist_ok=True)
    report, all_trades = {}, []
    for tf in TIMEFRAMES:
        btc_path, eth_path = root / "BTCUSDT" / f"{tf}.csv", root / "ETHUSDT" / f"{tf}.csv"
        if not btc_path.exists() or not eth_path.exists():
            report[tf] = {"status": "DATA INSUFFICIENT", "reason": "BTC and ETH benchmark files are both required"}
            continue
        try:
            btc, eth = load_file(btc_path), load_file(eth_path)
            tf_trades = []
            for symbol in ("BTCUSDT", "ETHUSDT"):
                path = root / symbol / f"{tf}.csv"
                if not path.exists():
                    report.setdefault(tf, {})[symbol] = {"status": "DATA INSUFFICIENT", "reason": "timeframe file missing"}
                    continue
                try:
                    frame = load_file(path)
                    trades = _run_one(symbol, tf, frame, btc, eth, args.fee_slippage_pct, args.funding_bps_per_8h)
                    metrics = summarize(trades)
                    report.setdefault(tf, {})[symbol] = {"status": "OK", **metrics}
                    if not trades.empty:
                        tf_trades.append(trades)
                except Exception as exc:
                    report.setdefault(tf, {})[symbol] = {"status": "DATA INSUFFICIENT", "reason": f"{type(exc).__name__}: {exc}"}
            if tf_trades:
                all_trades.extend(tf_trades)
        except Exception as exc:
            report[tf] = {"status": "DATA INSUFFICIENT", "reason": f"{type(exc).__name__}: {exc}"}
    combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    combined.to_csv(output / "trades_by_timeframe.csv", index=False)
    (output / "summary_by_timeframe.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# BTC/ETH Independent Timeframe Backtest", "",
             "Signals only; no orders submitted. Each timeframe is tested independently. ",
             "A timeframe is excluded if either BTC/ETH benchmark file is missing or invalid.",
             "30m is aggregated from complete 15m candles; 2h is aggregated from complete 1h candles.",
             "Fees/slippage are estimates; funding is a sensitivity assumption, not historical funding.", ""]
    for tf, result in report.items():
        lines.extend([f"## {tf}", ""])
        if result.get("status") == "DATA INSUFFICIENT":
            lines.append(f"DATA INSUFFICIENT: {result.get('reason')}")
        else:
            for symbol, metrics in result.items():
                lines.append(f"- **{symbol}:** {json.dumps(metrics, sort_keys=True)}")
        lines.append("")
    (output / "report_by_timeframe.md").write_text("\n".join(lines), encoding="utf-8")
    print((output / "report_by_timeframe.md").read_text())


if __name__ == "__main__":
    main()
