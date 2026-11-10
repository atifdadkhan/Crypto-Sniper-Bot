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
from strategy_modes import MODE_LABELS, valid_mode
from independent_engines import evaluate_engines
from indicators import enrich
from opportunity_ledger import observe, summary as watch_summary
from relative_strength import evaluate_relative_strength, precheck_recent_breakout
from slow_momentum import evaluate_slow_momentum, precheck_slow_momentum
from formatting import signal_text, help_text, price
from orderflow_stream import run_orderflow
from bitget_orderflow_journal import BitgetPaperJournal, english_alert
from central_bitget_strategies import (
    attach_hour_context, live_registry, normalized_candle_signal,
    unified_candle_alert,
)
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
ORDERFLOW_ONLY = os.getenv("CRYPTO_ORDERFLOW_ONLY", "true").strip().lower() not in ("0", "false", "no")
# Centralize all supported Bitget perpetual engines without enabling Binance Spot.
# The original ORDERFLOW_ONLY mode remains available by setting this to false.
UNIFIED_LIVE = os.getenv("CRYPTO_UNIFIED_LIVE", "true").strip().lower() not in ("0", "false", "no")
data_dir = Path(settings.data_dir)
data_dir.mkdir(parents=True, exist_ok=True)
STATE_FILE, HISTORY_FILE = data_dir / "state.json", data_dir / "history.json"
state = load_json(STATE_FILE, {})
if not isinstance(state.get("cooldowns"), dict):
    state["cooldowns"] = {}
state.setdefault("scan_cursor", 0)
state.setdefault("last_scan", None)
state.setdefault("last_error", None)
state["strategy_mode"] = valid_mode(state.get("strategy_mode", "recommended"))
state["orderflow_mode"] = state.get("orderflow_mode") if state.get("orderflow_mode") in ("both", "cvd", "wick") else "both"
state.setdefault("last_scan_result", {})
if not isinstance(state.get("watch_book"), dict):
    state["watch_book"] = {}
if not isinstance(state.get("watch_outcomes"), list):
    state["watch_outcomes"] = []
history = load_json(HISTORY_FILE, [])
if not isinstance(history, list):
    history = []

outbox = AlertOutbox(data_dir / "telegram_outbox.json", settings.signal_ttl_seconds)
bitget_paper_journal = None


def _bitget_journal():
    """Lazy SQLite connection; its database lives beside existing history.json."""
    global bitget_paper_journal
    if bitget_paper_journal is None:
        bitget_paper_journal = BitgetPaperJournal(
            data_dir / "bitget_orderflow_paper.sqlite3"
        )
    return bitget_paper_journal

scan_lock = asyncio.Lock()
coin_analysis_lock = asyncio.Lock()
delivery_lock = asyncio.Lock()
client = MarketData(settings.exchange_timeout_ms, settings.request_rate_limit)
last_scan_result = {"symbols": 0, "signals": 0, "skipped": 0, "no_setup": 0,
                    "developing": 0,
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
orderflow_task = None
orderflow_status = {"connected": False, "symbols": [], "last_trade_at": None, "last_book_at": None, "error": None}

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔍 Scan Market", callback_data="menu:scan"),
     InlineKeyboardButton("🔎 Analyze Coin", callback_data="menu:analyze")],
    [InlineKeyboardButton("📊 Market Overview", callback_data="menu:status"),
     InlineKeyboardButton("📚 Signal History", callback_data="menu:history")],
    [InlineKeyboardButton("🎯 Signal Style", callback_data="menu:strategy"),
     InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings")],
    [InlineKeyboardButton("🩺 System Status", callback_data="menu:system"),
     InlineKeyboardButton("❔ Help", callback_data="menu:help")],
])
BACK_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:home")]
])
SETTINGS_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔄 Refresh Markets", callback_data="menu:refresh"),
     InlineKeyboardButton("🧹 Clear Cache", callback_data="menu:cache")],
    [InlineKeyboardButton("🎯 Signal Style", callback_data="menu:strategy"),
     InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:home")]
])
CENTRAL_STRATEGY_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("⭐ Both Orderflow Engines", callback_data="ofmode:both")],
    [InlineKeyboardButton("📉 CVD Divergence", callback_data="ofmode:cvd"),
     InlineKeyboardButton("💧 Wick Hunt", callback_data="ofmode:wick")],
    [InlineKeyboardButton("🧠 15m Best Match", callback_data="mode:recommended")],
    [InlineKeyboardButton("🚀 15m Breakout", callback_data="mode:breakout"),
     InlineKeyboardButton("📈 15m EMA", callback_data="mode:ema")],
    [InlineKeyboardButton("🛡️ 15m Extra Check", callback_data="mode:rsi_macd")],
    [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:home")],
])
ORDERFLOW_STRATEGY_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("⭐ Both Orderflow Engines", callback_data="ofmode:both")],
    [InlineKeyboardButton("📉 CVD Divergence", callback_data="ofmode:cvd")],
    [InlineKeyboardButton("💧 Liquidity Wick Hunt", callback_data="ofmode:wick")],
    [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:home")],
])
STRATEGY_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("⭐ Best Match (Recommended)", callback_data="mode:recommended")],
    [InlineKeyboardButton("🚀 Breakout + Volume", callback_data="mode:breakout")],
    [InlineKeyboardButton("📈 Early Trend (EMA)", callback_data="mode:ema")],
    [InlineKeyboardButton("🛡️ Extra Check (RSI + MACD)", callback_data="mode:rsi_macd")],
    [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu:home")],
])
health_app = Flask(__name__)


