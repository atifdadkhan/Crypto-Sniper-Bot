"""English spot BUY/EXIT cards and a persistent journal of PAPER signal outcomes.
No exchange orders are sent, and estimated outcomes are not actual account P&L.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

TERMINAL = frozenset({"CLOSED", "EXPIRED", "CANCELLED"})
ROUND_TRIP_COST_PCT = 0.12


def _utc(value):
    timestamp = pd.to_datetime(value, utc=True, errors="raise")
    if pd.isna(timestamp):
        raise ValueError("invalid timestamp")
    return timestamp.isoformat()


def _snapshot(frame, limit=40):
    return [
        {"timestamp": _utc(row.timestamp),
         **{k: float(getattr(row, k)) for k in ("open", "high", "low", "close", "volume")}}
        for row in frame.tail(limit).itertuples(index=False)
    ]


def _atr(frame):
    last_close = frame.close.shift(1)
    ranges = pd.concat((frame.high - frame.low,
                        (frame.high - last_close).abs(),
                        (frame.low - last_close).abs()), axis=1).max(axis=1)
    return ranges.ewm(alpha=1/14, adjust=False, min_periods=14).mean()


def market_condition(frame):
    """Classify using only the candles supplied, not any future bars."""
    if len(frame) < 50:
        return "CAUTIOUS"
    close = frame.close.astype(float)
    volume = frame.volume.astype(float)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    strong = (ema20.iloc[-1] > ema50.iloc[-1]
              and close.iloc[-1] > ema20.iloc[-1]
              and close.iloc[-1] > close.iloc[-4]
              and volume.iloc[-1] >= volume.tail(20).mean())
    return "STRONG" if strong else "CAUTIOUS"


def build_buy_plan(frame):
    """Offer a price range and staged exits; these are not actual exchange fills."""
    if len(frame) < 20:
        raise ValueError("at least 20 completed candles required")
    close = float(frame.close.iloc[-1])
    atr = float(_atr(frame).iloc[-1])
    if not (math.isfinite(close) and math.isfinite(atr) and close > 0 and atr > 0):
        raise ValueError("valid price and volatility required")
    half_band = min(atr*.10, close*.005)
    entry_low, entry_high = close-half_band, close+half_band
    stop = close-1.5*atr
    if stop <= 0 or stop >= entry_low:
        raise ValueError("invalid stop below entry range")
    risk = entry_high-stop
    condition = market_condition(frame)
    strong = condition == "STRONG"
    return {"market_condition": condition,
            "plan_mode": "TP1_TP2_MOONSHOT" if strong else "TP1_ONLY",
            "entry_price_low": entry_low, "entry_price_high": entry_high,
            "tp1": entry_high+1.5*risk,
            "tp2": entry_high+2.5*risk if strong else None,
            "stop_loss": stop, "risk_per_unit": risk,
            "moonshot": "TRAILING_STOP_AFTER_TP2" if strong else "DISABLED",
            "cost_estimate_pct": ROUND_TRIP_COST_PCT}


def _price(value):
    if value is None:
        return "Not active"
    value = float(value)
    digits = 2 if value >= 100 else (4 if value >= 0.1 else 8)
    return "$" + f"{value:,.{digits}f}"


def format_signal(signal):
    symbol = str(signal["symbol"])
    pair = symbol[:-4]+"/USDT" if symbol.endswith("USDT") else symbol
    strategy = signal.get("strategy_name", signal.get("strategy", "Unknown"))
    if signal["action"] == "EXIT_TO_USDT":
        return (f"SELL / EXIT TO USDT — {pair}\n"
                f"Strategy: {strategy}\n"
                f"Signal Price: {_price(signal['price'])}\n"
                "Action: Exit this strategy's open spot holding, if any.\n"
                "No short selling or futures.")
    if signal["action"] != "BUY":
        raise ValueError("only BUY and EXIT_TO_USDT are trade messages")
    p = signal["trade_plan"]
    strong = p["plan_mode"] == "TP1_TP2_MOONSHOT"
    return (f"BUY — {pair}\n"
            f"Strategy: {strategy}\n"
            f"Market Condition: {p['market_condition']}\n"
            f"Entry Price Range: {_price(p['entry_price_low'])} - {_price(p['entry_price_high'])}\n"
            f"TP1: {_price(p['tp1'])}\n"
            f"TP2: {_price(p['tp2']) if strong else 'Disabled (TP1 only)'}\n"
            f"Moonshot: {'Trailing profits after TP2, only while market stays strong' if strong else 'Disabled'}\n"
            f"Stop Loss: {_price(p['stop_loss'])}\n"
            f"Trade Plan: {'TP1, then TP2, then Moonshot with trailing protection' if strong else 'Exit at TP1; avoid extended targets'}\n"
            "Spot only | Signal plan, not a confirmed fill")


def _signal_id(signal):
    key = "|".join(str(signal[k]) for k in
                   ("symbol", "timeframe", "strategy", "action", "candle_close_utc"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


class SignalJournal:
    """SQLite signal history with immutable source-candle snapshots and events."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL, strategy TEXT NOT NULL,
                action TEXT NOT NULL, signal_candle_utc TEXT NOT NULL,
                status TEXT NOT NULL, state_json TEXT NOT NULL,
                original_candles_json TEXT NOT NULL,
                created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signal_market
                ON signals(symbol,timeframe,status);
            CREATE TABLE IF NOT EXISTS signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT NOT NULL REFERENCES signals(signal_id),
                event_candle_utc TEXT NOT NULL, event_type TEXT NOT NULL,
                price REAL, fraction REAL, candle_json TEXT NOT NULL,
                UNIQUE(signal_id,event_candle_utc,event_type)
            );
        """)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def _event(self, sid, timestamp, event_type, price=None, fraction=None, candle=None):
        self.conn.execute(
            "INSERT OR IGNORE INTO signal_events "
            "(signal_id,event_candle_utc,event_type,price,fraction,candle_json) "
            "VALUES (?,?,?,?,?,?)",
            (sid, timestamp, event_type, price, fraction,
             json.dumps(candle or {}, allow_nan=False)))

    def record(self, signal, frame):
        """Persist a newly emitted signal once, including its original candles."""
        sid = _signal_id(signal)
        bar_ts = _utc(signal.get("signal_candle_utc", signal["candle_close_utc"]))
        plan = signal.get("trade_plan")
        if signal["action"] == "BUY" and not plan:
            raise ValueError("BUY requires a trade plan")
        state = {"signal_id": sid, "strategy_name": signal.get("strategy_name"),
                 "signal_price": signal.get("price"),
                 "signal_strength": signal.get("signal_strength"),
                 "metadata": signal.get("metadata", {}),
                 "market_condition": plan["market_condition"] if plan else None,
                 "plan": plan, "entry_price": None, "entry_time_utc": None,
                 "exit_price": None, "exit_time_utc": None, "exit_reason": None,
                 "closed_fraction": 0.0, "weighted_exit": 0.0,
                 "net_return_pct": None, "tp1_hit": False, "tp2_hit": False,
                 "moonshot_active": False, "trail_stop": None,
                 "peak_price": None, "bars_waited": 0,
                 "signal_candle_utc": bar_ts,
                 "signal_close_utc": _utc(signal["candle_close_utc"]),
                 "last_candle_utc": bar_ts,
                 "status": "PENDING_ENTRY" if signal["action"] == "BUY" else "EXIT_SIGNAL"}
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, signal["symbol"], signal["timeframe"], signal["strategy"],
             signal["action"], bar_ts, state["status"],
             json.dumps(state, allow_nan=False),
             json.dumps(_snapshot(frame), allow_nan=False), now, now))
        if cur.rowcount:
            self._event(sid, bar_ts, "SIGNAL_EMITTED",
                        candle=_snapshot(frame, 1)[0])
        self.conn.commit()
        return sid

    def _sell(self, sid, state, ts, price, portion, reason, candle):
        state["closed_fraction"] = min(1.0, state["closed_fraction"]+portion)
        state["weighted_exit"] += float(price)*portion
        self._event(sid, ts, reason, float(price), portion, candle)

    def _finish(self, sid, state, ts, price, reason, candle):
        remainder = 1.0-state["closed_fraction"]
        if remainder > 1e-8:
            self._sell(sid, state, ts, price, remainder, reason, candle)
        state["status"] = "CLOSED"
        state["exit_price"] = float(price)
        state["exit_time_utc"] = ts
        state["exit_reason"] = reason
        state["net_return_pct"] = round(
            (state["weighted_exit"]/state["entry_price"]-1)*100
            - state["plan"]["cost_estimate_pct"], 5)
        self._event(sid, ts, "TRADE_CLOSED", float(price), candle=candle)

    def advance(self, symbol, timeframe, frame):
        """Simulate only unseen completed candles; stop wins same-bar ambiguity.

        Entry is assumed at the WORST edge of a touched entry range.
        P&L is a paper model, never verified execution.
        """
        if frame.empty:
            return
        bars = frame.reset_index(drop=True)
        timestamps = [_utc(t) for t in bars.timestamp]
        atr = _atr(bars)
        rows = self.conn.execute(
            "SELECT signal_id,state_json FROM signals WHERE symbol=? AND timeframe=? "
            "AND action='BUY' AND status NOT IN ('CLOSED','EXPIRED','CANCELLED')",
            (symbol, timeframe)).fetchall()
        for sid, raw in rows:
            s = json.loads(raw)
            p = s["plan"]
            for i, ts in enumerate(timestamps):
                if ts <= s["last_candle_utc"] or s["status"] in TERMINAL:
                    continue
                bar = bars.iloc[i]
                op, hi, lo = float(bar.open), float(bar.high), float(bar.low)
                candle = _snapshot(bars.iloc[i:i+1], 1)[0]
                s["last_candle_utc"] = ts
                if s["status"] == "PENDING_ENTRY":
                    s["bars_waited"] += 1
                    if lo <= p["entry_price_high"] and hi >= p["entry_price_low"]:
                        s["entry_price"], s["entry_time_utc"] = p["entry_price_high"], ts
                        s["status"] = "OPEN_PAPER"
                        self._event(sid, ts, "PAPER_ENTRY", s["entry_price"], candle=candle)
                    elif s["bars_waited"] >= 3:
                        s["status"] = "EXPIRED"
                        self._event(sid, ts, "ENTRY_EXPIRED", candle=candle)
                        continue
                    else:
                        continue
                # The previously established stop applies before new targets/trailing levels.
                stop = max(p["stop_loss"], s["trail_stop"] or 0)
                if op <= stop or lo <= stop:
                    fill = min(op, stop) if op <= stop else stop
                    self._finish(sid, s, ts, fill, "STOP_LOSS_OR_TRAIL", candle)
                    continue
                if not s["tp1_hit"] and hi >= p["tp1"]:
                    s["tp1_hit"] = True
                    if p["plan_mode"] == "TP1_ONLY":
                        self._finish(sid, s, ts, p["tp1"], "TP1_ONLY", candle)
                        continue
                    self._sell(sid, s, ts, p["tp1"], .5, "TP1", candle)
                    s["status"] = "TP1_PARTIAL"
                # Conditions can weaken after the signal; no look-ahead candles used.
                still_strong = market_condition(bars.iloc[:i+1]) == "STRONG"
                if s["tp1_hit"] and not s["tp2_hit"] and not still_strong:
                    self._finish(sid, s, ts, float(bar.close),
                                 "MARKET_WEAKENED_AFTER_TP1", candle)
                    continue
                if p["tp2"] is not None and s["tp1_hit"] and not s["tp2_hit"] and hi >= p["tp2"]:
                    s["tp2_hit"] = True
                    self._sell(sid, s, ts, p["tp2"], .3, "TP2", candle)
                    s["moonshot_active"] = True
                    s["status"] = "MOONSHOT_PAPER"
                    s["trail_stop"], s["peak_price"] = p["tp1"], hi
                elif s["moonshot_active"]:
                    if not still_strong:
                        self._finish(sid, s, ts, float(bar.close),
                                     "MOONSHOT_MOMENTUM_FADED", candle)
                        continue
                    s["peak_price"] = max(s["peak_price"], hi)
                    current_atr = float(atr.iloc[i])
                    if math.isfinite(current_atr) and current_atr > 0:
                        s["trail_stop"] = max(s["trail_stop"],
                                              s["peak_price"]-1.5*current_atr)
                started = pd.to_datetime(s["entry_time_utc"], utc=True)
                if (pd.to_datetime(ts, utc=True)-started).total_seconds() >= 86400:
                    self._finish(sid, s, ts, float(bar.close), "MAX_24H_HOLD", candle)
            self.conn.execute(
                "UPDATE signals SET status=?,state_json=?,updated_utc=? WHERE signal_id=?",
                (s["status"], json.dumps(s, allow_nan=False),
                 datetime.now(timezone.utc).isoformat(), sid))
        self.conn.commit()

    def export(self, path):
        """CSV for per-symbol/per-strategy future analysis; actual fills not claimed."""
        records = []
        for sid, symbol, tf, strategy, action, raw in self.conn.execute(
                "SELECT signal_id,symbol,timeframe,strategy,action,state_json FROM signals"):
            s = json.loads(raw)
            pnl = s["net_return_pct"]
            records.append({
                "signal_id": sid, "symbol": symbol, "timeframe": tf,
                "strategy": strategy, "strategy_name": s["strategy_name"],
                "action": action, "status": s["status"],
                "market_condition": s["market_condition"],
                "signal_price": s["signal_price"],
                "signal_candle_utc": s["signal_candle_utc"],
                "entry_time_utc": s["entry_time_utc"],
                "entry_price": s["entry_price"],
                "tp1_hit": s["tp1_hit"], "tp2_hit": s["tp2_hit"],
                "exit_time_utc": s["exit_time_utc"],
                "exit_price": s["exit_price"], "exit_reason": s["exit_reason"],
                "net_return_pct": pnl,
                "outcome": ("WIN" if pnl > 0 else "LOSS") if pnl is not None else "UNRESOLVED",
                "outcome_type": "PAPER_ESTIMATE_NOT_ACTUAL_FILL"})
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(records).to_csv(path, index=False)
        return records
