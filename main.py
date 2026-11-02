import asyncio
import logging
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, jsonify
from waitress import serve
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from config import load_settings
from exchange import MarketData, macro_trend
from strategy import evaluate
from formatting import signal_text, help_text
from utils import load_json, atomic_json, cooldown_active

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("trendhive")
settings = load_settings()
data_dir = Path(settings.data_dir)
data_dir.mkdir(parents=True, exist_ok=True)
STATE_FILE, HISTORY_FILE = data_dir / "state.json", data_dir / "history.json"
state = load_json(STATE_FILE, {"cooldowns": {}, "last_scan": None, "last_error": None})
if not isinstance(state.get("cooldowns"), dict):
    state["cooldowns"] = {}
history = load_json(HISTORY_FILE, [])
if not isinstance(history, list):
    history = []
scan_lock = asyncio.Lock()
client = MarketData(settings.exchange_timeout_ms, settings.request_rate_limit)
last_scan_result = {"symbols": 0, "signals": 0, "skipped": 0}

# Compact Telegram inline menu. Callback data is intentionally short and fixed.
MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔍 SCAN NOW", callback_data="menu:scan"),
     InlineKeyboardButton("📊 MARKET STATUS", callback_data="menu:status")],
    [InlineKeyboardButton("📚 SIGNAL HISTORY", callback_data="menu:history"),
     InlineKeyboardButton("⚙️ SETTINGS", callback_data="menu:settings")],
    [InlineKeyboardButton("❔ HELP & COMMANDS", callback_data="menu:help")],
])

health_app = Flask(__name__)


@health_app.get("/")
@health_app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "A&T TrendHive Crypto Sniper",
                    "mode": "signals_only", "exchange": "Bitget USDT perpetual public data",
                    "last_scan": state.get("last_scan"), "scan": last_scan_result}), 200


def run_health_server():
    serve(health_app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


def authorized(update):
    return bool(update.effective_user and update.effective_user.id == settings.admin_id)


async def deny(update):
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Admin access only.")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    await update.effective_message.reply_text(help_text(), parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start_cmd(update, context)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    msg = (f"⚙️ <b>Settings</b>\nTop pairs: {settings.top_n}\n"
           f"Minimum quote volume: {settings.min_quote_volume:,.0f} USDT\n"
           f"Minimum signal score: {settings.min_score}/10\n"
           f"Cooldown: {settings.cooldown_minutes} minutes\n"
           f"Risk reference: {settings.risk_percent:.2f}% of notional cap\n"
           f"Notional cap reference: {settings.max_position_notional:.2f} USDT\n"
           f"Scan interval: {settings.scan_seconds}s\n"
           f"Concurrent symbols: {settings.concurrency}\n\n"
           "All sizing values are informational. No orders are sent.")
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    msg = (f"🟢 Bot process: running\n📡 Exchange: Bitget public market data\n"
           f"🕒 Last scan: {state.get('last_scan') or 'Not yet'}\n"
           f"📊 Pairs checked: {last_scan_result['symbols']}\n"
           f"🚨 New signals: {last_scan_result['signals']}\n"
           f"⚠️ Pairs skipped: {last_scan_result['skipped']}\n"
           f"❗ Last error: {state.get('last_error') or 'None'}\n"
           "🔒 Trading mode: SIGNALS ONLY")
    await update.effective_message.reply_text(msg, reply_markup=MAIN_MENU)


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    if not history:
        return await update.effective_message.reply_text("No signals recorded yet.", reply_markup=MAIN_MENU)
    lines = ["📚 <b>Recent signals</b>"]
    for item in reversed(history[-10:]):
        lines.append(f"• {item.get('side', '?')} {item.get('symbol', '?')} — "
                     f"score {item.get('score', '?')}/10 — {str(item.get('timestamp', ''))[:16]}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the persistent inline main-menu actions."""
    query = update.callback_query
    if not authorized(update):
        await query.answer("Admin access only.", show_alert=True)
        return
    await query.answer()
    action = (query.data or "").partition(":")[2]

    if action == "help":
        await query.message.reply_text(help_text(), parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)
    elif action == "settings":
        msg = (f"⚙️ <b>Settings</b>\nTop pairs: {settings.top_n}\n"
               f"Minimum quote volume: {settings.min_quote_volume:,.0f} USDT\n"
               f"Minimum signal score: {settings.min_score}/10\n"
               f"Cooldown: {settings.cooldown_minutes} minutes\n"
               f"Risk reference: {settings.risk_percent:.2f}% of notional cap\n"
               f"Notional cap reference: {settings.max_position_notional:.2f} USDT\n"
               f"Scan interval: {settings.scan_seconds}s\n"
               f"Concurrent symbols: {settings.concurrency}\n\n"
               "All sizing values are informational. No orders are sent.")
        await query.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)
    elif action == "status":
        msg = (f"🟢 Bot process: running\n📡 Exchange: Bitget public market data\n"
               f"🕒 Last scan: {state.get('last_scan') or 'Not yet'}\n"
               f"📊 Pairs checked: {last_scan_result['symbols']}\n"
               f"🚨 New signals: {last_scan_result['signals']}\n"
               f"⚠️ Pairs skipped: {last_scan_result['skipped']}\n"
               f"❗ Last error: {state.get('last_error') or 'None'}\n"
               "🔒 Trading mode: SIGNALS ONLY")
        await query.message.reply_text(msg, reply_markup=MAIN_MENU)
    elif action == "history":
        if not history:
            await query.message.reply_text("No signals recorded yet.", reply_markup=MAIN_MENU)
        else:
            lines = ["📚 <b>Recent signals</b>"]
            for item in reversed(history[-10:]):
                lines.append(f"• {item.get('side', '?')} {item.get('symbol', '?')} — "
                             f"score {item.get('score', '?')}/10 — {str(item.get('timestamp', ''))[:16]}")
            await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                            reply_markup=MAIN_MENU)
    elif action == "scan":
        msg = await query.message.reply_text(
            "🔍 Scanning Bitget USDT perpetual markets. Please wait...")
        result = await run_scan(context.bot, update.effective_chat.id)
        await msg.edit_text(result, reply_markup=MAIN_MENU)
    else:
        await query.message.reply_text("Unknown menu action. Use /start.", reply_markup=MAIN_MENU)


