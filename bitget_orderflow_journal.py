"""Bitget perpetual orderflow alerts and conservative, candle-based PAPER outcomes.

Public market data only. No order placement or actual exchange fill accounting.
All 1h outcome candles must be fully closed and contiguous; unknown gaps are never
silently counted as wins. The first incomplete signal hour is not simulated.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from html import escape
from pathlib import Path


HOUR_MS = 3_600_000
ROUND_TRIP_COST_PCT = 0.12
MAX_HOLD_HOURS = 72


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _safe_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite trade level")
    return number


def trade_plan(signal):
    """Regime is derived from preceding CLOSED Bitget 1h candles, if available."""
    entry = _safe_number(signal.entry)
    stop = _safe_number(signal.stop_loss)
    tp1 = _safe_number(signal.tp1)
    tp2 = _safe_number(signal.tp2)
    side = str(signal.side).upper()
    risk = abs(entry - stop)
    if entry <= 0 or risk <= 0 or side not in ("LONG", "SHORT"):
        raise ValueError("Invalid Bitget orderflow signal")
    if side == "LONG" and not (stop < entry < tp1 <= tp2):
        raise ValueError("Invalid LONG levels")
    if side == "SHORT" and not (stop > entry > tp1 >= tp2):
        raise ValueError("Invalid SHORT levels")
    strong = getattr(signal, "market_condition", "CAUTIOUS") == "STRONG"
    # A 4H strategy can legitimately expose ONE 3R target as both tp1 and tp2.
    # Do NOT invent a second target or moonshot allocation in that case.
    split_targets = abs(tp2 - tp1) > entry * 1e-9
    moonshot = bool(strong and split_targets)
    width = min(entry * 0.001, risk * 0.15)
    return {
        "condition": "STRONG" if strong else "CAUTIOUS",
        "entry": entry, "entry_low": entry - width, "entry_high": entry + width,
        "stop_loss": stop, "tp1": tp1, "tp2": tp2 if moonshot else None,
        "moonshot": moonshot, "risk_distance": risk,
        "side": side, "tp1_fraction": 0.5 if moonshot else 1.0,
        "tp2_fraction": 0.3 if moonshot else 0.0,
        "moonshot_fraction": 0.2 if moonshot else 0.0,
        "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        "paper_only": True,
    }


def english_alert(signal, fmt):
    """Bold, emoji-led Telegram alert in plain trading language."""
    plan = trade_plan(signal)
    f = lambda v: escape(fmt(v))
    side = plan["side"]
    coin = str(signal.symbol).split("/")[0].split(":")[0]
    is_buy = side == "LONG"
    direction = "BUY / LONG" if is_buy else "SELL / SHORT"
    direction_icon = "🟢" if is_buy else "🔴"
    if plan["condition"] == "STRONG":
        market_line = (
            "📈 <b>Market: Strong upward move supports this BUY</b>"
            if is_buy else
            "📉 <b>Market: Strong downward move supports this SELL</b>"
        )
    else:
        market_line = "🟡 <b>Market: Not strong enough for extended targets</b>"

    lines = [
        "🚨 <b>BITGET FUTURES SIGNAL</b>",
        f"<b>{escape(coin)}</b>  ·  <b>{direction_icon} {direction}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"🧠 <b>Strategy:</b> {escape(str(signal.strategy_name))}",
        market_line,
        f"🎯 <b>Entry Price Range:</b> <code>{f(plan['entry_low'])} – {f(plan['entry_high'])}</code> USDT",
        f"✅ <b>TP1:</b> <code>{f(plan['tp1'])}</code> USDT",
    ]
    if plan["moonshot"]:
        lines.extend([
            f"🏆 <b>TP2:</b> <code>{f(plan['tp2'])}</code> USDT",
            "🚀 <b>Moonshot:</b> Trail the final 20% if price keeps moving beyond TP2.",
        ])
    else:
        tp2_reason = (
            "not used by this strategy"
            if plan["condition"] == "STRONG" else
            "off because the market is not strong enough"
        )
        lines.extend([
            f"🏆 <b>TP2: OFF</b> — {tp2_reason}.",
            "🚀 <b>Moonshot: OFF</b> — planned exit is at TP1.",
        ])
    lines.extend([
        f"🛑 <b>Stop Loss:</b> <code>{f(plan['stop_loss'])}</code> USDT",
        "━━━━━━━━━━━━━━━━━━",
        "📒 <i>Every delivered signal is recorded and tracked against closed Bitget candles. "
        "Results are paper estimates, not actual fills or account P&amp;L. No auto orders.</i>",
    ])
    return "\n".join(lines)


class BitgetPaperJournal:
    """SQLite event journal for delivered Bitget perpetual orderflow signals."""

    def __init__(self, db_path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=15000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS paper_signals (
                signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                engine_id TEXT NOT NULL, side TEXT NOT NULL,
                signal_ms INTEGER NOT NULL, created_utc TEXT NOT NULL,
                data TEXT NOT NULL, status TEXT NOT NULL,
                last_bar_ms INTEGER NOT NULL, filled_fraction REAL NOT NULL,
                gross_pct REAL NOT NULL, tp1_hit INTEGER NOT NULL,
                tp2_hit INTEGER NOT NULL, best_price REAL NOT NULL,
                trail_stop REAL, net_pct REAL, closed_utc TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT NOT NULL,
                bar_open_ms INTEGER, event_type TEXT NOT NULL,
                fill_price REAL, ohlcv TEXT, recorded_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_open
                ON paper_signals(status, symbol);
        """)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def record_delivered(self, signal):
        """Call ONLY after Telegram confirms delivery; never claim an exchange fill."""
        plan = trade_plan(signal)
        stamp = int(signal.timestamp)
        stable_key = getattr(signal, "signal_key", None) or stamp
        sid = hashlib.sha256(
            f"bitget|{signal.symbol}|{signal.engine_id}|{signal.side}|{stable_key}".encode()
        ).hexdigest()[:32]
        snapshots = list(getattr(signal, "closed_1h", ()))
        record = {
            "symbol": signal.symbol, "engine_id": signal.engine_id,
            "strategy_name": signal.strategy_name, "side": signal.side,
            "signal_ms": stamp, "reason": signal.reason, "plan": plan,
            "pre_signal_closed_1h": snapshots,
            "candle_context": "Closed 1h bars available at signal time; "
                              "never infer unseen trade ticks or actual fills.",
        }
        next_hour = (stamp // HOUR_MS + 1) * HOUR_MS
        before = self.conn.total_changes
        with self.conn:
            self.conn.execute("""
                INSERT OR IGNORE INTO paper_signals
                (signal_id,symbol,engine_id,side,signal_ms,created_utc,data,status,
                 last_bar_ms,filled_fraction,gross_pct,tp1_hit,tp2_hit,best_price)
                VALUES (?,?,?,?,?,?,?,'OPEN_PAPER',?,0,0,0,0,?)
            """, (sid, signal.symbol, signal.engine_id, signal.side, stamp, _utc(),
                  json.dumps(record, allow_nan=False), next_hour - HOUR_MS, plan["entry"]))
            if self.conn.total_changes > before:
                self.conn.execute("""
                    INSERT INTO paper_events
                    (signal_id,bar_open_ms,event_type,fill_price,ohlcv,recorded_utc)
                    VALUES (?,?,?,?,?,?)
                """, (sid, None, "TELEGRAM_DELIVERED_PAPER_ENTRY",
                      plan["entry"], None, _utc()))
        return sid

    def open_symbols(self):
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT symbol FROM paper_signals WHERE status='OPEN_PAPER'"
        ).fetchall()]

    def advance(self, symbol, frame):
        """Evaluate FULL 1h bars after signal hour, with no gap interpolation."""
        if frame is None or frame.empty:
            return 0
        rows = sorted(
            (tuple(r) for r in frame[
                ["timestamp", "open", "high", "low", "close", "volume"]
            ].itertuples(index=False, name=None)),
            key=lambda x: int(x[0]),
        )
        if not rows:
            return 0
        updated = 0
        for original in self.conn.execute("""
            SELECT * FROM paper_signals WHERE symbol=? AND status='OPEN_PAPER'
        """, (symbol,)).fetchall():
            row = dict(original)
            plan = json.loads(row["data"])["plan"]
            side = row["side"]
            entry = plan["entry"]
            direction = 1 if side == "LONG" else -1
            for bar in rows:
                ts, o, h, l, c, v = bar
                ts = int(ts)
                if ts <= row["last_bar_ms"]:
                    continue
                if ts != row["last_bar_ms"] + HOUR_MS:
                    # Past signal-hour partial candles are not valid simulations.
                    if ts < row["last_bar_ms"] + HOUR_MS:
                        continue
                    row["status"] = "UNVERIFIABLE_DATA_GAP"
                    self._event(row["signal_id"], ts, "DATA_GAP_NO_PNL", None, bar)
                    break
                o, h, l, c, v = map(_safe_number, (o, h, l, c, v))
                if not (0 < l <= min(o, c) <= max(o, c) <= h and v >= 0):
                    row["status"] = "UNVERIFIABLE_DATA_GAP"
                    self._event(row["signal_id"], ts, "INVALID_OHLCV_NO_PNL", None, bar)
                    break
                row["last_bar_ms"] = ts
                remaining = 1.0 - row["filled_fraction"]
                if ts >= ((row["signal_ms"] // HOUR_MS + 1) + MAX_HOLD_HOURS) * HOUR_MS:
                    row["gross_pct"] += remaining * direction * (o - entry) / entry * 100
                    row["filled_fraction"] = 1.0
                    self._event(row["signal_id"], ts, "PAPER_TIME_EXIT", o, bar)
                    row["status"] = "CLOSED"
                    break
                stop = plan["stop_loss"]
                if row["trail_stop"] is not None:
                    stop = max(stop, row["trail_stop"]) if direction == 1 else min(stop, row["trail_stop"])
                stop_hit = l <= stop if direction == 1 else h >= stop
                if stop_hit:
                    # Stop-first whenever both stop and target fit the same OHLC bar.
                    row["gross_pct"] += remaining * direction * (stop - entry) / entry * 100
                    row["filled_fraction"] = 1.0
                    self._event(row["signal_id"], ts, "PAPER_STOP_OR_TRAIL", stop, bar)
                    row["status"] = "CLOSED"
                    break
                def hit(target):
                    return h >= target if direction == 1 else l <= target
                if not row["tp1_hit"] and hit(plan["tp1"]):
                    frac = plan["tp1_fraction"]
                    row["gross_pct"] += frac * direction * (plan["tp1"] - entry) / entry * 100
                    row["filled_fraction"] += frac
                    row["tp1_hit"] = 1
                    self._event(row["signal_id"], ts, "PAPER_TP1", plan["tp1"], bar)
                    if not plan["moonshot"]:
                        row["status"] = "CLOSED"
                        break
                if plan["moonshot"] and row["tp1_hit"] and not row["tp2_hit"] and hit(plan["tp2"]):
                    frac = plan["tp2_fraction"]
                    row["gross_pct"] += frac * direction * (plan["tp2"] - entry) / entry * 100
                    row["filled_fraction"] += frac
                    row["tp2_hit"] = 1
                    row["trail_stop"] = plan["tp1"]
                    row["best_price"] = plan["tp2"]
                    self._event(row["signal_id"], ts, "PAPER_TP2", plan["tp2"], bar)
                elif row["tp2_hit"]:
                    # Updated trail applies to NEXT bar, avoiding intrabar ordering guesses.
                    extreme = h if direction == 1 else l
                    row["best_price"] = (max(row["best_price"], extreme)
                                          if direction == 1 else min(row["best_price"], extreme))
                    proposed = row["best_price"] - direction * plan["risk_distance"]
                    row["trail_stop"] = (max(row["trail_stop"], proposed)
                                         if direction == 1 else min(row["trail_stop"], proposed))
            if row["status"] == "CLOSED":
                row["net_pct"] = row["gross_pct"] - ROUND_TRIP_COST_PCT
                row["closed_utc"] = _utc()
                event = "PAPER_WIN" if row["net_pct"] > 0 else "PAPER_LOSS"
                self._event(row["signal_id"], row["last_bar_ms"], event, None, None)
            with self.conn:
                self.conn.execute("""
                    UPDATE paper_signals SET status=?, last_bar_ms=?,
                    filled_fraction=?, gross_pct=?, tp1_hit=?, tp2_hit=?,
                    best_price=?, trail_stop=?, net_pct=?, closed_utc=?
                    WHERE signal_id=?
                """, (row["status"], row["last_bar_ms"], row["filled_fraction"],
                      row["gross_pct"], row["tp1_hit"], row["tp2_hit"],
                      row["best_price"], row["trail_stop"], row["net_pct"],
                      row["closed_utc"], row["signal_id"]))
            updated += 1
        return updated

    def _event(self, sid, ts, kind, fill, bar):
        with self.conn:
            self.conn.execute("""
                INSERT INTO paper_events
                (signal_id,bar_open_ms,event_type,fill_price,ohlcv,recorded_utc)
                VALUES (?,?,?,?,?,?)
            """, (sid, ts, kind, fill, json.dumps(list(bar)) if bar else None, _utc()))

    def outcome_counts(self):
        return dict(self.conn.execute("""
            SELECT SUM(CASE WHEN status='CLOSED' AND net_pct>0 THEN 1 ELSE 0 END) wins,
                   SUM(CASE WHEN status='CLOSED' AND net_pct<=0 THEN 1 ELSE 0 END) losses,
                   SUM(CASE WHEN status='UNVERIFIABLE_DATA_GAP' THEN 1 ELSE 0 END) gaps,
                   SUM(CASE WHEN status='OPEN_PAPER' THEN 1 ELSE 0 END) open
              FROM paper_signals
        """).fetchone())


    def performance_by_strategy(self):
        """Closed-candle paper scorecard; open/gap records are not wins or losses."""
        rows = self.conn.execute("""
            SELECT engine_id,
                   COUNT(*) AS signals,
                   SUM(CASE WHEN status='CLOSED' AND net_pct>0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN status='CLOSED' AND net_pct<=0 THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN status='OPEN_PAPER' THEN 1 ELSE 0 END) AS open,
                   SUM(CASE WHEN status='UNVERIFIABLE_DATA_GAP' THEN 1 ELSE 0 END) AS gaps,
                   AVG(CASE WHEN status='CLOSED' THEN net_pct END) AS avg_net_pct
              FROM paper_signals
             GROUP BY engine_id
             ORDER BY engine_id
        """).fetchall()
        return [dict(row) for row in rows]

    def research_breakdown(self):
        """Group results by engine, coin, direction and recorded market condition."""
        groups = {}
        rows = self.conn.execute("""
            SELECT engine_id, symbol, side, status, net_pct, data
              FROM paper_signals
        """).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["data"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            plan = payload.get("plan") or {}
            condition = str(plan.get("condition") or "UNKNOWN")
            key = (row["engine_id"], row["symbol"], row["side"], condition)
            item = groups.setdefault(key, {
                "engine_id": key[0], "symbol": key[1], "side": key[2],
                "condition": condition, "signals": 0, "wins": 0, "losses": 0,
                "open": 0, "gaps": 0, "closed_returns": [],
            })
            item["signals"] += 1
            status = row["status"]
            if status == "CLOSED":
                if row["net_pct"] is not None and float(row["net_pct"]) > 0:
                    item["wins"] += 1
                else:
                    item["losses"] += 1
                if row["net_pct"] is not None:
                    item["closed_returns"].append(float(row["net_pct"]))
            elif status == "OPEN_PAPER":
                item["open"] += 1
            elif status == "UNVERIFIABLE_DATA_GAP":
                item["gaps"] += 1
        result = []
        for item in groups.values():
            returns = item.pop("closed_returns")
            item["closed"] = item["wins"] + item["losses"]
            item["avg_net_pct"] = (
                sum(returns) / len(returns) if returns else None
            )
            result.append(item)
        return sorted(
            result,
            key=lambda x: (x["closed"], x["signals"], x["engine_id"], x["symbol"]),
            reverse=True,
        )
