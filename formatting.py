"""Compact Telegram signal and help text. Presentation only; no strategy logic."""
from html import escape


def price(value):
    value = float(value)
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.5f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def signal_text(signal, risk_percent=None, max_notional=None):
    """Accurate levels for each engine. 4H has ONE 3R target, not TP1/TP2."""
    kind = getattr(signal, "strategy_id", "CORE_15M")
    side = "🟢 LONG" if signal.side == "LONG" else "🔴 SHORT"
    symbol = escape(str(signal.symbol))
    if kind == "RS_4H":
        strategy = "4H Relative Strength Breakout"
        quality = "🧪 Experimental · Unscored"
    elif kind == "SLOW_4H":
        strategy = "4H Slow Momentum Breakout"
        quality = "🧪 Experimental · Unscored"
    else:
        strategy = ("15m Trend Pullback" if signal.setup == "Pullback"
                    else "15m Breakout Retest")
        strength = "Strong" if signal.score >= 9 else "Good"
        quality = f"⭐ {strength} setup · {signal.score}/10 (not win probability)"

    lines = [
        f"🎯 <b>A&amp;T TRENDHIVE | {side}</b>",
        f"<b>{symbol}</b>",
        "━━━━━━━━━━━━━━━━━",
        f"🧠 <b>{escape(strategy)}</b>",
        quality,
        "",
        "<b>TRADE LEVELS</b>",
        f"Entry: <code>{price(signal.entry)}</code>",
        f"Stop Loss: <code>{price(signal.stop_loss)}</code>",
    ]
    if kind in ("RS_4H", "SLOW_4H"):
        lines.append(f"Target (3R): <code>{price(signal.tp2)}</code>")
        if kind == "RS_4H":
            lines.append("⏳ Review after 5 days; no automatic exit.")
        else:
            lines.append("⏳ Review after 10 days; no automatic exit.")
    else:
        lines.extend([
            f"TP1: <code>{price(signal.tp1)}</code>",
            f"TP2: <code>{price(signal.tp2)}</code>",
        ])
    if getattr(signal, "counter_macro", False):
        lines.append("⚠️ Counter-macro setup: higher risk.")
    lines.extend([
        "",
        "⚠️ Skip if the current price has moved away from Entry.",
        "<i>Signals only · Trade manually · Profit not guaranteed.</i>",
    ])
    return "\n".join(lines)


def help_text():
    return (
        "🤖 <b>A&amp;T TRENDHIVE | CRYPTO SNIPER</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "🔎 <b>Analyze Coin</b> — select the button and send BTC, SOL, etc.\n"
        "🔍 <b>Scan Market</b> — scan eligible Bitget markets.\n"
        "📊 <b>Market Status</b> — latest scanner report.\n"
        "📚 <b>Signal History</b> — delivered alerts only.\n"
        "⚙️ <b>Settings</b> — current risk and scanner settings.\n\n"
        "<b>Quick commands</b>\n"
        "/analyze BTC · /scan · /status · /history · /settings · /help\n\n"
        "15m Pullback/Retest covers eligible markets; experimental 4H "
        "Relative Strength / Slow Momentum covers the liquid top-20 watch.\n"
        "<i>Bitget public USDT perpetual data · Signals only · No auto trading.</i>"
    )
