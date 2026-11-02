"""Walk-forward historical simulation of the project's existing LONG/SHORT rules.
No exchange orders are submitted. Uses closed candles and next-bar-open entries.
"""
from pathlib import Path
import argparse, json
import numpy as np
import pandas as pd
from indicators import enrich, trend
from strategy import _candidate
from backtesting.data_loader import load_symbol_folder

TFS = ("15m", "1h", "4h", "1d")
TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

def prepare(frames):
    out = {}
    for tf in TFS:
        f = enrich(frames[tf])
        # Each row's timestamp is candle open; it becomes available at candle close.
        close_ns = (pd.DatetimeIndex(f.timestamp) + pd.to_timedelta(TF_MS[tf], unit="ms")).asi8
        out[tf] = (f, close_ns)
    return out

def last_closed(pair, at_ns):
    frame, close_ns = pair
    idx = int(np.searchsorted(close_ns, at_ns, side="right") - 1)
    return None if idx < 0 else frame.iloc[idx]

def macro_at(prepared, at_ns):
    # Match the live exchange.py macro filter: completed 4h EMA9/21/50 alignment.
    row = last_closed(prepared["4h"], at_ns)
    if row is None:
        return "UNKNOWN"
    if row.close > row.ema9 > row.ema21 > row.ema50:
        return "BULLISH"
    if row.close < row.ema9 < row.ema21 < row.ema50:
        return "BEARISH"
    return "NEUTRAL"

def make_signal(symbol, target, btc, eth, i):
    fast = target["15m"][0]
    row15 = fast.iloc[i]
    signal_close_ns = int((pd.Timestamp(row15.timestamp) + pd.Timedelta(minutes=15)).value)
    rows = {tf: last_closed(target[tf], signal_close_ns) for tf in TFS}
    if any(row is None for row in rows.values()):
        return None
    btc_t, eth_t = macro_at(btc, signal_close_ns), macro_at(eth, signal_close_ns)
    candidates = []
    for side in ("LONG", "SHORT"):
        wanted = "BULLISH" if side == "LONG" else "BEARISH"
        opposite = "BEARISH" if side == "LONG" else "BULLISH"
        if trend(rows["1h"]) != wanted or trend(rows["4h"]) != wanted or trend(rows["1d"]) == opposite:
            continue
        if btc_t == opposite or eth_t == opposite:
            continue
        # Historical signal only knows the completed 15m close. The live quote is
        # unavailable historically, so use that close as a conservative proxy.
        sig = _candidate(rows["15m"], side, float(rows["15m"].close))
        if sig is None or sig.score < 7:
            continue
        sig.symbol = symbol
        sig.timestamp = str(pd.Timestamp(row15.timestamp) + pd.Timedelta(minutes=15))
        if btc_t == wanted and eth_t == wanted:
            sig.score = min(10, sig.score + 1)
            sig.reasons.append("BTC and ETH support the direction")
        candidates.append(sig)
    return max(candidates, key=lambda s: s.score) if candidates else None

def simulate(sig, bars, entry_i, max_hold=96):
    if entry_i >= len(bars):
        return None
    entry = float(bars.iloc[entry_i].open)
    stop = float(sig.stop_loss)
    risk = entry - stop if sig.side == "LONG" else stop - entry
    if not np.isfinite(entry) or entry <= 0 or risk <= 0 or risk / entry > 0.03:
        return None
    tp1 = entry + (1.5 * risk if sig.side == "LONG" else -1.5 * risk)
    tp2 = entry + (2.5 * risk if sig.side == "LONG" else -2.5 * risk)
    remaining, gross_r, tp1_hit = 1.0, 0.0, False
    end_i = min(entry_i + max_hold, len(bars) - 1)
    exit_price, exit_reason, exit_i = float(bars.iloc[end_i].close), "TIME_EXIT", end_i
    for j in range(entry_i, end_i + 1):
        b = bars.iloc[j]
        if sig.side == "LONG":
            stop_hit, one_hit, two_hit = float(b.low) <= stop, float(b.high) >= tp1, float(b.high) >= tp2
        else:
            stop_hit, one_hit, two_hit = float(b.high) >= stop, float(b.low) <= tp1, float(b.low) <= tp2
        # Conservative OHLC assumption if stop and a target both touched in one bar.
        if stop_hit:
            gross_r -= remaining
            exit_price, exit_reason, exit_i, remaining = stop, ("STOP_AFTER_TP1" if tp1_hit else "STOP_LOSS"), j, 0.0
            break
        if not tp1_hit and one_hit:
            gross_r += 0.5 * 1.5
            remaining -= 0.5
            tp1_hit = True
        if two_hit and remaining > 0:
            gross_r += remaining * 2.5
            remaining = 0.0
            exit_price, exit_reason, exit_i = tp2, "TP2", j
            break
        if j == end_i and remaining > 0:
            px = float(b.close)
            gross_r += remaining * ((px - entry) if sig.side == "LONG" else (entry - px)) / risk
            exit_price, exit_reason, exit_i, remaining = px, "TIME_EXIT", j, 0.0
    risk_pct = risk / entry * 100
    return {"symbol": sig.symbol, "side": sig.side, "signal_time": sig.timestamp,
            "entry_time": str(bars.iloc[entry_i].timestamp), "entry": entry,
            "stop_loss": stop, "tp1": tp1, "tp2": tp2,
            "exit_time": str(bars.iloc[exit_i].timestamp), "exit": exit_price,
            "exit_reason": exit_reason, "tp1_hit": bool(tp1_hit), "score": sig.score,
            "setup": sig.setup, "gross_r": gross_r, "risk_pct": risk_pct}

