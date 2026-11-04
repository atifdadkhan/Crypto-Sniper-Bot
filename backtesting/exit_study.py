"""Explore Crypto Sniper exit policies on local archived OHLCV (signals only).

No private exchange APIs, no Telegram, no live order execution. Both the
training and 2026 July-November validation sample have been previously viewed,
so all results remain EXPLORATORY, never auto-approved for live deployment.
"""
import argparse
import io
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd

from backtesting.backtest import TFS, prepare, make_signal, _contiguous_15m_segments
from backtesting.data_loader import normalise_ohlcv, validate_timeframe
from backtesting.exit_policy import POLICIES, simulate_exit

# Known 2026 archive exceptions only. Review new market classifications.
EXCLUDED_NONCRYPTO = {"ANTHROPICUSDT", "SAMSUNGUSDT", "NBISUSDT"}


def load_frames(symbol, archive=None, data_dir=None):
    frames = {}
    for tf in TFS:
        if archive is not None:
            try:
                raw = pd.read_csv(io.BytesIO(archive.read(f"data/{symbol}/{tf}.csv")))
            except KeyError as exc:
                raise ValueError(f"DATA INSUFFICIENT: {symbol} missing {tf}") from exc
        else:
            path = Path(data_dir) / symbol / f"{tf}.csv"
            if not path.is_file():
                raise ValueError(f"DATA INSUFFICIENT: missing {path}")
            raw = pd.read_csv(path)
        frame = normalise_ohlcv(raw, min_candles=100)
        validate_timeframe(frame, tf, symbol, allow_gaps=(tf == "15m"))
        frames[tf] = frame
    return frames


def metrics(trades):
    if not trades:
        return {"trades": 0, "profit_factor": None, "expectancy_r": None,
                "net_r": 0.0, "max_drawdown_r": None}
    values = np.asarray([trade["net_r"] for trade in trades], dtype=float)
    equity = np.r_[0.0, values.cumsum()]
    gains, losses = float(values[values > 0].sum()), float(-values[values < 0].sum())
    return {
        "trades": len(values),
        "profit_factor": round(gains / losses, 3) if losses else None,
        "expectancy_r": round(float(values.mean()), 4),
        "net_r": round(float(values.sum()), 3),
        "win_rate_pct": round(100 * float(np.mean(values > 0)), 2),
        "max_drawdown_r": round(float(np.min(equity - np.maximum.accumulate(equity))), 3),
    }


def collect_candidates(symbol, frames, btc, eth, start, end):
    """Expensive higher-timeframe evaluation happens ONCE per market."""
    result = []
    for segment_no, segment in enumerate(_contiguous_15m_segments(frames["15m"])):
        if len(segment) < 110:
            continue
        target = prepare({**frames, "15m": segment})
        fast = target["15m"][0]
        for i in range(100, len(fast) - 1):
            candle_close = pd.Timestamp(fast.iloc[i].timestamp) + pd.Timedelta(minutes=15)
            if candle_close < start or candle_close >= end:
                continue
            # Reject incomplete next entry candle or gaps.
            if pd.Timestamp(fast.iloc[i + 1].timestamp) != candle_close:
                continue
            signal = make_signal(symbol, target, btc, eth, i)
            if signal is not None:
                result.append((segment_no, i + 1, candle_close, signal, fast))
    return result


def evaluate_policy(symbol, candidates, policy, cost_pct):
    trades, last_exit, last_signal = [], {}, {}
    for segment_no, entry_i, stamp, signal, fast in candidates:
        if entry_i <= last_exit.get(segment_no, -1):
            continue
        prior = last_signal.get(segment_no)
        if prior is not None and stamp - prior < pd.Timedelta(minutes=180):
            continue
        trade = simulate_exit(signal, fast, entry_i, policy,
                              round_trip_cost_pct=cost_pct)
        if trade is None or trade["data_censored"]:
            continue
        trade.update({"symbol": symbol, "policy": policy.name, "cost_pct": cost_pct,
                      "setup": signal.setup, "score": signal.score,
                      "signal_time": str(stamp)})
        trades.append(trade)
        last_signal[segment_no] = stamp
        last_exit[segment_no] = int(pd.DatetimeIndex(fast.timestamp).searchsorted(
            pd.Timestamp(trade["exit_time"])))
    return trades


