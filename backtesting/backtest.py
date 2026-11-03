"""Historical simulation for the project's LONG/SHORT signals.

Uses completed candles, next-15m-open entries, explicit costs, and conservative
gap-aware stop execution. No exchange orders are ever submitted.
"""
from pathlib import Path
import argparse
import json
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
        # Input timestamp is candle OPEN; the row is only available at candle CLOSE.
        close_ns = (pd.DatetimeIndex(f.timestamp) + pd.to_timedelta(TF_MS[tf], unit="ms")).asi8
        out[tf] = (f, close_ns)
    return out


def last_closed(pair, at_ns):
    frame, close_ns = pair
    idx = int(np.searchsorted(close_ns, at_ns, side="right") - 1)
    return None if idx < 0 else frame.iloc[idx]


def macro_at(prepared, at_ns):
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
        # No historical live quote exists; the signal candle close is the proxy.
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


def _stop_fill_at_open(side, open_price, stop):
    # If the market gaps through a stop, a stop-market order is not guaranteed
    # to fill at the trigger. Model the worse opening price instead.
    if side == "LONG" and open_price <= stop:
        return open_price
    if side == "SHORT" and open_price >= stop:
        return open_price
    return stop


def simulate(sig, bars, entry_i, max_hold=96, fee_slippage_pct=0.12,
             funding_bps_per_8h=0.0):
    if entry_i >= len(bars):
        return None
    entry_bar = bars.iloc[entry_i]
    entry = float(entry_bar.open)
    stop = float(sig.stop_loss)
    if not np.isfinite(entry) or entry <= 0:
        return None
    # A gap at entry can put price beyond the signal's invalidation level.
    if (sig.side == "LONG" and entry <= stop) or (sig.side == "SHORT" and entry >= stop):
        return None
    risk = entry - stop if sig.side == "LONG" else stop - entry
    if not np.isfinite(risk) or risk <= 0 or risk / entry > 0.03:
        return None

    tp1 = entry + (1.5 * risk if sig.side == "LONG" else -1.5 * risk)
    tp2 = entry + (2.5 * risk if sig.side == "LONG" else -2.5 * risk)
    remaining, gross_r, tp1_hit = 1.0, 0.0, False
    end_i = min(entry_i + max_hold, len(bars) - 1)
    exit_price, exit_reason, exit_i = float(bars.iloc[end_i].close), "TIME_EXIT", end_i

    for j in range(entry_i, end_i + 1):
        b = bars.iloc[j]
        o, h, l, c = map(float, (b.open, b.high, b.low, b.close))
        if sig.side == "LONG":
            stop_hit, one_hit, two_hit = l <= stop, h >= tp1, h >= tp2
        else:
            stop_hit, one_hit, two_hit = h >= stop, l <= tp1, l <= tp2

        # Stop first if the same OHLC bar contains stop and target.
        if stop_hit:
            fill = _stop_fill_at_open(sig.side, o, stop)
            stop_r = ((fill - entry) if sig.side == "LONG" else (entry - fill)) / risk
            gross_r -= remaining * abs(stop_r)
            exit_price, exit_reason, exit_i, remaining = fill, (
                "STOP_AFTER_TP1" if tp1_hit else "STOP_LOSS"
            ), j, 0.0
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
            gross_r += remaining * ((c - entry) if sig.side == "LONG" else (entry - c)) / risk
            exit_price, exit_reason, exit_i, remaining = c, "TIME_EXIT", j, 0.0

    risk_pct = risk / entry * 100.0
    # fee_slippage_pct is the estimated combined round-trip cost on notional.
    cost_r = (fee_slippage_pct / 100.0) / max(risk_pct / 100.0, 1e-12)
    held_hours = max(
        0.0,
        (pd.Timestamp(bars.iloc[exit_i].timestamp) - pd.Timestamp(entry_bar.timestamp)).total_seconds() / 3600.0
    )
    # Funding is a sensitivity estimate, not historical Bitget funding data.
    funding_pct = (funding_bps_per_8h / 10_000.0) * (held_hours / 8.0)
    funding_r = funding_pct / max(risk_pct / 100.0, 1e-12)
    return {
        "symbol": sig.symbol, "side": sig.side, "signal_time": sig.timestamp,
        "entry_time": str(entry_bar.timestamp), "entry": entry, "stop_loss": stop,
        "tp1": tp1, "tp2": tp2, "exit_time": str(bars.iloc[exit_i].timestamp),
        "exit": exit_price, "exit_reason": exit_reason, "tp1_hit": bool(tp1_hit),
        "score": sig.score, "setup": sig.setup, "gross_r": gross_r,
        "risk_pct": risk_pct, "estimated_cost_pct": fee_slippage_pct,
        "estimated_cost_r": cost_r, "held_hours": round(held_hours, 4),
        "funding_bps_per_8h_assumption": funding_bps_per_8h,
        "estimated_funding_r": funding_r, "net_r": gross_r - cost_r - funding_r,
    }


