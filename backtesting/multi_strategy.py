"""Five independent LONG/SHORT strategy engines and a reproducible research runner.

All decisions use completed candles; entries are simulated at the next candle open.
No exchange orders are sent. Results are research-only and include estimated costs.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from indicators import enrich
from strategy import _candidate
from backtesting.backtest import simulate
from backtesting.data_loader import load_file, validate_timeframe

TIMEFRAMES = ("15m", "30m", "1h", "2h", "4h", "1d")
INTERVAL_MS = {"15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
               "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}
SYMBOLS = ("BTCUSDT", "ETHUSDT")
STRATEGIES = ("trend_pullback", "breakout_retest", "momentum_continuation",
              "range_reversal", "volatility_expansion")


def _signal(row, side, name, stop, score=7):
    """Create a simulator-compatible signal with strict risk validation."""
    entry = float(row.close)
    atr = float(row.atr)
    stop = float(stop)
    risk = entry - stop if side == "LONG" else stop - entry
    if not all(np.isfinite(v) and v > 0 for v in (entry, atr, stop, risk)):
        return None
    if risk < 0.65 * atr or risk > 2.0 * atr or risk / entry > 0.03:
        return None
    if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
        return None
    return SimpleNamespace(symbol="", side=side, stop_loss=stop, score=int(score),
                           setup=name, timestamp="")


def _strategy_signal(row, side, name):
    """Independent, causal entry rules for each engine."""
    long = side == "LONG"
    close, atr = float(row.close), float(row.atr)
    adx, rsi = float(row.adx), float(row.rsi)
    vol = float(row.vol_ratio)
    swing_low, swing_high = float(row.recent_swing_low), float(row.recent_swing_high)
    bullish = close > row.open
    bearish = close < row.open
    trend_up = row.close > row.ema21 > row.ema50 and row.ema21 > row.ema21_prev3
    trend_down = row.close < row.ema21 < row.ema50 and row.ema21 < row.ema21_prev3

    if name == "trend_pullback":
        candidate = _candidate(row, side, close)
        # Preserve the research engine's historical >=7 quality baseline.
        # The live scanner alone may opt into lower thresholds explicitly.
        if candidate is None or candidate.score < 7:
            return None
        candidate.setup = name
        return candidate

    if name == "breakout_retest":
        valid = bool(row.breakout_retest_long) if long else bool(row.breakout_retest_short)
        valid &= trend_up if long else trend_down
        valid &= (48 <= rsi <= 67 and bullish) if long else (33 <= rsi <= 52 and bearish)
        valid &= 14 <= adx <= 45 and 0.6 <= vol <= 5.0
        stop = swing_low - 0.12 * atr if long else swing_high + 0.12 * atr
        return _signal(row, side, name, stop, 8) if valid else None

    if name == "momentum_continuation":
        macd_fast = row.macd_line
        macd_signal = row.macd_signal
        valid = trend_up and macd_fast > macd_signal and 52 <= rsi <= 68
        if not long:
            valid = trend_down and macd_fast < macd_signal and 32 <= rsi <= 48
        valid &= 18 <= adx <= 48 and 0.7 <= vol <= 4.5
        valid &= (bullish and row.close_location >= 0.60) if long else (bearish and row.close_location <= 0.40)
        stop = swing_low - 0.10 * atr if long else swing_high + 0.10 * atr
        return _signal(row, side, name, stop, 8) if valid else None

    if name == "range_reversal":
        width = max(float(row.range_high - row.range_low), 1e-12)
        position = (close - float(row.range_low)) / width
        valid = position <= 0.15 and rsi <= 38 and bullish and row.close_location >= 0.62
        if not long:
            valid = position >= 0.85 and rsi >= 62 and bearish and row.close_location <= 0.38
        valid &= adx <= 32 and vol >= 0.55
        stop = min(float(row.low), swing_low) - 0.12 * atr if long else max(float(row.high), swing_high) + 0.12 * atr
        return _signal(row, side, name, stop, 7) if valid else None

    if name == "volatility_expansion":
        valid = float(row.range) >= 1.10 * atr and vol >= 1.50
        valid &= close > row.prior_high and trend_up if long else close < row.prior_low and trend_down
        valid &= (bullish and row.close_location >= 0.70) if long else (bearish and row.close_location <= 0.30)
        valid &= adx >= 18
        stop = swing_low - 0.10 * atr if long else swing_high + 0.10 * atr
        return _signal(row, side, name, stop, 8) if valid else None

    raise ValueError(f"Unknown strategy engine: {name}")


def enrich_for_engines(frame):
    """Add causal MACD features on top of shared validated indicators."""
    x = enrich(frame)
    fast = x.close.ewm(span=12, adjust=False).mean()
    slow = x.close.ewm(span=26, adjust=False).mean()
    x["macd_line"] = fast - slow
    x["macd_signal"] = x.macd_line.ewm(span=9, adjust=False).mean()
    x = x.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)
    return x


def contiguous_segments(frame, timeframe):
    stamps = pd.DatetimeIndex(frame.timestamp)
    interval = INTERVAL_MS[timeframe]
    values = stamps.asi8 // 1_000_000
    if np.any(values % interval):
        raise ValueError(f"timestamps are not aligned to {timeframe} UTC boundaries")
    cuts = (np.flatnonzero(np.diff(values) != interval) + 1).tolist()
    return [part.reset_index(drop=True) for part in np.split(frame, cuts) if len(part)]


def run_engine_segment(symbol, timeframe, frame, engine, fee_slippage_pct):
    bars = enrich_for_engines(frame)
    trades = []
    next_entry_index = 100
    max_hold = max(1, 86_400_000 // INTERVAL_MS[timeframe])
    for i in range(100, len(bars) - 1):
        if i < next_entry_index:
            continue
        row = bars.iloc[i]
        candidates = []
        for side in ("LONG", "SHORT"):
            sig = _strategy_signal(row, side, engine)
            if sig is not None:
                sig.symbol = symbol
                sig.timestamp = str(pd.Timestamp(row.timestamp) +
                                    pd.Timedelta(milliseconds=INTERVAL_MS[timeframe]))
                candidates.append(sig)
        if not candidates:
            continue
        sig = max(candidates, key=lambda item: item.score)
        trade = simulate(sig, bars, i + 1, max_hold, fee_slippage_pct, 0.0)
        if trade is None:
            continue
        trade["strategy"] = engine
        trade["timeframe"] = timeframe
        trades.append(trade)
        exits = np.flatnonzero(pd.DatetimeIndex(bars.timestamp) == pd.Timestamp(trade["exit_time"]))
        next_entry_index = int(exits[0]) + 1 if len(exits) else i + 2
    return pd.DataFrame(trades)


def metrics(trades):
    if trades.empty:
        return {"status": "NO QUALIFYING TRADES", "trades": 0, "win_rate_pct": None,
                "profit_factor": None, "net_r": 0.0, "max_drawdown_r": 0.0}
    net = pd.to_numeric(trades.net_r, errors="coerce").dropna()
    wins = net[net > 0].sum()
    losses = -net[net < 0].sum()
    equity = net.cumsum()
    drawdown = equity - equity.cummax().clip(lower=0)
    return {
        "status": "RESEARCH_ONLY",
        "trades": int(len(net)),
        "win_rate_pct": round(float((net > 0).mean() * 100), 2),
        "profit_factor": round(float(wins / losses), 3) if losses > 0 else None,
        "net_r": round(float(net.sum()), 3),
        "average_r": round(float(net.mean()), 4),
        "max_drawdown_r": round(float(drawdown.min()), 3),
        "long_trades": int((trades.side == "LONG").sum()),
        "short_trades": int((trades.side == "SHORT").sum()),
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backtest five independent BTC/ETH LONG/SHORT strategy engines")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--fee-slippage-pct", type=float, default=0.12)
    args = parser.parse_args()
    if not np.isfinite(args.fee_slippage_pct) or args.fee_slippage_pct < 0:
        parser.error("fee-slippage-pct must be finite and non-negative")

    root = Path(args.data_dir)
    manifest_path = root / "timeframe_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError("DATA INSUFFICIENT: timeframe_manifest.json is required")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    statuses = manifest.get("timeframes", {})
    report, all_trades = {}, []

    for tf in TIMEFRAMES:
        report[tf] = {}
        for symbol in SYMBOLS:
            key = symbol.replace("USDT", "")
            status = statuses.get(key, {}).get(tf, {}).get("status")
            path = root / symbol / f"{tf}.csv"
            if status != "VALID" or not path.exists():
                report[tf][symbol] = {"status": "DATA INSUFFICIENT", "reason": f"manifest={status}; file missing or not approved"}
                continue
            try:
                frame = load_file(path)
                validate_timeframe(frame, tf, symbol, allow_gaps=True)
                segments = contiguous_segments(frame, tf)
            except Exception as exc:
                report[tf][symbol] = {"status": "DATA INSUFFICIENT", "reason": str(exc)}
                continue

            report[tf][symbol] = {}
            for engine in STRATEGIES:
                pieces = []
                tested = 0
                for segment in segments:
                    if len(segment) < 120:
                        continue
                    tested += 1
                    result = run_engine_segment(symbol, tf, segment, engine, args.fee_slippage_pct)
                    if not result.empty:
                        pieces.append(result)
                trades = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
                report[tf][symbol][engine] = {
                    "continuous_segments_tested": tested,
                    **metrics(trades),
                }
                if not trades.empty:
                    all_trades.append(trades)

    output = Path("backtest_results")
    output.mkdir(exist_ok=True)
    combined = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    combined.to_csv(output / "multi_strategy_trades.csv", index=False)
    (output / "multi_strategy_summary.json").write_text(
        json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    lines = [
        "# Multi-Strategy BTC/ETH Backtest", "",
        "Research-only historical simulation. Signals use completed candles and enter at the next candle open.",
        "Five separate engines are evaluated independently for LONG and SHORT. No exchange orders are sent.",
        f"Estimated combined round-trip fee + slippage: {args.fee_slippage_pct:.4f}% of notional.",
        "Funding is not included because historical funding data is not part of this dataset.",
        "Stops are evaluated before targets when both occur in one OHLC candle. Data gaps split simulations.",
        "Win rate means positive net-R trades after estimated costs; it is not a probability forecast.",
        "", "| Timeframe | Symbol | Strategy | Trades | Win rate | PF | Net R | Max DD R |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for tf, symbols in report.items():
        for symbol, engines in symbols.items():
            if engines.get("status") == "DATA INSUFFICIENT":
                lines.append(f"| {tf} | {symbol} | — | — | — | — | — | — | DATA INSUFFICIENT: {engines['reason']} |")
                continue
            for engine, m in engines.items():
                lines.append(f"| {tf} | {symbol} | {engine} | {m['trades']} | {m['win_rate_pct']} | {m['profit_factor']} | {m['net_r']} | {m['max_drawdown_r']} |")
    (output / "multi_strategy_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