def _utc(date):
    stamp = pd.Timestamp(date)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


class _NoArchive:
    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False


def run_study(archive_path=None, data_dir=None, out_dir="exit_study_results",
              test_start="2026-03-01", train_end="2026-07-01",
              test_end="2026-11-03T08:30:00Z"):
    start, pivot, end = (_utc(x) for x in (test_start, train_end, test_end))
    if not start < pivot < end:
        raise ValueError("Date order must be test_start < train_end < test_end")
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) if archive_path else _NoArchive() as archive:
        if archive is not None:
            names = {p.split("/")[1] for p in archive.namelist()
                     if p.startswith("data/") and p.endswith("/15m.csv")}
        else:
            names = {p.name for p in Path(data_dir).iterdir() if p.is_dir()}
        symbols = sorted(s for s in names if s.endswith("USDT") and s not in EXCLUDED_NONCRYPTO)
        if not {"BTCUSDT", "ETHUSDT"}.issubset(symbols):
            raise ValueError("DATA INSUFFICIENT: missing BTCUSDT / ETHUSDT")
        btc = prepare(load_frames("BTCUSDT", archive, data_dir))
        eth = prepare(load_frames("ETHUSDT", archive, data_dir))
        all_trades, skipped = [], {}
        for symbol in symbols:
            try:
                frames = load_frames(symbol, archive, data_dir)
                candidates = collect_candidates(symbol, frames, btc, eth, start, end)
                for policy in POLICIES:
                    for cost in (0.12, 0.18):
                        all_trades.extend(evaluate_policy(symbol, candidates, policy, cost))
            except (ValueError, KeyError, IndexError) as exc:
                skipped[symbol] = f"DATA INSUFFICIENT: {exc}"
            print(f"{symbol}: candidates reviewed; skipped={symbol in skipped}", flush=True)
    trades = pd.DataFrame(all_trades)
    trades.to_csv(output / "trades.csv", index=False)
    summaries = []
    for policy in POLICIES:
        for cost in (0.12, 0.18):
            if trades.empty:
                group = trades
            else:
                group = trades[(trades.policy == policy.name) & (trades.cost_pct == cost)]
            if group.empty:
                train, eval_set = [], []
            else:
                t = pd.to_datetime(group.entry_time, utc=True)
                train = group[t < pivot].to_dict("records")
                eval_set = group[t >= pivot].to_dict("records")
            for label, subset in (("train", train), ("previously_seen_evaluation", eval_set)):
                summaries.append({"policy": policy.name, "cost_pct": cost,
                                  "period": label, **metrics(subset)})
    summary = pd.DataFrame(summaries)
    summary.to_csv(output / "summary.csv", index=False)
    (output / "decision.json").write_text(json.dumps({
        "production_strategy_change_approved": False,
        "reason": "No untouched holdout; real funding, live fills, correlations unverified",
        "train_screening": {"minimum_trades": 50, "profit_factor": 1.30,
                            "expectancy_r": 0.15},
        "cost_scenarios_pct": [0.12, 0.18],
        "markets": len(symbols), "skipped": skipped,
        "warning": "R is not account return. Never promote on these reused samples."
    }, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--archive", help="Zip containing data/SYMBOL/{15m,1h,4h,1d}.csv")
    group.add_argument("--data-dir", help="Extracted data/ directory")
    parser.add_argument("--out-dir", default="exit_study_results")
    parser.add_argument("--test-start", default="2026-03-01")
    parser.add_argument("--train-end", default="2026-07-01")
    parser.add_argument("--test-end", default="2026-11-03T08:30:00Z")
    args = parser.parse_args()
    summary = run_study(args.archive, args.data_dir, args.out_dir,
                        args.test_start, args.train_end, args.test_end)
    print(summary.to_string(index=False))
    print("RESEARCH ONLY — zero changes to live strategy or exchange positions.")


if __name__ == "__main__":
    main()
