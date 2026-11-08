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
    """Mobile-first, at most ten lines. Keep exactly the strategy's levels."""
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
        strategy = {
            "Pullback": "15m Trend Pullback",
            "Breakout retest": "15m Breakout Retest",
            "Breakout": "15m Confirmed Breakout",
            "Liquidity Sweep": "15m Liquidity Sweep",
            "Trend Continuation": "15m Trend Continuation",
        }.get(signal.setup, "15m Confirmed Setup")
        strategy = escape(str(getattr(signal, "strategy_name", "") or strategy))
        quality = (f"⭐ {'Strong' if signal.score >= 9 else 'Good'} · "
                   f"{signal.score}/10 (setup quality, not win probability)")

    lines = [
        f"🚨 <b>NEW SIGNAL | {side}</b>",
        f"<b>{symbol}</b> · {escape(strategy)}",
        quality,
        f"🎯 Entry: <code>{price(signal.entry)}</code>",
        f"🛑 Stop Loss: <code>{price(signal.stop_loss)}</code>",
    ]
    if kind in ("RS_4H", "SLOW_4H"):
        lines.append(f"🏆 Target (3R): <code>{price(signal.tp2)}</code>")
        lines.append("⏳ Review after 5 days; no automatic exit." if kind == "RS_4H"
                     else "⏳ Review after 10 days; no automatic exit.")
    else:
        lines.extend([
            f"✅ TP1: <code>{price(signal.tp1)}</code>",
            f"🏆 TP2: <code>{price(signal.tp2)}</code>",
        ])
    if kind == "CORE_15M":
        for label, raw in (
            ("📈 Trends", getattr(signal, "htf_trends", "")),
            ("🌐 Macro", getattr(signal, "macro_trends", "")),
        ):
            if raw:
                safe = escape(str(raw))
                safe = (safe.replace("BULLISH", "🟢")
                            .replace("BEARISH", "🔴")
                            .replace("NEUTRAL", "⚪")
                            .replace("UNKNOWN", "⚪"))
                lines.append(f"{label}: {safe}")
    if getattr(signal, "counter_macro", False):
        lines.append("⚠️ Counter-macro: higher risk.")
    lines.append("⚠️ Skip if current price moved away from Entry.")
    lines.append("<i>Manual signal only · Profit not guaranteed.</i>")
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