async def _scan_one(symbol, semaphore, btc, eth):
    async with semaphore:
        candles = await client.multi_timeframe(symbol, asyncio.Semaphore(1))
        live_price = await client.live_price(symbol)
        return evaluate(symbol, candles, btc, eth, settings.min_score, current_price=live_price)


async def run_scan(bot=None, chat_id=None):
    global history, state, last_scan_result
    if scan_lock.locked():
        return "A scan is already running. Please wait."
    async with scan_lock:
        checked, skipped, generated, delivered = 0, 0, [], []
        try:
            if not client.markets:
                await client.initialize()
            symbols = await client.top_symbols(settings.top_n, settings.min_quote_volume)
            if not symbols:
                raise RuntimeError("No liquid active USDT perpetual markets met the volume filter")

            macro = await asyncio.gather(
                macro_trend(client.exchange, "BTC/USDT:USDT"),
                macro_trend(client.exchange, "ETH/USDT:USDT"),
                return_exceptions=True,
            )
            btc = macro[0] if isinstance(macro[0], str) else "UNKNOWN"
            eth = macro[1] if isinstance(macro[1], str) else "UNKNOWN"
            semaphore = asyncio.Semaphore(settings.concurrency)
            tasks = [_scan_one(symbol, semaphore, btc, eth) for symbol in symbols]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for symbol, result in zip(symbols, results):
                checked += 1
                if isinstance(result, BaseException):
                    skipped += 1
                    log.warning("Skipping %s: %s", symbol, result)
                    continue
                if result is None:
                    continue
                key = f"{symbol}|{result.side}"
                if cooldown_active(state["cooldowns"].get(key), settings.cooldown_minutes):
                    continue
                generated.append(result)

            # Deliver first. Only start cooldown after successful delivery (or record-only mode).
            for signal in generated:
                delivered_ok = True
                if bot is not None and chat_id is not None:
                    try:
                        await bot.send_message(chat_id=chat_id,
                            text=signal_text(signal, settings.risk_percent, settings.max_position_notional),
                            parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=MAIN_MENU)
                    except Exception:
                        delivered_ok = False
                        log.exception("Telegram delivery failed for %s", signal.symbol)
                history.append(signal.to_dict())
                if delivered_ok:
                    state["cooldowns"][f"{signal.symbol}|{signal.side}"] = time.time()
                    delivered.append(signal)

            history = history[-300:]
            state["last_scan"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            state["last_error"] = None
            atomic_json(STATE_FILE, state)
            atomic_json(HISTORY_FILE, history)
            last_scan_result = {"symbols": checked, "signals": len(delivered), "skipped": skipped}
            return (f"Scan complete: {checked} pairs checked, {len(delivered)} alerts delivered, "
                    f"{skipped} pairs skipped for data/errors. BTC: {btc}, ETH: {eth}. "
                    f"No signal means filters were not fully confirmed.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Scan failed")
            state["last_error"] = str(exc)[:300]
            atomic_json(STATE_FILE, state)
            last_scan_result = {"symbols": checked, "signals": 0, "skipped": skipped}
            return f"Scan failed: {str(exc)[:250]}"


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    msg = await update.effective_message.reply_text("🔍 Scanning Bitget USDT perpetual markets. Please wait...")
    result = await run_scan(context.bot, update.effective_chat.id)
    await msg.edit_text(result, reply_markup=MAIN_MENU)


async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE):
    result = await run_scan(context.bot, settings.admin_id)
    log.info("Scheduled scan: %s", result)


async def post_init(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Open the main menu"),
            BotCommand("scan", "Scan markets now"),
            BotCommand("status", "Bot and scanner status"),
            BotCommand("settings", "View scanner settings"),
            BotCommand("history", "Recent signals"),
            BotCommand("help", "Help and commands"),
        ])
    except Exception:
        log.exception("Could not register Telegram command menu")
    try:
        await client.initialize()
        log.info("Loaded %d Bitget markets", len(client.markets))
    except Exception:
        log.exception("Initial market load failed; scanner will retry on next scan")
    if application.job_queue:
        application.job_queue.run_repeating(scheduled_scan, interval=settings.scan_seconds,
                                            first=30, name="market_scan")
    else:
        log.error("JobQueue unavailable; scheduled scanning disabled. Install python-telegram-bot[job-queue].")


async def post_shutdown(application: Application):
    await client.close()


def main():
    threading.Thread(target=run_health_server, daemon=True, name="health-server").start()
    application = (Application.builder().token(settings.telegram_token)
                   .post_init(post_init).post_shutdown(post_shutdown).build())
    for command, handler in (("start", start_cmd), ("help", help_cmd), ("scan", scan_cmd),
                             ("status", status_cmd), ("settings", settings_cmd), ("history", history_cmd)):
        application.add_handler(CommandHandler(command, handler))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:(scan|status|history|settings|help)$"))
    log.info("Starting A&T TrendHive in SIGNALS-ONLY mode")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