def _strategy_text():
    if ORDERFLOW_ONLY:
        mode = {"both": "Both Orderflow Engines", "cvd": "CVD Divergence",
                "wick": "Liquidity Wick Hunt"}[state["orderflow_mode"]]
        return ("🎯 <b>LIVE ORDERFLOW ENGINES</b>\n\n"
                f"Selected: <b>{escape(mode)}</b>\n\n"
                "📉 CVD: aggressive trade-volume divergence with reversal.\n"
                "💧 Wick Hunt: 1H stop sweep, 3:1 L2 imbalance, VWAP reversion.\n"
                + ("<b>Central Bitget mode:</b> 15m Pullback/Breakout and "
                   "4H Relative Strength/Slow Momentum are ALSO running.\n"
                   f"15m style: {escape(MODE_LABELS[valid_mode(state['strategy_mode'])])}.\n"
                   if UNIFIED_LIVE else
                   "The 15m/4H scheduled scanners are disabled.\n")
                + "Signals only; no authenticated order execution.")
    selected = valid_mode(state.get("strategy_mode", "recommended"))
    return (
        "🎯 <b>CHOOSE SIGNAL STYLE</b>\n\n"
        f"Current: <b>{escape(MODE_LABELS[selected])}</b>\n\n"
        "⭐ Best Match: bot chooses from three checks.\n"
        "🚀 Breakout: price breaks a level with strong activity.\n"
        "📈 Early Trend: catches a fresh trend change.\n"
        "🛡️ Extra Check: waits for two indicators to agree.\n\n"
        "All choices still need live-price and safety checks. "
        "This affects 15m alerts; experimental 4H alerts are separate. "
        "No choice guarantees profit."
    )


