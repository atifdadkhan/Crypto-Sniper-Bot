from html import escape


def price(value):
    value = float(value)
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.5f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def signal_text(signal, risk_percent=None, max_notional=None):
    """Identify the exact signal engine; research 4H has a SINGLE 3R target."""
    direction = "🟢 LONG" if signal.side == "LONG" else "🔴 SHORT"
    if getattr(signal, "strategy_id", "CORE_15M") == "RS_4H":
        return (
            f"<b>A&amp;T TRENDHIVE  |  {direction}</b>\n"
            f"<b>{escape(signal.symbol)}</b>\n"
            f"🧠 Strategy: <b>4H Relative Strength Breakout</b>\n"
            f"🧪 Experimental live-monitoring signal\n"
            f"🎯 Entry: <code>{price(signal.entry)}</code>\n"
            f"🛑 Stop Loss (2 ATR): <code>{price(signal.stop_loss)}</code>\n"
            f"🏆 Target (3R): <code>{price(signal.tp2)}</code>\n"
            f"⏳ Review time exit after 5 days; no automatic exit tracking.\n"
            f"\n💡 Coin outperformed/underperformed BTC and ETH over 7 days; "
            f"confirmed 4H breakout.\n"
            f"⚠️ Historical research only; profitability is NOT guaranteed.\n"
            f"⚠️ Skip if live price has moved away from the stated entry.\n"
            f"<i>Signals only — all trades and exits are manual.</i>"
        )

    strength = "Strong" if signal.score >= 9 else "Good"
    if signal.setup == "Pullback" and signal.side == "LONG":
        reason = "Price pulled back and buyers stepped in."
    elif signal.setup == "Pullback":
        reason = "Price pulled back and sellers stepped in."
    else:
        reason = "Price retested a key level and confirmed the move."
    strategy_name = ("15m Trend Pullback" if signal.setup == "Pullback"
                     else "15m Breakout Retest")
    macro_warning = ("⚠️ Counter-macro setup: coin is stronger than BTC/ETH; extra risk.\n"
                     if getattr(signal, "counter_macro", False) else "")
    return (
        f"<b>A&amp;T TRENDHIVE  |  {direction}</b>\n"
        f"<b>{escape(signal.symbol)}</b>  ·  {strength} setup ({signal.score}/10)\n"
        f"🧠 Strategy: <b>{strategy_name}</b>\n"
        f"🕓 Timeframe: <b>15m entry / 1H + 4H trend</b>\n"
        f"🎯 Entry: <code>{price(signal.entry)}</code>\n"
        f"🛑 Stop Loss: <code>{price(signal.stop_loss)}</code>\n"
        f"✅ TP1: <code>{price(signal.tp1)}</code>\n"
        f"🏆 TP2: <code>{price(signal.tp2)}</code>\n"
        f"💡 {reason}\n"
        f"{macro_warning}"
        f"⚠️ If price has moved away from Entry, skip it.\n"
        f"<i>Signal only — you place every trade manually.</i>"
    )


def help_text():
    return (
        "🤖 <b>A&amp;T TrendHive Crypto Sniper</b>\n\n"
        "/scan — scan liquid markets now\n"
        "/status — bot and scanner status\n"
        "/settings — current scanner settings\n"
        "/history — delivered signals\n"
        "/help — commands\n\n"
        "Dual engines: 15m Pullback/Retest + experimental 4H Relative Strength.\n"
        "Markets: Bitget USDT-margined perpetual swaps. LONG/SHORT alerts only. "
        "No automatic trading or order placement."
    )
