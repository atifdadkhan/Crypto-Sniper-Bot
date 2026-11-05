"""Read-only single-coin reporting for the existing live scanner engines.

No order entry, no own strategy decisions and no alert queue writes.
Only shows validated live outputs, never forecasts invented trade levels.
"""
from html import escape
import re
import time

from formatting import price


def normalize_coin(value):
    """Accept BTC, BTCUSDT, BTC/USDT or BTC/USDT:USDT (USDT linear swap)."""
    if not isinstance(value, str):
        return None
    token = value.strip().upper()
    for suffix in ("/USDT:USDT", "/USDT", "USDT"):
        if token.endswith(suffix):
            token = token[:-len(suffix)]
            break
    if not re.fullmatch(r"[A-Z0-9]{2,20}", token):
        return None
    return f"{token}/USDT:USDT"


def signal_is_current(signal, core_ttl_seconds, now=None):
    """Mirror the live outbox closed-candle/TTL guard for manual display."""
    now = time.time() if now is None else float(now)
    try:
        close_ms = int(getattr(signal, "candle_close_ms", 0) or 0)
        ttl = int(getattr(signal, "ttl_seconds", core_ttl_seconds))
    except (TypeError, ValueError, OverflowError):
        return False
    if not close_ms or not 120 <= ttl <= 14400:
        return False
    close_at = close_ms / 1000.0
    return close_at <= now + 60 and now - close_at < ttl


def _signal_section(signal, core=False):
    engine_id = getattr(signal, "strategy_id", "CORE_15M")
    if core:
        engine = ("15m Trend Pullback" if getattr(signal, "setup", "") == "Pullback"
                  else "15m Breakout Retest")
    else:
        engine = ("4H Relative Strength Breakout" if engine_id == "RS_4H"
                  else "4H Slow Momentum Breakout")
    direction = "🟢 LONG" if signal.side == "LONG" else "🔴 SHORT"
    lines = [f"<b>{escape(engine)}: {direction}</b>"]
    if core:
        lines.append(f"Setup quality: {int(signal.score)}/10 (not a win probability)")
    else:
        lines.append("Experimental 4H model — unscored")
    lines.extend((
        f"Entry (live quote): <code>{price(signal.entry)}</code>",
        f"Stop Loss: <code>{price(signal.stop_loss)}</code>",
    ))
    if core:
        lines.append(f"TP1: <code>{price(signal.tp1)}</code>")
        lines.append(f"TP2: <code>{price(signal.tp2)}</code>")
    else:
        lines.append(f"Target (3R): <code>{price(signal.tp2)}</code>")
    if getattr(signal, "counter_macro", False):
        lines.append("⚠️ Counter-macro setup — extra risk")
    return "\n".join(lines)


def format_analysis(symbol, btc, eth, signals, failures, four_hour_eligible,
                    core_ttl_seconds=900, four_hour_enabled=True, now=None):
    """Separate results, including failed data vs. truly absent setups."""
    symbol = escape(symbol)
    engines = {getattr(signal, "strategy_id", "CORE_15M"): signal
               for signal in signals}
    core = engines.get("CORE_15M")
    four_hour = engines.get("RS_4H") or engines.get("SLOW_4H")
    now = time.time() if now is None else float(now)

    def state(signal, failure=None, core_model=False):
        if failure:
            return f"⚠️ DATA UNAVAILABLE ({escape(str(failure))}) — no valid analysis"
        if signal is None:
            return "⏸ WAIT — no qualifying setup"
        if not signal_is_current(signal, core_ttl_seconds, now=now):
            return "⏸ WAIT — setup expired; entry not actionable"
        return _signal_section(signal, core=core_model)

    lines = [
        "🔎 <b>A&amp;T TrendHive | Coin Analysis</b>",
        f"<b>{symbol}</b> · Bitget USDT perpetual",
        f"BTC 4H: {escape(str(btc.get('trend', 'UNKNOWN')))} | "
        f"ETH 4H: {escape(str(eth.get('trend', 'UNKNOWN')))}",
        "",
        "<b>15m Pullback / Retest</b>",
        state(core, failures.get("CORE_15M"), core_model=True),
        "",
        "<b>4H Relative Strength / Slow Momentum</b>",
    ]
    if not four_hour_enabled:
        lines.append("⏸ NOT EVALUATED — 4H engine disabled")
    elif not four_hour_eligible:
        lines.append("⏸ NOT EVALUATED — outside live top-20 liquid 4H watch")
    else:
        lines.append(state(four_hour, failures.get("RS_4H")))
    lines.extend((
        "",
        "Manual snapshot only; does not queue signals or change cooldowns.",
        "Signals can expire and prices can move. No guaranteed profit or automatic trading.",
    ))
    return "\n".join(lines)