def _readiness():
    if ORDERFLOW_ONLY:
        t, b = orderflow_status.get("last_trade_at"), orderflow_status.get("last_book_at")
        fresh = bool(t and b and time.time() - t < 30 and time.time() - b < 30)
        detail = {"scanner_fresh": fresh, "last_scan_age_seconds": None,
                  "pending_alerts": len(outbox.items), "last_error": orderflow_status.get("error"),
                  "last_scan": "Live CCXT Pro orderflow streams",
                  "scan": {"symbols": len(orderflow_status.get("symbols") or []),
                           "universe": len(orderflow_status.get("symbols") or []), "scan_batch": 0},
                  "orderflow": dict(orderflow_status)}
        if UNIFIED_LIVE:
            stamp = state.get("last_scan_epoch")
            try:
                age = int(time.time() - float(stamp)) if stamp else None
            except (ValueError, TypeError):
                age = None
            detail["candle_scanner_fresh"] = bool(
                age is not None and 0 <= age < max(settings.scan_seconds * 2, 1800)
            )
            detail["candle_scan_age_seconds"] = age
            detail["candle_scan_error"] = state.get("last_error")
            detail["candle_scan"] = last_scan_result
            detail["orderflow_fresh"] = fresh
            detail["central_strategies"] = live_registry(
                state["orderflow_mode"], True, settings.rs_engine_enabled
            )
        return detail
    last = state.get("last_scan_epoch")
    try:
        seconds = int(time.time() - float(last)) if last is not None else None
    except (ValueError, TypeError):
        seconds = None
    is_fresh = seconds is not None and 0 <= seconds < max(settings.scan_seconds * 2, 1800)
    detail = {"scanner_fresh": is_fresh, "last_scan_age_seconds": seconds,
              "pending_alerts": len(outbox.items),
              "last_error": state.get("last_error"),
              "last_scan": state.get("last_scan"), "scan": last_scan_result}
    if UNIFIED_LIVE:
        t, b = orderflow_status.get("last_trade_at"), orderflow_status.get("last_book_at")
        flow_fresh = bool(t and b and time.time() - t < 30 and time.time() - b < 30)
        detail["orderflow_fresh"] = flow_fresh
        detail["orderflow"] = dict(orderflow_status)
        detail["candle_scanner_fresh"] = is_fresh
        detail["candle_scan_error"] = state.get("last_error")
        detail["central_strategies"] = live_registry(
            state["orderflow_mode"], True, settings.rs_engine_enabled
        )
    return detail


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
    if UNIFIED_LIVE:
        ready_now = bool(
            ready_now and detail.get("orderflow_fresh")
            and detail.get("candle_scanner_fresh")
            and detail.get("candle_scan_error") is None
        )
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
    if ORDERFLOW_ONLY:
        detail = _readiness()
        symbols = orderflow_status.get("symbols") or []
        central = ""
        if UNIFIED_LIVE:
            central = (
                "\n<b>15m + 4H candle scanner:</b> "
                f"{'FRESH' if detail.get('candle_scanner_fresh') else 'WAIT / DEGRADED'}\n"
                f"Last scan: {escape(str(state.get('last_scan') or 'Not yet'))}\n"
                f"Scanned: {last_scan_result.get('symbols', 0)} / "
                f"{last_scan_result.get('universe', 0)}\n"
                f"15m queued: {last_scan_result.get('core15m_queued', 0)} | "
                f"4H RS: {last_scan_result.get('rs4h_queued', 0)} | "
                f"4H Slow: {last_scan_result.get('slow4h_queued', 0)}\n"
                f"Last candle-scan error: {escape(str(state.get('last_error') or 'None'))}\n"
            )
        return ("📊 <b>CENTRAL BITGET MARKET OVERVIEW</b>\n"
                f"Market: Bitget USDT perpetuals | Orderflow watch: {len(symbols)} pairs\n"
                f"Symbols: {escape(', '.join(s.split('/')[0] for s in symbols))}\n"
                f"WebSocket trades + L2 fresh: {'YES' if detail['scanner_fresh'] else 'NOT YET'}\n"
                f"Orderflow mode: {escape(state['orderflow_mode'])}\n"
                + central + "Signals only; no automatic orders.")
    result = last_scan_result
    research = watch_summary(state["watch_book"], state["watch_outcomes"])
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
        f"⏸ No setup: {result['no_setup']} | 👀 WATCH: {result.get('developing', 0)}\n"
        f"📈 WATCH follow-through (1 ATR): {research['favorable_1atr']}/{research['measured']} (not PnL)\n"
        f"📊 Opportunity Report: {result['signals']} delivered | {result['no_setup']} without setup | {result['skipped']} data skips\n"
        f"Last error: {escape(str(state.get('last_error') or 'None'))}\n\n"
        "<i>Manual signals only. Zero alerts can be normal.</i>"
    )


def _settings_text():
    if ORDERFLOW_ONLY:
        return ("⚙️ <b>LIVE BITGET ORDERFLOW SETTINGS</b>\n"
                f"Markets: dynamic top {escape(os.getenv('ORDERFLOW_TOP_N', '8'))} liquid USDT swaps\n"
                f"Minimum 24h volume: {settings.min_quote_volume:,.0f} USDT\n"
                f"Engines: {escape(state['orderflow_mode'])}\n"
                "Free CCXT Pro trades + L2 depth; 1H swing levels.\n"
                + ("15m closed-candle scanner: ON (rotating liquid Bitget markets).\n"
                   f"4H RS + Slow Momentum: {'ON' if settings.rs_engine_enabled else 'OFF'} "
                   f"(liquid top {settings.four_hour_top_n}).\n"
                   f"15m style: {escape(MODE_LABELS[valid_mode(state['strategy_mode'])])}.\n"
                   if UNIFIED_LIVE else
                   "15m/4H scanners: OFF (orderflow-only configuration).\n")
                + "You choose your own investment; no live orders.")
    return (
        "⚙️ <b>SCANNER SETTINGS</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"Markets: Top {settings.top_n or 'all eligible'}\n"
        f"Min 24h volume: {settings.min_quote_volume:,.0f} USDT\n"
        f"Signal quality floor: {settings.min_score}/10\n"
        f"Signal style: {escape(MODE_LABELS[valid_mode(state.get('strategy_mode', 'recommended'))])}\n"
        f"Scan interval: {settings.scan_seconds // 60} min\n"
        f"15m batch: {settings.scan_batch_size or 'All'}\n"
        f"4H research engine: {'ON' if settings.rs_engine_enabled else 'OFF'}\n"
        f"4H liquid watch: top {settings.four_hour_top_n}\n"
        f"Cooldown: {settings.cooldown_minutes} min\n"
        "\n<i>You choose how much USDT to invest yourself. "
        "These are price-level signals only; no automatic orders.</i>"
    )


