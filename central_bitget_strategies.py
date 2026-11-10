"""One Bitget USDT-perpetual signal contract for CVD/Wick and 15m/4h engines.

No Binance Spot data, private exchange endpoints, authentication or order calls.
This module routes presentation and paper ledger metadata; it does not relax
any strategy's existing entry confirmation or liquidity guardrails.
"""
from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone
from bitget_orderflow_journal import english_alert, trade_plan


BITGET_LIVE_ENGINES = (
    "CVD_DIVERGENCE",
    "WICK_HUNT",
    "CORE_15M",
    "RS_4H",
    "SLOW_4H",
)
SPOT_STRATEGY_COUNT = 12  # Separate Binance Spot research; NEVER Bitget Futures alerts.


def live_registry(orderflow_mode="both", candle_live=True, four_hour_live=True):
    """Actual runnable engines, not all code present in the repository."""
    return {
        "venue": "Bitget",
        "market_type": "USDT perpetual futures",
        "execution": "SIGNALS_ONLY",
        "live_engine_ids": [
            engine for engine in BITGET_LIVE_ENGINES
            if ((engine == "CVD_DIVERGENCE" and orderflow_mode in ("both", "cvd"))
                or (engine == "WICK_HUNT" and orderflow_mode in ("both", "wick"))
                or (engine == "CORE_15M" and candle_live)
                or (engine in ("RS_4H", "SLOW_4H") and candle_live and four_hour_live))
        ],
        "spot_research_only": SPOT_STRATEGY_COUNT,
        "shared_delivery": "telegram",
        "shared_paper_journal": "bitget_orderflow_paper.sqlite3",
    }


def closed_hour_context(frame, side):
    """Return only validated closed Bitget 1h bars and strict trend alignment."""
    if frame is None or len(frame) < 20:
        return "CAUTIOUS", ()
    required = ("timestamp", "open", "high", "low", "close", "volume")
    if any(col not in frame.columns for col in required):
        return "CAUTIOUS", ()
    try:
        snapshots = tuple(
            tuple(int(v) if i == 0 else float(v) for i, v in enumerate(row))
            for row in frame.loc[:, required].tail(40).itertuples(index=False, name=None)
        )
        if len(snapshots) < 20:
            return "CAUTIOUS", snapshots
        closes = tuple(x[4] for x in snapshots)
        average = sum(closes[-20:]) / 20
        momentum = closes[-1] - closes[-9]
        strong = (closes[-1] > average and momentum > 0) if side == "LONG" else (
            closes[-1] < average and momentum < 0
        )
        return ("STRONG" if strong else "CAUTIOUS"), snapshots
    except (TypeError, ValueError, OverflowError):
        return "CAUTIOUS", ()


def attach_hour_context(signal, frame):
    """Attach current closed Bitget 1h snapshot to already-validated strategy signal."""
    condition, snapshot = closed_hour_context(frame, signal.side)
    signal.market_condition = condition
    signal.closed_1h = snapshot
    return signal


def normalized_candle_signal(record, delivered_ms, source_key=None):
    """Convert a queued 15m/4h signal to the same paper/alert schema as orderflow.

    A candle signal's timestamp is the time of actual confirmed Telegram send,
    NOT an assumed fill at the close of its originating 15m or 4h bar.
    """
    strategy_id = str(record.get("strategy_id") or "CORE_15M")
    if strategy_id not in ("CORE_15M", "RS_4H", "SLOW_4H"):
        raise ValueError("Unknown Bitget candle strategy")
    reason = record.get("reasons") or []
    if isinstance(reason, (tuple, list)):
        reason = "; ".join(str(x) for x in reason[:3])
    strategy_name = str(record.get("strategy_name") or record.get("setup")
                        or strategy_id)
    return SimpleNamespace(
        symbol=str(record["symbol"]),
        engine_id=strategy_id,
        strategy_name=strategy_name,
        side=str(record["side"]),
        entry=float(record["entry"]),
        stop_loss=float(record["stop_loss"]),
        tp1=float(record["tp1"]),
        tp2=float(record["tp2"]),
        timestamp=int(delivered_ms),
        score=int(record.get("score") or 0),
        reason=str(reason or "Confirmed closed-candle Bitget setup"),
        market_condition=str(record.get("market_condition") or "CAUTIOUS"),
        closed_1h=tuple(tuple(row) for row in (record.get("closed_1h") or ())),
        signal_key=source_key,
    )


def unified_candle_alert(signal, fmt):
    """English alert for the original strategy levels. Does not generate trades."""
    if not hasattr(signal, "to_dict"):
        raise ValueError("Expected a validated Bitget candle engine signal")
    converted = normalized_candle_signal(signal.to_dict(), int(
        datetime.now(timezone.utc).timestamp() * 1000
    ))
    return english_alert(converted, fmt)


def paper_plan_valid(signal):
    """Only journal actionable finite levels; invalid levels cannot masquerade as fills."""
    return trade_plan(signal)