def run_symbol(symbol, target_frames, btc_frames, eth_frames, fee_slippage_pct=0.12, max_hold=96):
    target, btc, eth = prepare(target_frames), prepare(btc_frames), prepare(eth_frames)
    fast = target["15m"][0]
    trades, next_signal_i = [], 100
    for i in range(100, len(fast) - 1):
        if i < next_signal_i:
            continue
        sig = make_signal(symbol, target, btc, eth, i)
        if sig is None:
            continue
        trade = simulate(sig, fast, i + 1, max_hold=max_hold)
        if trade is None:
            continue
        # Approximate round-trip fees + slippage on full notional; convert to R.
        cost_r = (fee_slippage_pct / 100.0) / max(trade["risk_pct"] / 100.0, 1e-12)
        trade["estimated_cost_pct"] = fee_slippage_pct
        trade["net_r"] = trade["gross_r"] - cost_r
        trades.append(trade)
        # One position at a time; wait until the simulated trade exits.
        try:
            next_signal_i = int(fast.index[fast.timestamp == pd.Timestamp(trade["exit_time"])][0]) + 1
        except Exception:
            next_signal_i = i + 2
    return pd.DataFrame(trades)

def summarize(df):
    if df.empty:
        return {"trades": 0, "win_rate_pct": None, "profit_factor": None, "net_r": 0.0,
                "max_drawdown_r": 0.0, "tp1_hit_rate_pct": None, "note": "No qualifying trades found."}
    r = df.net_r.astype(float)
    gains, losses = float(r[r > 0].sum()), float(-r[r < 0].sum())
    equity = r.cumsum()
    drawdown = equity - equity.cummax()
    return {"trades": int(len(df)), "win_rate_pct": round(float((r > 0).mean() * 100), 2),
            "profit_factor": round(gains / losses, 3) if losses else None,
            "net_r": round(float(r.sum()), 3), "average_r": round(float(r.mean()), 3),
            "max_drawdown_r": round(float(drawdown.min()), 3),
            "tp1_hit_rate_pct": round(float(df.tp1_hit.mean() * 100), 2),
            "long_trades": int((df.side == "LONG").sum()), "short_trades": int((df.side == "SHORT").sum())}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="data")
    p.add_argument("--symbols", default="auto", help="auto = every complete symbol folder in data-dir")
    p.add_argument("--fee-slippage-pct", type=float, default=0.12)
    p.add_argument("--max-hold-bars", type=int, default=96)
    a = p.parse_args()
    root = Path(a.data_dir)
    btc = load_symbol_folder(root / "BTCUSDT")
    eth = load_symbol_folder(root / "ETHUSDT")
    reports, all_trades = {}, []
    if a.symbols.strip().lower() == "auto":
        symbols = sorted(p.name.upper() for p in root.iterdir()
                         if p.is_dir() and all((p / f"{tf}.csv").exists() or len(list(p.glob(f"*{tf}*.csv"))) == 1
                                               for tf in TFS))
    else:
        symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if not symbols:
        raise RuntimeError("No eligible altcoin datasets found. Download complete 15m/1h/4h/1d folders first.")
    for symbol in symbols:
        try:
            frames = load_symbol_folder(root / symbol)
            trades = run_symbol(symbol, frames, btc, eth, a.fee_slippage_pct, a.max_hold_bars)
            reports[symbol] = summarize(trades)
            if not trades.empty:
                all_trades.append(trades)
        except Exception as exc:
            reports[symbol] = {"trades": 0, "error": f"{type(exc).__name__}: {exc}",
                               "note": "DATA INSUFFICIENT or symbol skipped"}
    output = Path("backtest_results")
    output.mkdir(exist_ok=True)
    combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    combined.to_csv(output / "trades.csv", index=False)
    (output / "summary.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
    lines = ["# A&T TrendHive Full-Market Historical Backtest", "",
             "All complete eligible symbol folders are tested, including BTC/ETH; BTC/ETH also supply macro filters.",
             "Historical simulation only; not a guarantee of future results.", "",
             f"**Symbols tested:** {len(reports)} | **Total trades:** {len(combined)}", "",
             "| Symbol | Trades | Win rate | Profit factor | Net R | Max DD (R) | TP1 hit rate | Status |",
             "|---|---:|---:|---:|---:|---:|---:|---|"]
    for sym, m in reports.items():
        if "error" in m:
            lines.append(f"| {sym} | 0 | — | — | — | — | — | DATA INSUFFICIENT: {m['error']} |")
        else:
            lines.append(f"| {sym} | {m['trades']} | {m.get('win_rate_pct')} | {m.get('profit_factor')} | {m['net_r']} | {m['max_drawdown_r']} | {m.get('tp1_hit_rate_pct')} | OK |")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print((output / "report.md").read_text())

if __name__ == "__main__":
    main()