def _system_status_text():
    if ORDERFLOW_ONLY:
        detail = _readiness()
        return ("🩺 <b>ORDERFLOW SYSTEM STATUS</b>\n"
                f"Live trade/book freshness: {'OK' if detail['scanner_fresh'] else 'WAIT / DEGRADED'}\n"
                f"Watching: {len(orderflow_status.get('symbols') or [])} liquid USDT pairs\n"
                f"Latest stream error: {escape(str(orderflow_status.get('error') or 'None'))}\n"
                + (f"15m/4H scanner: {'FRESH' if detail.get('candle_scanner_fresh') else 'WAIT / DEGRADED'}\n"
                   f"Candle-scan error: {escape(str(detail.get('candle_scan_error') or 'None'))}\n"
                   if UNIFIED_LIVE else "")
                + "Separate engines, centralized paper ledger; no automatic orders.")
    detail = _readiness()
    scanner = "🟢 Scanning recently" if detail["scanner_fresh"] else "🟠 Scan not yet fresh"
    return (
        "🩺 <b>SYSTEM STATUS</b>\n"
        f"Scanner: {scanner}\n"
        f"Last scan: {escape(str(detail['last_scan'] or 'Not yet'))}\n"
        f"Pending Telegram alerts: {detail['pending_alerts']}\n"
        f"Last scan error: {escape(str(detail['last_error'] or 'None'))}\n"
        "<i>Manual signals only. No TP/SL-hit notifications or order execution.</i>"
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
    if ORDERFLOW_ONLY:
        try:
            counts = _bitget_journal().outcome_counts()
            lines.append(
                "\n<b>Bitget PAPER outcomes (1H closed candles):</b> "
                f"{counts.get('wins') or 0} wins, "
                f"{counts.get('losses') or 0} losses, "
                f"{counts.get('open') or 0} open, "
                f"{counts.get('gaps') or 0} unverified data gaps."
            )
            lines.append("<i>Paper estimates only; not exchange-executed P&amp;L.</i>")
        except Exception as exc:
            log.warning("Bitget paper journal history unavailable: %s", type(exc).__name__)
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


def _research_text():
    """Readable, evidence-based scorecard from delivered Bitget paper signals."""
    journal = _bitget_journal()
    counts = journal.outcome_counts()
    strategies = journal.performance_by_strategy()
    breakdown = journal.research_breakdown()
    wins, losses = counts.get("wins") or 0, counts.get("losses") or 0
    open_count, gaps = counts.get("open") or 0, counts.get("gaps") or 0
    closed = wins + losses
    lines = [
        "🔬 <b>STRATEGY RESEARCH REPORT</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📊 <b>All signals:</b> {wins} wins · {losses} losses · "
        f"{open_count} still open · {gaps} unverified data gaps",
        "",
        "<b>RESULTS BY STRATEGY</b>",
    ]
    if not strategies:
        lines.append("No delivered signals have been recorded yet.")
    for item in strategies:
        sw, sl = item.get("wins") or 0, item.get("losses") or 0
        done = sw + sl
        rate = (100.0 * sw / done) if done else None
        avg = item.get("avg_net_pct")
        avg_text = f"{float(avg):+.2f}%" if avg is not None else "not available"
        rate_text = f"{rate:.0f}%" if rate is not None else "not enough closed results"
        lines.append(
            f"• <b>{escape(str(item['engine_id']))}</b>: "
            f"{item['signals']} signals · {sw} wins / {sl} losses · "
            f"{rate_text} win rate · {avg_text} average paper return"
        )
    lines.extend(["", "<b>COIN + DIRECTION + MARKET BREAKDOWN</b>"])
    if not breakdown:
        lines.append("No coin-level results yet.")
    for item in breakdown[:10]:
        coin = str(item["symbol"]).split("/")[0].split(":")[0]
        done = item["closed"]
        rate = 100.0 * item["wins"] / done if done else None
        avg = item.get("avg_net_pct")
        rate_text = f"{rate:.0f}% wins" if rate is not None else "no closed results"
        avg_text = f"{float(avg):+.2f}% avg" if avg is not None else "avg unavailable"
        lines.append(
            f"• <b>{escape(coin)} {escape(str(item['side']))}</b> · "
            f"{escape(str(item['engine_id']))} · {escape(str(item['condition']))}: "
            f"{item['wins']}W/{item['losses']}L · {rate_text} · {avg_text}"
        )
    lines.extend([
        "",
        "<b>HOW THIS HELPS FUTURE RESEARCH</b>",
        "We keep each delivered signal, its strategy, coin, direction, market condition, "
        "the 40 available closed 1H candles before the signal, and the following closed "
        "candles used for its paper result.",
        "Open trades and data gaps are not counted as wins or losses. Treat groups with "
        "fewer than 20 closed examples as early evidence only. Test any proposed change "
        "with out-of-sample / walk-forward backtesting before changing live strategies.",
        "<i>Paper estimates from public Bitget candles — not real fills or account P&amp;L. "
        "No automatic orders.</i>",
    ])
    return "\n".join(lines)


async def research_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return await deny(update)
    try:
        report = _research_text()
    except Exception as exc:
        log.warning("Bitget research report unavailable: %s", type(exc).__name__)
        report = "⚠️ Research report unavailable. The saved signal journal was not changed."
    await update.effective_message.reply_text(
        report, parse_mode=ParseMode.HTML, reply_markup=BACK_MENU
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
    if ORDERFLOW_ONLY and (query.data or "").startswith("ofmode:"):
        requested = (query.data or "").partition(":")[2]
        if requested in ("both", "cvd", "wick"):
            state["orderflow_mode"] = requested
            _save_state()
        return await _panel(query, "✅ Orderflow engines updated.\n\n" + _strategy_text(),
                            CENTRAL_STRATEGY_MENU if UNIFIED_LIVE else ORDERFLOW_STRATEGY_MENU)
    if ORDERFLOW_ONLY and not UNIFIED_LIVE and (query.data or "").startswith("mode:"):
        return await _panel(query, _strategy_text(), ORDERFLOW_STRATEGY_MENU)
    if (query.data or "").startswith("mode:"):
        requested = (query.data or "").partition(":")[2]
        if requested not in MODE_LABELS:
            return await _panel(query, "⚠️ Unknown signal style.",
                                CENTRAL_STRATEGY_MENU if UNIFIED_LIVE else STRATEGY_MENU)
        if scan_lock.locked():
            return await _panel(query, "⏳ Scan running. Change the style after it finishes.",
                                CENTRAL_STRATEGY_MENU if UNIFIED_LIVE else STRATEGY_MENU)
        state["strategy_mode"] = requested
        _save_state()
        return await _panel(query, "✅ Style updated.\n\n" + _strategy_text(),
                            CENTRAL_STRATEGY_MENU if UNIFIED_LIVE else STRATEGY_MENU)
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
    if action == "strategy":
        return await _panel(query, _strategy_text(),
                            CENTRAL_STRATEGY_MENU if UNIFIED_LIVE else
                             (ORDERFLOW_STRATEGY_MENU if ORDERFLOW_ONLY else STRATEGY_MENU))
    if action == "settings":
        return await _panel(query, _settings_text(), SETTINGS_MENU)
    if action == "status":
        return await _panel(query, _scan_summary(), BACK_MENU)
    if action == "system":
        return await _panel(query, _system_status_text(), BACK_MENU)
    if action == "cache":
        if scan_lock.locked() or coin_analysis_lock.locked():
            return await _panel(query, "⏳ Scanner busy. Clear cache after this scan.", SETTINGS_MENU)
        # Only invalidate the short-lived liquid-market ranking. Never touch
        # candle history, signal history, cooldowns or stored Telegram alerts.
        client._rank_cache = (0.0, None, [])
        return await _panel(
            query, "✅ Crypto market-list cache cleared.\n"
                   "Next scan refreshes eligible USDT markets.\n"
                   "Signals, cooldowns and settings are preserved.", SETTINGS_MENU)
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
        if ORDERFLOW_ONLY and not UNIFIED_LIVE:
            return await _panel(query, _scan_summary(), BACK_MENU)
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


def _confirmed_signal_history(record, paper_signal, delivery_id=None):
    """One history/journal finalizer used by BOTH orderflow and candle engines.

    Called only after Telegram send_message returns successfully. No exchange
    execution is implied. Ledger failures must not re-send a delivered alert.
    """
    item = dict(record)
    if delivery_id:
        item["delivery_id"] = delivery_id
    try:
        item["paper_signal_id"] = _bitget_journal().record_delivered(paper_signal)
    except Exception as exc:
        item["paper_signal_id"] = None
        log.warning("Central Bitget paper journal failed: %s", type(exc).__name__)
    item.pop("closed_1h", None)  # Actual OHLCV snapshot stays in SQLite, not JSON history.
    if not delivery_id or not any(x.get("delivery_id") == delivery_id for x in history):
        history.append(item)
        del history[:-300]
        _save_history()
    return item["paper_signal_id"]


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
            if UNIFIED_LIVE:
                try:
                    converted = normalized_candle_signal(
                        item_data, int(time.time() * 1000), source_key=item["id"]
                    )
                    item_data["engine_id"] = converted.engine_id
                    _confirmed_signal_history(item_data, converted, delivery_id=item["id"])
                except Exception as exc:
                    # Telegram already acknowledged the send. Never re-send
                    # solely because journal normalization failed.
                    log.warning("Central Bitget candle journal conversion failed: %s",
                                type(exc).__name__)
                    item_data["delivery_id"] = item["id"]
                    item_data["paper_signal_id"] = None
                    item_data.pop("closed_1h", None)
                    if not any(x.get("delivery_id") == item["id"] for x in history):
                        history.append(item_data)
                        del history[:-300]
                        _save_history()
            else:
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


def _audit_crypto_watch(symbol, frame, should_watch=False, reason="Developing setup"):
    """Track next four completed candles for a rejected/watch opportunity."""
    if frame is None or len(frame) < 100:
        return
    try:
        last = enrich(frame).iloc[-1]
        bars = [
            (int(row.timestamp), float(row.high), float(row.low), float(row.close))
            for row in frame.tail(16).itertuples(index=False)
        ]
        # Use the same causal closed-bar feature calculations as the trade engine.
        side = "LONG" if float(last.close) >= float(last.ema21) else "SHORT"
        observe(state["watch_book"], state["watch_outcomes"],
                symbol, side, bars, should_watch=should_watch,
                reason=reason, atr=float(last.atr))
    except (ValueError, TypeError, KeyError, AttributeError):
        log.debug("WATCH evidence not available for %s", symbol)


async def _scan_one(symbol, sem, btc, eth, core_enabled=True, four_hour_enabled=True, track_opportunities=False):
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
                candidate = evaluate(
                    symbol, candles, btc["trend"], eth["trend"], settings.min_score,
                    current_price=live, btc_return_6_pct=btc["return_6_pct"],
                    eth_return_6_pct=eth["return_6_pct"],
                    strategy_mode=state.get("strategy_mode", "recommended"),
                )
                if candidate is not None and UNIFIED_LIVE and hasattr(candidate, "to_dict"):
                    attach_hour_context(candidate, candles.get("1h"))
                if track_opportunities:
                    evidence = [
                        e for direction in ("LONG", "SHORT")
                        for e in evaluate_engines(candles["15m"], direction)
                        if e.confirmed
                    ]
                    is_watch = candidate is None and bool(evidence)
                    if is_watch:
                        failures["WATCH"] = ",".join(sorted({e.engine for e in evidence}))
                    _audit_crypto_watch(symbol, candles["15m"], is_watch,
                                        failures.get("WATCH", "No WATCH setup"))
                return candidate
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
                    if rs is not None and UNIFIED_LIVE and hasattr(rs, "to_dict"):
                        hourly = candles.get("1h") if candles is not None else None
                        if hourly is None:
                            try:
                                hourly = await asyncio.wait_for(
                                    client.candles(symbol, "1h"), timeout=35
                                )
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                hourly = None
                        attach_hour_context(rs, hourly)
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
        scanned = skipped = no_setup = delivered = queued = developing = 0
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
                # Always scan the most liquid assets each pass; rotate the
                # remainder using the existing finite API-request budget.
                priority = min(settings.four_hour_top_n, max(1, cap // 3))
                priority = min(priority, n)
                tail = all_symbols[priority:]
                width = cap - priority
                cursor = int(state.get("scan_cursor", 0)) % len(tail)
                symbols = list(all_symbols[:priority]) + [
                    tail[(cursor + j) % len(tail)] for j in range(width)
                ]
                state["scan_cursor"] = (cursor + width) % len(tail)
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
                    core_enabled=check_core, four_hour_enabled=check_four_hour,
                    track_opportunities=True
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
                if failures.get("WATCH"):
                    developing += 1
                if not signals:
                    if not failures.get("CORE_15M") and not failures.get("RS_4H"):
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
                    if outbox.add(signal, (unified_candle_alert(signal, price)
                                           if UNIFIED_LIVE else
                                           signal_text(signal, settings.risk_percent,
                                                       settings.max_position_notional))):
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
                "no_setup": no_setup, "developing": developing,
                "universe": n, "scan_batch": len(symbols),
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
    if ORDERFLOW_ONLY and not UNIFIED_LIVE:
        active = symbol in (orderflow_status.get("symbols") or [])
        return (f"🔎 <b>{escape(symbol)}</b>\n"
                + ("✅ Live trade/order-book feed is configured for this pair.\n" if active
                   else "⏸ Not in the current liquid orderflow watch; no fake analysis.\n")
                + "Signals appear automatically only on confirmed CVD or wick events.")
    if coin_analysis_lock.locked():
        return "⏳ Another coin analysis is running. Try again shortly."
    # Queue one analysis instead of forcing the user to resend after each scan.
    async with coin_analysis_lock:
        if scan_lock.locked():
            try:
                await asyncio.wait_for(scan_lock.acquire(), timeout=120)
            except asyncio.TimeoutError:
                return "⏳ Market scan is busy. Please try again shortly."
            else:
                scan_lock.release()
        if not client.markets:
            await asyncio.wait_for(client.initialize(), timeout=60)
        # Full liquidity ranking is point-in-time, matching live scanner eligibility.
        all_symbols = await asyncio.wait_for(
            client.top_symbols(settings.top_n, settings.min_quote_volume),
            timeout=120,
        )
        if symbol not in all_symbols:
            return ("⏸ <b>WAIT — not eligible</b>\n"
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
    if manual_coin_task is not None and not manual_coin_task.done():
        return await message.reply_text(
            "⏳ Another coin analysis is running; try again shortly.",
            reply_markup=MAIN_MENU,
        )
    progress = await message.reply_text(
        f"🔎 {escape(symbol)}: Analysis queued…" if scan_lock.locked() else f"🔎 Analyzing {escape(symbol)}…"
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
    if ORDERFLOW_ONLY and not UNIFIED_LIVE:
        return await update.effective_message.reply_text(
            _scan_summary(), parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)
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


async def scheduled_opportunity_report(context: ContextTypes.DEFAULT_TYPE):
    """At most one brief report per 4 hours; skip stale or missing scans."""
    try:
        stamp = float(state.get("last_scan_epoch") or 0)
        age = time.time() - stamp
        if stamp <= 0 or age < 0 or age > max(3600, settings.scan_seconds * 2):
            return
        r = last_scan_result
        if not r.get("symbols"):
            return
        message = (
            "📊 <b>CRYPTO OPPORTUNITY REPORT</b>\n"
            f"Scanned: {r['symbols']} / {r['universe']}\n"
            f"Alerts delivered: {r['signals']} | Pending: {len(outbox.items)}\n"
            f"No setup: {r['no_setup']} | Developing: {r.get('developing', 0)}\n"
            f"Data errors: {r['skipped']} | Follow-through stats in Market Status\n"
            "Tap Market Status for details."
        )
        await context.bot.send_message(
            chat_id=settings.admin_id, text=message, parse_mode=ParseMode.HTML
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Opportunity report unavailable: %s", type(exc).__name__)


async def publish_orderflow_signal(application, signal):
    """Live orderflow uses the same English plan and paper ledger as candle alerts."""
    key = f"{signal.engine_id}|{signal.symbol}|{signal.side}"
    last = state["cooldowns"].get(key)
    if last is not None and cooldown_active(last, settings.cooldown_minutes):
        return
    body = english_alert(signal, price)
    await application.bot.send_message(chat_id=settings.admin_id, text=body,
                                       parse_mode=ParseMode.HTML, reply_markup=MAIN_MENU)
    record = {
        "symbol": signal.symbol, "side": signal.side,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "strategy_id": signal.engine_id,
        "engine_id": signal.engine_id, "strategy_name": signal.strategy_name,
        "entry": signal.entry, "stop_loss": signal.stop_loss,
        "tp1": signal.tp1, "tp2": signal.tp2,
        "market_condition": getattr(signal, "market_condition", "CAUTIOUS"),
    }
    _confirmed_signal_history(record, signal)
    state["cooldowns"][key] = time.time()
    _save_state()
    log.info("Central Bitget Telegram signal delivered: %s %s %s",
             signal.symbol, signal.engine_id, signal.side)


async def refresh_bitget_paper_outcomes(context):
    """Check delivered signal outcomes on validated, closed Bitget 1h candles.

    This is intentionally separate from Telegram delivery and exchange orders.
    Unavailable/stale candle feeds do not fabricate wins or losses.
    """
    try:
        journal = _bitget_journal()
        symbols = journal.open_symbols()
    except Exception as exc:
        log.warning("Bitget paper journal unavailable: %s", type(exc).__name__)
        return
    for symbol in symbols:
        try:
            frame = await asyncio.wait_for(client.candles(symbol, "1h"), timeout=40)
            journal.advance(symbol, frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Bitget paper outcome refresh %s: %s",
                        symbol, type(exc).__name__)

async def post_init(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Open the main menu"),
            BotCommand("scan", "Scan eligible markets now"),
            BotCommand("analyze", "Analyze one Bitget coin"),
            BotCommand("status", "Scanner health and coverage"),
            BotCommand("settings", "Scanner settings"),
            BotCommand("history", "Delivered signals only"),
            BotCommand("research", "Strategy and coin paper performance"),
            BotCommand("help", "Help and commands"),
        ])
    except Exception as exc:
        log.warning("Unable to register bot commands: %s", type(exc).__name__)
    try:
        await client.initialize()
        log.info("Loaded %d Bitget markets", len(client.markets))
    except Exception as exc:
        log.warning("Initial market discovery failed: %s", type(exc).__name__)
    global orderflow_task
    if ORDERFLOW_ONLY or UNIFIED_LIVE:
        orderflow_task = asyncio.create_task(
            run_orderflow(client, settings,
                          lambda: state.get("orderflow_mode", "both"),
                          lambda signal: publish_orderflow_signal(application, signal),
                          orderflow_status), name="live_bitget_orderflow")
        if application.job_queue:
            application.job_queue.run_repeating(retry_pending, interval=60, first=60,
                                                name="retry_telegram_alerts")
            application.job_queue.run_repeating(refresh_bitget_paper_outcomes,
                                                interval=900, first=180,
                                                name="bitget_paper_outcome_refresh")
        log.info("Bitget CVD/Wick market-data engines enabled")
        if not UNIFIED_LIVE:
            return
    if application.job_queue and (UNIFIED_LIVE or not ORDERFLOW_ONLY):
        # Align repeat scans with a just-closed 15m candle instead of a random
        # offset determined by Render's process boot time.
        first_boundary = (900 - int(time.time()) % 900) + 12
        application.job_queue.run_repeating(scheduled_scan, interval=settings.scan_seconds,
                                            first=first_boundary, name="market_scan")
        application.job_queue.run_once(scheduled_scan, when=30, name="startup_scan")
        if not (ORDERFLOW_ONLY or UNIFIED_LIVE):
            application.job_queue.run_repeating(
                retry_pending, interval=60, first=60, name="retry_telegram_alerts"
            )
        application.job_queue.run_repeating(
            scheduled_opportunity_report, interval=14400, first=14400,
            name="crypto_opportunity_report"
        )
    elif application.job_queue is None:
        log.error("JobQueue unavailable; scheduled scans are disabled.")
    if UNIFIED_LIVE:
        log.info("CENTRAL BITGET LIVE: orderflow CVD/Wick + 15m core + 4H RS/Slow, shared Telegram/history/SQLite; NO ORDERS")


async def post_shutdown(application: Application):
    if orderflow_task is not None:
        orderflow_task.cancel()
        await asyncio.gather(orderflow_task, return_exceptions=True)
    await client.close()


async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Telegram polling/update errors without exposing credentials."""
    error = context.error
    if isinstance(error, Exception):
        log.warning("Telegram update/polling error (%s); library will retry transient network failures", type(error).__name__)
    else:
        log.warning("Telegram update/polling error (unknown type)")


def main():
    threading.Thread(target=run_health_server, daemon=True, name="health-server").start()
    app = (Application.builder().token(settings.telegram_token)
           .post_init(post_init).post_shutdown(post_shutdown)
           .concurrent_updates(4).build())
    for command, handler in (
        ("start", start_cmd), ("help", help_cmd), ("scan", scan_cmd),
        ("analyze", analyze_cmd),
        ("status", status_cmd), ("settings", settings_cmd), ("history", history_cmd),
        ("research", research_cmd),
    ):
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(
        menu_callback, pattern=r"^(?:menu:(?:scan|analyze|status|system|history|settings|help|refresh|cache|home|strategy)|mode:(?:recommended|breakout|ema|rsi_macd)|ofmode:(?:both|cvd|wick))$"
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_coin_text))
    app.add_error_handler(telegram_error_handler)
    log.info("A&T TrendHive Crypto Sniper — signals only, no order placement")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