def run_symbol(symbol, target_frames, btc_frames, eth_frames, fee_slippage_pct=0.12,
               max_hold=96, funding_bps_per_8h=0.0):
    target, btc, eth = prepare(target_frames), prepare(btc_frames), prepare(eth_frames)
    fast = target["15m"][0]
    trades, next_signal_i = [], 100
    for i in range(100, len(fast) - 1):
        if i < next_signal_i:
            continue
        sig = make_signal(symbol, target, btc, eth, i)
        if sig is None:
            continue
        trade = simulate(sig, fast, i + 1, max_hold, fee_slippage_pct, funding_bps_per_8h)
        if trade is None:
            continue
        trades.append(trade)
        # A new signal is allowed only after the previous simulated exit candle.
        exit_stamp = pd.Timestamp(trade["exit_time"])
        matched = np.flatnonzero(pd.DatetimeIndex(fast.timestamp) == exit_stamp)
        next_signal_i = int(matched[0]) + 1 if len(matched) else i + 2
    return pd.DataFrame(trades)


def summarize(df):
    if df.empty:
        return {"trades": 0, "win_rate_pct": None, "profit_factor": None,
                "net_r": 0.0, "max_drawdown_r": 0.0,
                "tp1_hit_rate_pct": None, "note": "No qualifying trades found."}
    r = df.net_r.astype(float)
    gains, losses = float(r[r > 0].sum()), float(-r[r < 0].sum())
    equity = pd.concat([pd.Series([0.0]), r.cumsum().reset_index(drop=True)], ignore_index=True)
    drawdown = equity - equity.cummax()
    return {
        "trades": int(len(df)), "win_rate_pct": round(float((r > 0).mean() * 100), 2),
        "profit_factor": round(gains / losses, 3) if losses else None,
        "net_r": round(float(r.sum()), 3), "average_r": round(float(r.mean()), 3),
        "max_drawdown_r": round(float(drawdown.min()), 3),
        "tp1_hit_rate_pct": round(float(df.tp1_hit.mean() * 100), 2),
        "long_trades": int((df.side == "LONG").sum()),
        "short_trades": int((df.side == "SHORT").sum()),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="data")
    p.add_argument("--symbols", default="auto")
    p.add_argument("--fee-slippage-pct", type=float, default=0.12,
                   help="Estimated combined round-trip fee + slippage as percent of notional")
    p.add_argument("--funding-bps-per-8h", type=float, default=0.0,
                   help="Optional funding sensitivity assumption in bps per 8h; not historical funding data")
    p.add_argument("--max-hold-bars", type=int, default=96)
    a = p.parse_args()
    if not np.isfinite(a.fee_slippage_pct) or a.fee_slippage_pct < 0:
        p.error("--fee-slippage-pct must be finite and >= 0")
    if not np.isfinite(a.funding_bps_per_8h):
        p.error("--funding-bps-per-8h must be finite")
    if a.max_hold_bars < 1:
        p.error("--max-hold-bars must be >= 1")

    root = Path(a.data_dir)
    btc, eth = load_symbol_folder(root / "BTCUSDT"), load_symbol_folder(root / "ETHUSDT")
    if a.symbols.strip().lower() == "auto":
        symbols = sorted(p.name.upper() for p in root.iterdir() if p.is_dir()
                         and p.name.upper().endswith("USDT")
                         and all(len(list(p.glob(f"*{tf}*.csv"))) +
                                 len(list(p.glob(f"*{tf}*.xlsx"))) +
                                 len(list(p.glob(f"*{tf}*.xls"))) == 1 for tf in TFS))
    else:
        symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    if not symbols:
        raise RuntimeError("No eligible symbol datasets found. DATA INSUFFICIENT.")

    reports, all_trades = {}, []
    for symbol in symbols:
        try:
            frames = load_symbol_folder(root / symbol)
            trades = run_symbol(symbol, frames, btc, eth, a.fee_slippage_pct,
                                a.max_hold_bars, a.funding_bps_per_8h)
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
    lines = [
        "# A&T TrendHive Historical Backtest", "",
        "Signals only; no orders submitted. Results are a historical simulation, not a forecast.",
        f"**Symbols evaluated:** {len(reports)} | **Trades:** {len(combined)}",
        f"**Round-trip cost assumption:** {a.fee_slippage_pct:.4f}% notional | "
        f"**Funding sensitivity:** {a.funding_bps_per_8h:.4f} bps per 8h", "",
        "A zero funding assumption means funding is omitted. Funding sensitivity is not actual historical funding.",
        "Stop gaps are filled at the worse bar open. If stop and target both occur in one OHLC bar, stop is assumed first.",
        "Only datasets with continuous, UTC-aligned candles are accepted. Historical live quote is unavailable; signal close is a proxy.",
        "", "| Symbol | Trades | Win rate | Profit factor | Net R | Max DD (R) | TP1 hit rate | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---|"
    ]
    for sym, m in reports.items():
        if "error" in m:
            lines.append(f"| {sym} | 0 | — | — | — | — | — | DATA INSUFFICIENT: {m['error']} |")
        else:
            lines.append(f"| {sym} | {m['trades']} | {m.get('win_rate_pct')} | {m.get('profit_factor')} | "
                         f"{m['net_r']} | {m['max_drawdown_r']} | {m.get('tp1_hit_rate_pct')} | OK |")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print((output / "report.md").read_text())


if __name__ == "__main__":
    main()
