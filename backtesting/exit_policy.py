"""Research-only exit simulation. Never submits or modifies exchange orders.

Uses next-bar-open entry, stop-first OHLC ambiguity, unfavorable stop gaps,
partial profit taking, cost in R, and optional (assumed, not historical) funding.
"""
from dataclasses import dataclass
import math
import pandas as pd


@dataclass(frozen=True)
class ExitPolicy:
    name: str
    tp1_r: float = 1.5
    tp2_r: float = 2.5
    max_hold_bars: int = 96
    breakeven_after_tp1: bool = False

    def __post_init__(self):
        if not self.name or not isinstance(self.max_hold_bars, int) or self.max_hold_bars < 1:
            raise ValueError("Exit policy needs a name and positive integer holding bars")
        if not (math.isfinite(self.tp1_r) and math.isfinite(self.tp2_r)
                and 0 < self.tp1_r < self.tp2_r):
            raise ValueError("Exit targets must be finite, positive, and increasing")


# Predeclared hypotheses, not optimized or approved trading configurations.
POLICIES = (
    ExitPolicy("BASELINE"),
    ExitPolicy("TP1_1R_TP2_2R", 1.0, 2.0),
    ExitPolicy("TP1_1R_TP2_2_5R", 1.0, 2.5),
    ExitPolicy("TP1_1_5R_TP2_3R", 1.5, 3.0),
    ExitPolicy("TP1_1_5R_TP2_4R", 1.5, 4.0),
    ExitPolicy("TIME_EXIT_12H", 1.5, 2.5, 48),
    ExitPolicy("TIME_EXIT_48H", 1.5, 2.5, 192),
    ExitPolicy("BREAKEVEN_AFTER_TP1", 1.5, 2.5, 96, True),
)


def simulate_exit(sig, bars, entry_i, policy=POLICIES[0],
                  round_trip_cost_pct=0.12, funding_bps_per_8h=0.0):
    """Simulate a historical manual trade; return a trade dict or None.

    bars are 15-minute OHLCV with timestamp at candle OPEN. Caller MUST pass
    the next bar after the signal's completed candle as entry_i. Unknown
    intrabar ordering is resolved conservatively, stop first.
    """
    if not (math.isfinite(round_trip_cost_pct) and round_trip_cost_pct >= 0
            and math.isfinite(funding_bps_per_8h)):
        raise ValueError("Cost and funding assumptions must be finite; cost cannot be negative")
    if sig.side not in ("LONG", "SHORT"):
        raise ValueError("Invalid signal side")
    if entry_i < 0 or entry_i >= len(bars):
        return None
    entry_row = bars.iloc[entry_i]
    entry, stop = float(entry_row.open), float(sig.stop_loss)
    if not all(math.isfinite(x) and x > 0 for x in (entry, stop)):
        return None
    is_long = sig.side == "LONG"
    risk = entry - stop if is_long else stop - entry
    if risk <= 0 or risk / entry > 0.03:
        return None
    tp1 = entry + (policy.tp1_r * risk if is_long else -policy.tp1_r * risk)
    tp2 = entry + (policy.tp2_r * risk if is_long else -policy.tp2_r * risk)
    if tp1 <= 0 or tp2 <= 0:
        return None

    remaining, gross_r, reached_tp1 = 1.0, 0.0, False
    active_stop = stop
    last_i = min(entry_i + policy.max_hold_bars - 1, len(bars) - 1)
    exit_i, exit_reason = last_i, "TIME_EXIT"
    exit_price = float(bars.iloc[last_i].close)
    for j in range(entry_i, last_i + 1):
        row = bars.iloc[j]
        o, h, l, c = (float(row.open), float(row.high), float(row.low), float(row.close))
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            return None
        if h < max(o, c, l) or l > min(o, c, h):
            return None
        stop_hit = l <= active_stop if is_long else h >= active_stop
        one_hit = h >= tp1 if is_long else l <= tp1
        two_hit = h >= tp2 if is_long else l <= tp2
        if stop_hit:
            fill = min(o, active_stop) if is_long else max(o, active_stop)
            gross_r += remaining * ((fill - entry) if is_long else (entry - fill)) / risk
            remaining, exit_i, exit_price = 0.0, j, fill
            exit_reason = "STOP_AFTER_TP1" if reached_tp1 else "STOP_LOSS"
            break
        if not reached_tp1 and one_hit:
            gross_r += 0.5 * policy.tp1_r
            remaining -= 0.5
            reached_tp1 = True
            # The breakeven stop affects only following bars, not the
            # unknowable sequence inside the current OHLC bar.
            if policy.breakeven_after_tp1:
                active_stop = entry
        if two_hit and remaining > 0:
            gross_r += remaining * policy.tp2_r
            remaining, exit_i, exit_price, exit_reason = 0.0, j, tp2, "TP2"
            break
        if j == last_i and remaining > 0:
            gross_r += remaining * ((c - entry) if is_long else (entry - c)) / risk
            remaining, exit_i, exit_price = 0.0, j, c

    risk_pct = 100 * risk / entry
    cost_r = round_trip_cost_pct / risk_pct
    try:
        held_hours = max(0.0, (pd.Timestamp(bars.iloc[exit_i].timestamp)
                         - pd.Timestamp(entry_row.timestamp)).total_seconds() / 3600.0)
    except (AttributeError, ValueError):
        held_hours = 0.0
    # Funding bps is only a scenario assumption. Actual funding is timestamped,
    # per-market, settlement-dependent, and can change sign.
    funding_r = ((funding_bps_per_8h / 100.0) / risk_pct) * (held_hours / 8.0)
    return {
        "entry_time": str(entry_row.timestamp),
        "exit_time": str(bars.iloc[exit_i].timestamp),
        "entry": entry, "exit": exit_price, "stop_loss": stop,
        "tp1": tp1, "tp2": tp2, "side": sig.side,
        "exit_reason": exit_reason, "tp1_hit": reached_tp1,
        "gross_r": gross_r, "cost_r": cost_r, "funding_r_assumed": funding_r,
        "net_r": gross_r - cost_r - funding_r,
        "risk_pct": risk_pct, "held_hours": held_hours,
        "data_censored": exit_i == len(bars) - 1 and exit_reason == "TIME_EXIT"
                      and entry_i + policy.max_hold_bars > len(bars),
    }
