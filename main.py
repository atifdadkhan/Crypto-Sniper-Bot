"""A&T TrendHive Crypto Sniper — live market research and manual signals only.

No authenticated exchange endpoints, private keys or order-placement functions.
"""
import asyncio
from html import escape
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
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import load_settings
from exchange import MarketData, macro_snapshot
from strategy import evaluate
from relative_strength import evaluate_relative_strength, precheck_recent_breakout
from slow_momentum import evaluate_slow_momentum, precheck_slow_momentum
from formatting import signal_text, help_text
from single_coin import normalize_coin, format_analysis
from delivery import AlertOutbox
from scan_diagnostics import safe_failure_reason
from utils import load_json, atomic_json, cooldown_active

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# HTTPX INFO includes request URLs. Telegram API URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("trendhive")

settings = load_settings()
data_dir = Path(settings.data_dir)
data_dir.mkdir(parents=True, exist_ok=True)
STATE_FILE, HISTORY_FILE = data_dir / "state.json", data_dir / "history.json"
state = load_json(STATE_FILE, {})
if not isinstance(state.get("cooldowns"), dict):
    state["cooldowns"] = {}
state.setdefault("scan_cursor", 0)
state.setdefault("last_scan", None)
state.setdefault("last_error", None)
state.setdefault("last_scan_result", {})
history = load_json(HISTORY_FILE, [])
if not isinstance(history, list):
    history = []

outbox = AlertOutbox(data_dir / "telegram_outbox.json", settings.signal_ttl_seconds)
scan_lock = asyncio.Lock()
coin_analysis_lock = asyncio.Lock()
delivery_lock = asyncio.Lock()
client = MarketData(settings.exchange_timeout_ms, settings.request_rate_limit)
last_scan_result = {"symbols": 0, "signals": 0, "skipped": 0, "no_setup": 0,
                    "universe": 0, "scan_batch": 0, "delivery_pending": len(outbox.items),
                    "core15m_queued": 0, "rs4h_queued": 0, "rs4h_errors": 0,
                    "slow4h_queued": 0,
                    "four_hour_watch": 0, "four_hour_extra": 0}
if isinstance(state.get("last_scan_result"), dict):
    last_scan_result.update(state["last_scan_result"])
last_scan_result["delivery_pending"] = len(outbox.items)
last_delivery_error = None
manual_scan_task = None
manual_coin_task = None
market_refresh_task = None

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔎 Analyze Coin", callback_data="menu:analyze"),
     InlineKeyboardButton("🔍 Scan Market", callback_data="menu:scan")],
    [InlineKeyboardButton("📊 Market Status", callback_data="menu:status"),
     InlineKeyboardButton("📚 Signal History", callback_data="menu:history")],
    [InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
     InlineKeyboardButton("❔ Help", callback_data="menu:help")],
])
BACK_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:home")]
])
SETTINGS_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔄 Refresh Markets", callback_data="menu:refresh")],
    [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:home")]
])
health_app = Flask(__name__)


def _readiness():
    last = state.get("last_scan_epoch")
    try:
        seconds = int(time.time() - float(last)) if last is not None else None
    except (ValueError, TypeError):
        seconds = None
    is_fresh = seconds is not None and 0 <= seconds < max(settings.scan_seconds * 2, 1800)
    return {"scanner_fresh": is_fresh, "last_scan_age_seconds": seconds,
            "pending_alerts": len(outbox.items),
            "last_error": state.get("last_error"),
            "last_scan": state.get("last_scan"), "scan": last_scan_result}


@health_app.get("/")
@health_app.get("/health")
def health():
    # Liveness is not readiness. A 200 here never claims that a trade is profitable.
    return jsonify(status="ok", service="A&T TrendHive Crypto Sniper",
                   mode="signals_only", **_readiness()), 200


@health_app.get("/ready")
def ready():
    detail = _readiness()
    ready_now = bool(detail["scanner_fresh"] and detail["last_error"] is None)
    return jsonify(status="ready" if ready_now else "degraded", **detail), (200 if ready_now else 503)


