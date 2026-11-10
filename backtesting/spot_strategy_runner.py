"""Per-coin walk-forward backtests for the 12 Crypto Sniper spot engines."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.full_market_runner import ALL_TIMEFRAMES, load_market_timeframes
from backtesting.multi_strategy import INTERVAL_MS, contiguous_segments
from spot_strategy_engines import DataInsufficient, STRATEGY_IDS, STRATEGY_SPECS, evaluate_strategy


def _atr(frame, n=14):
    previous = frame.close.shift(1)
    tr = pd.concat((frame.high-frame.low, (frame.high-previous).abs(),
                    (frame.low-previous).abs()), axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()


def _metrics(trades):
    if trades.empty:
        return {"trades": 0, "win_rate_pct": None, "profit_factor": None,
                "net_r": 0.0, "average_r": None, "max_drawdown_r": 0.0}
    net = pd.to_numeric(trades.net_r, errors="coerce").dropna()
    wins, losses = float(net[net > 0].sum()), float(-net[net < 0].sum())
    equity = net.cumsum()
    dd = equity - equity.cummax().clip(lower=0)
    return {
        "trades": int(len(net)),
        "win_rate_pct": round(float((net > 0).mean()*100), 2),
        "profit_factor": round(wins/losses, 3) if losses > 0 else None,
        "net_r": round(float(net.sum()), 3),
        "average_r": round(float(net.mean()), 4),
        "max_drawdown_r": round(float(dd.min()), 3),
    }


def simulate_spot(symbol, timeframe, frame, signals, strategy_id, fee_slippage_pct,
                  stop_atr=1.5, target_r=2.5, max_hold_hours=24,
                  start=0, end=None, sample_period="OUT_OF_SAMPLE"):
    """Closed-bar signal -> next-open spot entry/exit; conservative stop-first fills."""
    end = len(frame) if end is None else min(int(end), len(frame))
    start = max(0, int(start))
    if end-start < 3:
        return pd.DataFrame()
    bars, sig = frame.reset_index(drop=True), signals.reset_index(drop=True)
    atr = _atr(bars)
    max_hold = max(1, int(math.ceil(max_hold_hours*3_600_000/INTERVAL_MS[timeframe])))
    trades, i = [], max(1, start)
    while i < end-1:
        if not bool(sig.entry_long.iloc[i]) or pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
            i += 1
            continue
        entry_i = i+1
        entry = float(bars.open.iloc[entry_i])
        risk = float(atr.iloc[i])*stop_atr
        stop, target = entry-risk, entry+target_r*risk
        if not all(math.isfinite(v) and v > 0 for v in (entry, risk, stop, target)):
            i += 1
            continue
        exit_price = exit_i = None
        reason = None
        last = min(end-1, entry_i+max_hold-1)
        for j in range(entry_i, last+1):
            op, hi, lo = map(float, (bars.open.iloc[j], bars.high.iloc[j], bars.low.iloc[j]))
            if op <= stop:
                exit_price, exit_i, reason = op, j, "STOP_GAP"
                break
            if lo <= stop and hi >= target:
                exit_price, exit_i, reason = stop, j, "STOP_FIRST"
                break
            if lo <= stop:
                exit_price, exit_i, reason = stop, j, "STOP"
                break
            if hi >= target:
                exit_price, exit_i, reason = target, j, "TARGET"
                break
            if bool(sig.exit_long.iloc[j]) and j+1 < end:
                exit_price, exit_i, reason = float(bars.open.iloc[j+1]), j+1, "STRATEGY_EXIT"
                break
            if j == last and j+1 < end:
                exit_price, exit_i, reason = float(bars.open.iloc[j+1]), j+1, "TIME_EXIT"
                break
        if exit_price is None:
            break  # open trade at split/end is censored, not marked to a convenient close
        gross_r = (exit_price-entry)/risk
        cost_r = (fee_slippage_pct/100)*entry/risk
        entry_ts = bars.timestamp.iloc[entry_i] if "timestamp" in bars else entry_i
        exit_ts = bars.timestamp.iloc[exit_i] if "timestamp" in bars else exit_i
        trades.append({
            "symbol": symbol, "timeframe": timeframe, "strategy": strategy_id,
            "side": "BUY", "sample_period": sample_period,
            "entry_time": str(entry_ts), "exit_time": str(exit_ts),
            "entry_price": entry, "exit_price": exit_price,
            "stop_loss": stop, "take_profit": target,
            "gross_r": gross_r, "cost_r": cost_r, "net_r": gross_r-cost_r,
            "net_return_pct": (exit_price/entry-1)*100-fee_slippage_pct,
            "exit_reason": reason, "bars_held": exit_i-entry_i+1,
        })
        i = exit_i+1
    return pd.DataFrame(trades)


def _timeframe_results(symbol, timeframe, frame, fee, minimum_trades,
                       stop_atr, target_r, max_hold_hours):
    output, all_oos = {}, []
    segments = contiguous_segments(frame, timeframe)
    for strategy_id in STRATEGY_IDS:
        spec = STRATEGY_SPECS[strategy_id]
        train_parts, test_parts = [], []
        tested = overlay_events = lock_bars = total_bars = 0
        failure = None
        for segment in segments:
            try:
                signals = evaluate_strategy(segment, strategy_id)
            except DataInsufficient as exc:
                failure = str(exc)
                continue
            tested += 1
            if spec["type"] == "overlay":
                overlay_events += int((signals.overlay_signal != "NEUTRAL").sum())
                continue
            if spec["type"] == "filter":
                lock = signals.lockout.fillna(False) if "lockout" in signals else pd.Series(False, index=signals.index)
                lock_bars += int(lock.sum())
                total_bars += len(lock)
                continue
            split = max(1, min(len(segment)-1, int(len(segment)*0.60)))
            train = simulate_spot(symbol, timeframe, segment, signals, strategy_id, fee,
                                  stop_atr, target_r, max_hold_hours, 0, split, "IN_SAMPLE")
            test = simulate_spot(symbol, timeframe, segment, signals, strategy_id, fee,
                                 stop_atr, target_r, max_hold_hours, split, len(segment), "OUT_OF_SAMPLE")
            if not train.empty: train_parts.append(train)
            if not test.empty: test_parts.append(test)

        train = pd.concat(train_parts, ignore_index=True) if train_parts else pd.DataFrame()
        test = pd.concat(test_parts, ignore_index=True) if test_parts else pd.DataFrame()
        ins, oos = _metrics(train), _metrics(test)
        if tested == 0:
            output[strategy_id] = {
                "status": "DATA INSUFFICIENT", "reason": failure or "no valid continuous segment",
                "strategy_type": spec["type"], "segments_tested": 0,
                "in_sample": ins, "out_of_sample": oos,
            }
        elif spec["type"] == "overlay":
            oos["overlay_events"] = overlay_events
            output[strategy_id] = {
                "status": "OVERLAY ONLY — NOT EXECUTABLE", "strategy_type": "overlay",
                "segments_tested": tested, "in_sample": ins, "out_of_sample": oos,
            }
        elif spec["type"] == "filter":
            oos.update({"lockout_bars": lock_bars, "total_bars": total_bars,
                        "lockout_pct": round(100*lock_bars/total_bars, 2) if total_bars else None})
            output[strategy_id] = {
                "status": "FILTER ONLY — NO STANDALONE TRADES", "strategy_type": "filter",
                "segments_tested": tested, "in_sample": ins, "out_of_sample": oos,
            }
        else:
            status = ("DATA INSUFFICIENT" if oos["trades"] < minimum_trades else
                      "PROMISING — NEEDS WALK-FORWARD" if oos["profit_factor"] is not None
                      and oos["profit_factor"] >= 1.20 and oos["net_r"] > 0 else
                      "EXPERIMENTAL — NOT APPROVED")
            output[strategy_id] = {
                "status": status, "strategy_type": spec["type"], "segments_tested": tested,
                "in_sample": ins, "out_of_sample": oos,
            }
            if not test.empty: all_oos.append(test)
    return output, all_oos


def run_research(data_dir="data", output_dir="backtest_results/spot_strategy_engines",
                 fee_slippage_pct=0.12, minimum_oos_trades=30,
                 stop_atr=1.5, target_r=2.5, max_hold_hours=24):
    root = Path(data_dir)
    manifest_path = root/"download_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("DATA INSUFFICIENT: download_manifest.json missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("exchange") != "Binance" or manifest.get("market_type") != "spot":
        raise RuntimeError("Refusing non-Binance or non-spot data")
    folders = sorted(p for p in root.iterdir()
                     if p.is_dir() and not p.name.startswith(".") and p.name.endswith("USDT"))
    summary, trade_frames = {}, []
    for folder in folders:
        frames, statuses = load_market_timeframes(folder)
        summary[folder.name] = {"data": statuses, "strategies": {}}
        for tf in ALL_TIMEFRAMES:
            if tf not in frames: continue
            result, trades = _timeframe_results(
                folder.name, tf, frames[tf], fee_slippage_pct, minimum_oos_trades,
                stop_atr, target_r, max_hold_hours,
            )
            summary[folder.name]["strategies"][tf] = result
            trade_frames.extend(trades)

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    all_trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    all_trades.to_csv(output/"spot_strategy_oos_trades.csv", index=False)
    (output/"spot_strategy_summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    coin_rows = []
    for symbol, item in summary.items():
        for tf, engines in item["strategies"].items():
            for sid, record in engines.items():
                m = record.get("out_of_sample", {})
                coin_rows.append({"symbol": symbol, "timeframe": tf, "strategy": sid,
                    "status": record.get("status"), "oos_trades": m.get("trades", 0),
                    "oos_win_rate_pct": m.get("win_rate_pct"), "oos_profit_factor": m.get("profit_factor"),
                    "oos_net_r": m.get("net_r"), "oos_max_drawdown_r": m.get("max_drawdown_r")})
    per_coin = pd.DataFrame(coin_rows)
    per_coin.to_csv(output/"per_coin_strategy_leaderboard.csv", index=False)

    aggregate = []
    if not all_trades.empty:
        for (tf, sid), group in all_trades.groupby(["timeframe", "strategy"]):
            m = _metrics(group.sort_values("entry_time").reset_index(drop=True))
            markets = int(group.symbol.nunique())
            status = ("DATA INSUFFICIENT" if m["trades"] < minimum_oos_trades or markets < 5 else
                      "PROMISING — NEEDS WALK-FORWARD" if m["profit_factor"] is not None
                      and m["profit_factor"] >= 1.20 and m["net_r"] > 0 else
                      "EXPERIMENTAL — NOT APPROVED")
            aggregate.append({"timeframe": tf, "strategy": sid, "markets": markets,
                "status": status, "oos_trades": m["trades"], "oos_win_rate_pct": m["win_rate_pct"],
                "oos_profit_factor": m["profit_factor"], "oos_net_r": m["net_r"],
                "oos_max_drawdown_r": m["max_drawdown_r"]})
    global_board = pd.DataFrame(aggregate)
    if not global_board.empty:
        rank = {"PROMISING — NEEDS WALK-FORWARD": 0, "EXPERIMENTAL — NOT APPROVED": 1, "DATA INSUFFICIENT": 2}
        global_board["_rank"] = global_board.status.map(rank).fillna(3)
        global_board = global_board.sort_values(
            ["_rank", "oos_profit_factor", "oos_net_r"],
            ascending=[True, False, False], na_position="last").drop(columns="_rank")
    global_board.to_csv(output/"global_oos_strategy_leaderboard.csv", index=False)
    report = [
        "# Crypto Sniper — Spot Strategy Research", "",
        f"Markets analysed: {len(folders)}",
        f"Round-trip fee + slippage: {fee_slippage_pct:.4f}% of entry notional",
        f"Risk model: {stop_atr:.2f} ATR stop, {target_r:.2f}R target, {max_hold_hours:g}h maximum hold",
        "Spot only: BUY opens spot; bearish/short cues only exit to USDT. No short selling, leverage or futures orders.",
        "Signals use completed candles; entries/exits fill no earlier than the next candle open.",
        "Stop gaps use the worse open; ambiguous same-candle stop/target events assume stop first.",
        "Each continuous data segment is split chronologically 60% in-sample / 40% out-of-sample.",
        "Funding, basis, OI and CVD require genuine aligned feature data. Missing features are DATA INSUFFICIENT.",
        "Pooled R is a trade-level research summary, not capital-weighted portfolio return or a probability forecast.",
        "No strategy is auto-approved for live use. No exchange orders are sent.", "",
        "| Timeframe | Strategy | Markets | OOS trades | PF | Net R | Max DD R | Status |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in global_board.to_dict("records"):
        report.append(f"| {r['timeframe']} | {r['strategy']} | {r['markets']} | {r['oos_trades']} | {r['oos_profit_factor']} | {r['oos_net_r']} | {r['oos_max_drawdown_r']} | {r['status']} |")
    (output/"spot_strategy_report.md").write_text("\n".join(report)+"\n", encoding="utf-8")
    return {"markets": len(folders), "oos_trades": len(all_trades),
            "summary": str(output/"spot_strategy_summary.json"),
            "leaderboard": str(output/"global_oos_strategy_leaderboard.csv")}


def main():
    p = argparse.ArgumentParser(description="Full-market Binance Spot research for 12 independent engines")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--output-dir", default="backtest_results/spot_strategy_engines")
    p.add_argument("--fee-slippage-pct", type=float, default=0.12)
    p.add_argument("--minimum-oos-trades", type=int, default=30)
    p.add_argument("--stop-atr", type=float, default=1.5)
    p.add_argument("--target-r", type=float, default=2.5)
    p.add_argument("--max-hold-hours", type=float, default=24)
    a = p.parse_args()
    if (not math.isfinite(a.fee_slippage_pct) or a.fee_slippage_pct < 0 or
        a.minimum_oos_trades < 1 or a.stop_atr <= 0 or a.target_r <= 0 or a.max_hold_hours <= 0):
        p.error("invalid cost, sample floor or risk settings")
    result = run_research(a.data_dir, a.output_dir, a.fee_slippage_pct,
                          a.minimum_oos_trades, a.stop_atr, a.target_r, a.max_hold_hours)
    print(f"Markets analysed: {result['markets']}; OOS trade rows: {result['oos_trades']}")
    print(f"Leaderboard: {result['leaderboard']}")
    print("RESEARCH ONLY — no strategy auto-approval or exchange orders.")


if __name__ == "__main__":
    main()