def run_health_server():
    serve(health_app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


def authorized(update):
    return bool(update.effective_user and update.effective_user.id == settings.admin_id)


async def deny(update):
    if update.effective_message:
        await update.effective_message.reply_text("⛔ Admin access only.")


def _save_state():
    state["last_scan_result"] = last_scan_result
    atomic_json(STATE_FILE, state)


def _save_history():
    atomic_json(HISTORY_FILE, history[-300:])


def _scan_summary():
    result = last_scan_result
    return (
        "📊 <b>MARKET STATUS</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"🕒 Last scan: <code>{escape(str(state.get('last_scan') or 'Not yet'))}</code>\n"
        f"🔍 Checked: <b>{result['symbols']} / {result['universe']}</b> markets\n"
        f"15m rotating batch: {result['scan_batch']}\n"
        f"4H liquid watch: {result.get('four_hour_watch', 0)}\n"
        f"4H additional checks: {result.get('four_hour_extra', 0)}\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"📨 Delivered: {result['signals']} | Pending: {len(outbox.items)}\n"
        f"15m alerts queued: {result.get('core15m_queued', 0)}\n"
        f"4H RS / Slow queued: {result.get('rs4h_queued', 0)} / "
        f"{result.get('slow4h_queued', 0)}\n"
        f"⚠️ 15m data skips: {result['skipped']}\n"
        f"⚠️ 4H data skips: {result.get('rs4h_errors', 0)}\n"
        f"⏸ No setup: {result['no_setup']}\n"
        f"📊 Opportunity Report: {result['signals']} delivered | {result['no_setup']} without setup | {result['skipped']} data skips\n"
        f"Last error: {escape(str(state.get('last_error') or 'None'))}\n\n"
        "<i>Manual signals only. Zero alerts can be normal.</i>"
    )


def _settings_text():
    return (
        "⚙️ <b>SCANNER SETTINGS</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"Markets: Top {settings.top_n or 'all eligible'}\n"
        f"Min 24h volume: {settings.min_quote_volume:,.0f} USDT\n"
        f"Signal quality floor: {settings.min_score}/10\n"
        f"Scan interval: {settings.scan_seconds // 60} min\n"
        f"15m batch: {settings.scan_batch_size or 'All'}\n"
        f"4H research engine: {'ON' if settings.rs_engine_enabled else 'OFF'}\n"
        f"4H liquid watch: top {settings.four_hour_top_n}\n"
        f"Cooldown: {settings.cooldown_minutes} min\n"
        f"Risk reference: {settings.risk_percent:.2f}%\n"
        f"Position notional cap: {settings.max_position_notional:.2f} USDT\n\n"
        "<i>Read-only configuration · No automatic orders.</i>"
    )


def _history_text():
    if not history:
        return "📚 <b>SIGNAL HISTORY</b>\nNo delivered signals recorded yet."
    lines = ["📚 <b>RECENT DELIVERED SIGNALS</b>", "━━━━━━━━━━━━━━━━━"]
    for item in reversed(history[-8:]):
        symbol = escape(str(item.get("symbol", "?")))
        side = escape(str(item.get("side", "?")))
        strategy = item.get("strategy_name") or (
            "15m Trend Pullback" if item.get("setup") == "Pullback"
            else "15m Breakout Retest"
        )
        stamp = escape(str(item.get("timestamp", ""))[:16].replace("T", " "))
        lines.append(f"• <b>{symbol}</b> · {side}\n"
                     f"  {escape(str(strategy))} · {stamp}")
    lines.append("\n<i>Only confirmed Telegram deliveries, not trade fills.</i>")
    return "\n".join(lines)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    context.user_data.pop("awaiting_coin", None)
    await update.effective_message.reply_text(
        help_text(), parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await start_cmd(update, context)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    await update.effective_message.reply_text(
        _settings_text(), parse_mode=ParseMode.HTML, reply_markup=SETTINGS_MENU
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    await update.effective_message.reply_text(
        _scan_summary(), parse_mode=ParseMode.HTML, reply_markup=BACK_MENU
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    await update.effective_message.reply_text(
        _history_text(), parse_mode=ParseMode.HTML, reply_markup=BACK_MENU
    )


async def _panel(query, text, keyboard=BACK_MENU):
    """Edit a navigation panel in place, but never overwrite a trade alert."""
    current = getattr(query.message, "text", "") or ""
    # Delivered signal messages have level fields and must remain visible
    # in the chat history when the attached menu is pressed.
    is_trade_alert = "Entry:" in current and "Stop Loss" in current
    if is_trade_alert:
        return await query.message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )
    try:
        return await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=keyboard,
        )
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return None
        raise


async def _background_manual_scan(progress, bot, chat_id):
    try:
        report = await run_scan(bot, chat_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Manual scan failed: %s", type(exc).__name__)
        report = "⚠️ Market scan unavailable. Check scanner status."
    try:
        await progress.edit_text(report, reply_markup=MAIN_MENU)
    except Exception as exc:
        log.warning("Unable to update scan progress: %s", type(exc).__name__)


async def _background_market_refresh(progress):
    try:
        if scan_lock.locked() or coin_analysis_lock.locked():
            report = "⏳ Scanner busy. Refresh markets after the current scan."
        else:
            await asyncio.wait_for(client.initialize(), timeout=60)
            report = f"✅ Market metadata refreshed: {len(client.markets)} markets."
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Market refresh failed: %s", type(exc).__name__)
        report = "⚠️ Market refresh unavailable. Check Render logs."
    try:
        await progress.edit_text(report, reply_markup=MAIN_MENU)
    except Exception as exc:
        log.warning("Unable to update refresh progress: %s", type(exc).__name__)


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global manual_scan_task, market_refresh_task
    query = update.callback_query
    if not authorized(update):
        return await query.answer("Admin only.", show_alert=True)
    # Acknowledge the button tap first. Network-bound work is scheduled outside
    # the update callback so the Telegram menu remains responsive.
    await query.answer()
    action = (query.data or "").partition(":")[2]
    if action != "analyze":
        context.user_data.pop("awaiting_coin", None)
    if action == "home":
        context.user_data.pop("awaiting_coin", None)
        return await _panel(query, help_text(), MAIN_MENU)
    if action == "analyze":
        context.user_data["awaiting_coin"] = True
        return await _panel(
            query,
            "🔎 <b>ANALYZE COIN</b>\n\n"
            "Send a symbol: <code>BTC</code>, <code>SOL</code>, "
            "<code>BTCUSDT</code>.\n"
            "Or use <code>/analyze BTC</code>.\n\n"
            "Only active, liquid Bitget USDT perpetual markets are eligible.",
            BACK_MENU,
        )
    if action == "help":
        return await _panel(query, help_text(), BACK_MENU)
    if action == "settings":
        return await _panel(query, _settings_text(), SETTINGS_MENU)
    if action == "status":
        return await _panel(query, _scan_summary(), BACK_MENU)
    if action == "history":
        return await _panel(query, _history_text(), BACK_MENU)
    if action == "refresh":
        if market_refresh_task is not None and not market_refresh_task.done():
            return await _panel(query, "⏳ Refresh already running.", SETTINGS_MENU)
        if scan_lock.locked() or coin_analysis_lock.locked():
            return await _panel(query, "⏳ Scanner is busy; try refresh later.", SETTINGS_MENU)
        progress = await query.message.reply_text("🔄 Updating Bitget market list…")
        market_refresh_task = context.application.create_task(
            _background_market_refresh(progress),
            name="manual_market_refresh",
        )
        return
    if action == "scan":
        if scan_lock.locked() or (
            manual_scan_task is not None and not manual_scan_task.done()
        ):
            return await _panel(query, "⏳ A market scan is already running.", BACK_MENU)
        progress = await query.message.reply_text(
            "🔍 Market scan started. You can still use the menu."
        )
        manual_scan_task = context.application.create_task(
            _background_manual_scan(progress, context.bot, settings.admin_id),
            name="manual_market_scan",
        )
        return


async def _deliver_pending(bot, chat_id, limit=5):
    """Retry within TTL, persist delivery and cooldown only on Telegram success."""
    global last_delivery_error
    if bot is None or chat_id is None:
        return 0
    if delivery_lock.locked():
        return 0
    delivered = 0
    async with delivery_lock:
        outbox.prune()
        for item in list(outbox.items)[:limit]:
            if float(item["expires_at"]) <= time.time():
                outbox.remove(item["id"])
                continue
            try:
                await bot.send_message(chat_id=chat_id, text=item["text"],
                                       parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True,
                                       reply_markup=MAIN_MENU)
            except Exception as exc:
                last_delivery_error = type(exc).__name__
                log.warning("Telegram alert delivery failed (%s). Queued for retry.",
                            type(exc).__name__)
                outbox.failed(item["id"])
                break
            # A crash between send and persist may produce one duplicate after restart.
            item_data = dict(item["signal"])
            item_data["delivery_id"] = item["id"]
            if not any(x.get("delivery_id") == item["id"] for x in history):
                history.append(item_data)
                del history[:-300]
                _save_history()
            engine = item_data.get("strategy_id", "CORE_15M")
            state["cooldowns"][f"{engine}|{item_data['symbol']}|{item_data['side']}"] = time.time()
            outbox.remove(item["id"])
            _save_state()
            last_delivery_error = None
            delivered += 1
    return delivered


async def retry_pending(context: ContextTypes.DEFAULT_TYPE):
    if not outbox.items:
        return
    delivered = await _deliver_pending(context.bot, settings.admin_id, limit=8)
    if delivered:
        log.info("Retried and delivered %d pending alerts", delivered)


def scan_assignments(core_batch, four_hour_watch):
    """Combine original rotating 15m coverage with a recurring liquid 4H watch.

    Each symbol is scheduled once per pass. A watch-only assignment never
    evaluates the old 15m strategy, preserving its original scan coverage.
    """
    core = list(dict.fromkeys(core_batch))
    watch = list(dict.fromkeys(four_hour_watch))
    active = set(watch)
    included = set(core)
    jobs = [(symbol, True, symbol in active) for symbol in core]
    jobs.extend((symbol, False, True) for symbol in watch if symbol not in included)
    return jobs


async def _scan_one(symbol, sem, btc, eth, core_enabled=True, four_hour_enabled=True):
    """Run both engines independently; one timeframe failure cannot veto the other."""
    async with sem:
        async def scan():
            core, rs = None, None
            failures = {}
            candles, recent_4h, live = None, None, None
            async def core_attempt():
                nonlocal candles, recent_4h, live
                candles = await client.multi_timeframe(symbol, asyncio.Semaphore(1))
                recent_4h = candles["4h"]
                live = await client.live_price(symbol)
                # Original 15m engine: exact same strategy and input arguments.
                return evaluate(
                    symbol, candles, btc["trend"], eth["trend"], settings.min_score,
                    current_price=live, btc_return_6_pct=btc["return_6_pct"],
                    eth_return_6_pct=eth["return_6_pct"],
                )
            try:
                if core_enabled:
                    core = await asyncio.wait_for(
                        core_attempt(), timeout=max(60, settings.exchange_timeout_ms * 6 / 1000)
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures["CORE_15M"] = type(exc).__name__
                log.warning("15m scan skipped %s: %s (reason=%s)", symbol,
                            type(exc).__name__, safe_failure_reason(exc))

            if four_hour_enabled and settings.rs_engine_enabled:
                async def rs_attempt():
                    recent = recent_4h
                    price = live
                    if recent is None:
                        recent = await client.candles(symbol, "4h")
                    # Two 4H setup families share ONE history fetch and
                    # return at most one 4H alert per symbol/candle.
                    rs_ready = precheck_recent_breakout(recent, btc, eth)
                    slow_ready = precheck_slow_momentum(recent)
                    if not rs_ready and not slow_ready:
                        return None
                    if price is None:
                        price = await client.live_price(symbol)
                    extended = await client.extended_four_hour(symbol, recent)
                    if rs_ready:
                        primary = evaluate_relative_strength(
                            symbol, extended, price, btc, eth,
                            ttl_seconds=settings.rs_signal_ttl_seconds,
                        )
                        if primary is not None:
                            return primary
                    if slow_ready:
                        return evaluate_slow_momentum(
                            symbol, extended, price,
                            ttl_seconds=settings.rs_signal_ttl_seconds,
                        )
                    return None
                try:
                    # A slow 4H history fetch cannot discard a valid 15m signal.
                    rs = await asyncio.wait_for(rs_attempt(), timeout=75)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures["RS_4H"] = type(exc).__name__
                    log.warning("4H RS scan skipped %s: %s (reason=%s)", symbol,
                                type(exc).__name__, safe_failure_reason(exc))
            return symbol, [signal for signal in (core, rs) if signal is not None], failures

        return await scan()


async def run_scan(bot=None, chat_id=None):
    global last_scan_result
    if scan_lock.locked():
        return "A market scan is already running."
    async with scan_lock:
        scanned = skipped = no_setup = delivered = queued = 0
        core_queued = rs_queued = slow_queued = rs_errors = 0
        tasks = []
        started = time.monotonic()
        try:
            if not client.markets:
                await client.initialize()
            all_symbols = await client.top_symbols(settings.top_n, settings.min_quote_volume)
            if not all_symbols:
                raise RuntimeError("No active liquid Bitget USDT perpetual markets found")

            n = len(all_symbols)
            cap = settings.scan_batch_size
            if cap > 0 and n > cap:
                cursor = int(state.get("scan_cursor", 0)) % n
                symbols = [all_symbols[(cursor + j) % n] for j in range(cap)]
                state["scan_cursor"] = (cursor + len(symbols)) % n
            else:
                symbols = all_symbols
                state["scan_cursor"] = 0
            # Preserve deterministic rotating coverage instead of triggering thousands
            # of HTTP requests in a single Render free scan.
            macro = await asyncio.gather(
                macro_snapshot(client.exchange, "BTC/USDT:USDT"),
                macro_snapshot(client.exchange, "ETH/USDT:USDT"),
                return_exceptions=True,
            )
            btc = macro[0] if isinstance(macro[0], dict) else {"trend": "UNKNOWN", "return_6_pct": None}
            eth = macro[1] if isinstance(macro[1], dict) else {"trend": "UNKNOWN", "return_6_pct": None}
            # Core 15m rotates through ALL liquid symbols. The 4H engine
            # watches only the most liquid coins EVERY pass, including when
            # they are not in the current 15m batch. This prevents the
            # 1-hour 4H alert TTL expiring during an all-market rotation.
            four_hour_watch = (all_symbols[:settings.four_hour_top_n]
                               if settings.rs_engine_enabled else [])
            assignments = scan_assignments(symbols, four_hour_watch)
            sem = asyncio.Semaphore(settings.concurrency)
            tasks = [
                asyncio.create_task(_scan_one(
                    symbol, sem, btc, eth,
                    core_enabled=check_core, four_hour_enabled=check_four_hour
                ))
                for symbol, check_core, check_four_hour in assignments
            ]

            # Process finished markets immediately: never wait for the slowest market
            # before alerting the first valid candidate.
            for completed in asyncio.as_completed(tasks):
                _, signals, failures = await completed
                scanned += 1
                if failures.get("CORE_15M"):
                    skipped += 1
                if failures.get("RS_4H"):
                    rs_errors += 1
                if not signals:
                    no_setup += 1
                    continue
                # Each strategy gets its own symbol+side cooldown and candle
                # outbox ID, so a 4H signal cannot suppress a valid 15m alert.
                for signal in signals:
                    engine = getattr(signal, "strategy_id", "CORE_15M")
                    key = f"{engine}|{signal.symbol}|{signal.side}"
                    last = state["cooldowns"].get(key)
                    if last is None and engine == "CORE_15M":
                        last = state["cooldowns"].get(f"{signal.symbol}|{signal.side}")
                    if cooldown_active(last, settings.cooldown_minutes):
                        continue
                    if outbox.add(signal, signal_text(signal, settings.risk_percent,
                                                     settings.max_position_notional)):
                        queued += 1
                        if engine == "RS_4H":
                            rs_queued += 1
                        elif engine == "SLOW_4H":
                            slow_queued += 1
                        else:
                            core_queued += 1
                        delivered += await _deliver_pending(bot, chat_id, limit=5)

            state["last_scan"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            state["last_scan_epoch"] = time.time()
            state["last_error"] = None if scanned > skipped else "All sampled markets failed data checks"
            last_scan_result = {
                "symbols": scanned, "signals": delivered, "skipped": skipped,
                "no_setup": no_setup, "universe": n, "scan_batch": len(symbols),
                "delivery_pending": len(outbox.items),
                "core15m_queued": core_queued, "rs4h_queued": rs_queued,
                "slow4h_queued": slow_queued, "rs4h_errors": rs_errors,
                "four_hour_watch": len(four_hour_watch),
                "four_hour_extra": len(assignments) - len(symbols),
            }
            _save_state()
            duration = round(time.monotonic() - started, 1)
            return (f"Scan complete: {scanned}/{n} pairs checked in {duration}s; "
                    f"{delivered} Telegram alerts sent, {queued} queued, "
                    f"{skipped} data/errors, {no_setup} no setup. "
                    f"BTC: {btc['trend']}; ETH: {eth['trend']}. "
                    f"15m queued: {core_queued}; 4H RS queued: {rs_queued}; "
                    f"4H Slow queued: {slow_queued}; "
                    f"4H RS data/errors: {rs_errors}. "
                    f"15m rotation: {len(symbols)}; 4H liquid watch: {len(four_hour_watch)}; "
                    f"extra 4H-only checks: {len(assignments)-len(symbols)}. "
                    f"Pending: {len(outbox.items)}.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Scan failed: %s", type(exc).__name__)
            state["last_error"] = type(exc).__name__
            last_scan_result = {
                "symbols": scanned, "signals": delivered, "skipped": skipped,
                "no_setup": no_setup, "universe": 0, "scan_batch": 0,
                "delivery_pending": len(outbox.items),
            }
            _save_state()
            return f"Scan failed: {type(exc).__name__}. Check Render logs."
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


async def run_coin_analysis(symbol):
    """Manual read-only dual-engine evaluation, never an alert or automatic order."""
    if coin_analysis_lock.locked():
        return "⏳ Another coin analysis is running. Try again shortly."
    # Keep automated scans the priority on the free Render instance.
    if scan_lock.locked():
        return "⏳ Market scan in progress. Try /analyze again after it completes."
    async with coin_analysis_lock:
        if not client.markets:
            await asyncio.wait_for(client.initialize(), timeout=60)
        # Full liquidity ranking is point-in-time, matching live scanner eligibility.
        all_symbols = await asyncio.wait_for(
            client.top_symbols(settings.top_n, settings.min_quote_volume),
            timeout=120,
        )
        if symbol not in all_symbols:
            return ("⏸ <b>WAIT — not eligible</b>\\n"
                    f"{escape(symbol)} is not in the current active, liquid "
                    "Bitget USDT perpetual scanner universe. "
                    "No trade levels or signals were generated.")
        four_hour_eligible = bool(
            settings.rs_engine_enabled and
            symbol in all_symbols[:settings.four_hour_top_n]
        )
        snapshots = await asyncio.gather(
            macro_snapshot(client.exchange, "BTC/USDT:USDT"),
            macro_snapshot(client.exchange, "ETH/USDT:USDT"),
            return_exceptions=True,
        )
        btc = (snapshots[0] if isinstance(snapshots[0], dict)
               else {"trend": "UNKNOWN", "return_6_pct": None})
        eth = (snapshots[1] if isinstance(snapshots[1], dict)
               else {"trend": "UNKNOWN", "return_6_pct": None})
        _, signals, failures = await asyncio.wait_for(
            _scan_one(symbol, asyncio.Semaphore(1), btc, eth,
                      core_enabled=True, four_hour_enabled=four_hour_eligible),
            timeout=max(150, settings.exchange_timeout_ms * 10 / 1000),
        )
        return format_analysis(
            symbol, btc, eth, signals, failures,
            four_hour_eligible=four_hour_eligible,
            core_ttl_seconds=settings.signal_ttl_seconds,
            four_hour_enabled=settings.rs_engine_enabled,
        )


async def _background_coin_analysis(progress, symbol):
    try:
        result = await run_coin_analysis(symbol)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Single-coin analysis unavailable: %s (reason=%s)",
                    type(exc).__name__, safe_failure_reason(exc))
        result = ("⚠️ Coin analysis temporarily unavailable. "
                  "Market data could not be validated; no signal generated.")
    try:
        await progress.edit_text(
            result, parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU
        )
    except Exception as exc:
        log.warning("Unable to update analysis result: %s", type(exc).__name__)


async def _reply_coin_analysis(message, raw_symbol, application):
    global manual_coin_task
    symbol = normalize_coin(raw_symbol)
    if symbol is None:
        return await message.reply_text(
            "⚠️ Invalid coin. Use /analyze BTC or /analyze SOL.",
            reply_markup=MAIN_MENU,
        )
    if scan_lock.locked():
        return await message.reply_text(
            "⏳ Market scan in progress; try analysis after it finishes.",
            reply_markup=MAIN_MENU,
        )
    if manual_coin_task is not None and not manual_coin_task.done():
        return await message.reply_text(
            "⏳ Another coin analysis is running; try again shortly.",
            reply_markup=MAIN_MENU,
        )
    progress = await message.reply_text(
        f"🔎 Analyzing {escape(symbol)}… Menu is still responsive."
    )
    manual_coin_task = application.create_task(
        _background_coin_analysis(progress, symbol),
        name="manual_coin_analysis",
    )


async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    if not context.args:
        context.user_data["awaiting_coin"] = True
        return await update.effective_message.reply_text(
            "🔎 Send a coin symbol like BTC, SOL or 1000PEPE. "
            "Or use /analyze BTC.", reply_markup=BACK_MENU,
        )
    context.user_data.pop("awaiting_coin", None)
    await _reply_coin_analysis(
        update.effective_message, " ".join(context.args), context.application
    )


async def analyze_coin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update) or not context.user_data.get("awaiting_coin"):
        return
    context.user_data.pop("awaiting_coin", None)
    await _reply_coin_analysis(
        update.effective_message, update.effective_message.text,
        context.application,
    )


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global manual_scan_task
    if not authorized(update):
        return await deny(update)
    if scan_lock.locked() or (
        manual_scan_task is not None and not manual_scan_task.done()
    ):
        return await update.effective_message.reply_text(
            "⏳ A market scan is already running.", reply_markup=MAIN_MENU
        )
    progress = await update.effective_message.reply_text(
        "🔍 Market scan started. You can still use the menu."
    )
    manual_scan_task = context.application.create_task(
        _background_manual_scan(progress, context.bot, settings.admin_id),
        name="manual_market_scan",
    )


async def scheduled_scan(context: ContextTypes.DEFAULT_TYPE):
    result = await run_scan(context.bot, settings.admin_id)
    log.info("Scheduled scan: %s", result)


async def post_init(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Open the main menu"),
            BotCommand("scan", "Scan eligible markets now"),
            BotCommand("analyze", "Analyze one Bitget coin"),
            BotCommand("status", "Scanner health and coverage"),
            BotCommand("settings", "Scanner settings"),
            BotCommand("history", "Delivered signals only"),
            BotCommand("help", "Help and commands"),
        ])
    except Exception as exc:
        log.warning("Unable to register bot commands: %s", type(exc).__name__)
    try:
        await client.initialize()
        log.info("Loaded %d Bitget markets", len(client.markets))
    except Exception as exc:
        log.warning("Initial market discovery failed: %s", type(exc).__name__)
    if application.job_queue:
        # Align repeat scans with a just-closed 15m candle instead of a random
        # offset determined by Render's process boot time.
        first_boundary = (900 - int(time.time()) % 900) + 12
        application.job_queue.run_repeating(scheduled_scan, interval=settings.scan_seconds,
                                            first=first_boundary, name="market_scan")
        application.job_queue.run_once(scheduled_scan, when=30, name="startup_scan")
        application.job_queue.run_repeating(retry_pending, interval=60,
                                            first=60, name="retry_telegram_alerts")
    else:
        log.error("JobQueue unavailable; scheduled scans are disabled.")


async def post_shutdown(application: Application):
    await client.close()


def main():
    threading.Thread(target=run_health_server, daemon=True, name="health-server").start()
    app = (Application.builder().token(settings.telegram_token)
           .post_init(post_init).post_shutdown(post_shutdown)
           .concurrent_updates(4).build())
    for command, handler in (
        ("start", start_cmd), ("help", help_cmd), ("scan", scan_cmd),
        ("analyze", analyze_cmd),
        ("status", status_cmd), ("settings", settings_cmd), ("history", history_cmd),
    ):
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(
        menu_callback, pattern=r"^menu:(scan|analyze|status|history|settings|help|refresh|home)$"
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_coin_text))
    log.info("A&T TrendHive Crypto Sniper — signals only, no order placement")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
