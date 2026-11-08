"""Fast Telegram interaction / presentation regression tests (offline only)."""
import asyncio
import importlib
from types import SimpleNamespace

from formatting import signal_text


class FakeMessage:
    def __init__(self, text="🤖 A&T TRENDHIVE"):
        self.text = text
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        return self


class FakeQuery:
    def __init__(self, action, text="🤖 A&T TRENDHIVE"):
        self.data = "menu:" + action
        self.message = FakeMessage(text)
        self.answers = []
        self.edits = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeApplication:
    def __init__(self):
        self.tasks = []

    def create_task(self, coro, name=None):
        # Do not start network jobs in unit tests; ensure handler submits
        # them without awaiting the slow path.
        self.tasks.append(name)
        coro.close()
        return SimpleNamespace(done=lambda: False)


def module(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "1234:CI_DUMMY_NOT_REAL")
    monkeypatch.setenv("ADMIN_ID", "1234")
    app = importlib.import_module("main")
    from config import load_settings
    monkeypatch.setattr(app, "settings", load_settings())
    return app


def interaction(query, app):
    context = SimpleNamespace(user_data={}, application=app, bot=object())
    update = SimpleNamespace(
        callback_query=query, effective_user=SimpleNamespace(id=1234),
    )
    return update, context


def test_clean_home_menu_and_back_navigation(monkeypatch):
    app = module(monkeypatch)
    buttons = [b.callback_data for row in app.MAIN_MENU.inline_keyboard for b in row]
    assert buttons == [
        "menu:analyze", "menu:scan", "menu:status",
        "menu:history", "menu:strategy", "menu:settings", "menu:help",
    ]
    query = FakeQuery("analyze")
    update, ctx = interaction(query, FakeApplication())
    asyncio.run(app.menu_callback(update, ctx))
    assert len(query.answers) == 1
    assert "Send a symbol" in query.edits[0][0]
    assert ctx.user_data["awaiting_coin"] is True
    back = FakeQuery("home")
    update2 = SimpleNamespace(callback_query=back, effective_user=update.effective_user)
    asyncio.run(app.menu_callback(update2, ctx))
    assert ctx.user_data.get("awaiting_coin") is None
    assert "CRYPTO SNIPER" in back.edits[0][0]


def test_navigation_preserves_existing_trade_alert(monkeypatch):
    app = module(monkeypatch)
    trade = "A&T TRENDHIVE\nEntry: 100\nStop Loss: 95\nTP1: 108"
    query = FakeQuery("settings", trade)
    update, ctx = interaction(query, FakeApplication())
    asyncio.run(app.menu_callback(update, ctx))
    assert query.answers and not query.edits
    assert query.message.replies
    assert "SCANNER SETTINGS" in query.message.replies[0][0]


def test_admin_denied_before_any_market_operation(monkeypatch):
    app = module(monkeypatch)
    query = FakeQuery("scan")
    update, ctx = interaction(query, FakeApplication())
    update.effective_user.id = 999
    asyncio.run(app.menu_callback(update, ctx))
    assert len(query.answers) == 1
    assert query.answers[0][1]["show_alert"] is True
    assert not ctx.application.tasks


def test_scan_callback_is_nonblocking_and_immediately_acknowledged(monkeypatch):
    app = module(monkeypatch)
    monkeypatch.setattr(app, "manual_scan_task", None)
    fake_app = FakeApplication()
    query = FakeQuery("scan")
    update, ctx = interaction(query, fake_app)
    asyncio.run(app.menu_callback(update, ctx))
    assert len(query.answers) == 1
    assert len(query.message.replies) == 1
    assert fake_app.tasks == ["manual_market_scan"]


def test_refresh_callback_is_nonblocking(monkeypatch):
    app = module(monkeypatch)
    monkeypatch.setattr(app, "market_refresh_task", None)
    fake_app = FakeApplication()
    query = FakeQuery("refresh")
    update, ctx = interaction(query, fake_app)
    asyncio.run(app.menu_callback(update, ctx))
    assert query.answers and fake_app.tasks == ["manual_market_refresh"]


def test_compact_signals_keep_exact_4h_target_and_core_tp1_tp2():
    core = SimpleNamespace(
        symbol="BTC/USDT:USDT", side="LONG", score=8, setup="Pullback",
        entry=100, stop_loss=98, tp1=103, tp2=105, counter_macro=True,
    )
    out = signal_text(core)
    assert "15m Trend Pullback" in out and "8/10" in out
    assert "TP1:" in out and "TP2:" in out
    assert "Entry:" in out and "Stop Loss:" in out
    assert "Counter-macro" in out
    four = SimpleNamespace(
        symbol="BTC/USDT:USDT", side="SHORT",
        strategy_id="RS_4H", entry=100, stop_loss=105,
        tp1=85, tp2=85,
    )
    out = signal_text(four)
    assert "4H Relative Strength Breakout" in out
    assert "Target (3R)" in out and "TP1:" not in out
    assert "Unscored" in out and "not guaranteed" in out


def test_alert_html_escapes_symbol():
    signal = SimpleNamespace(
        symbol="<TEST>&", side="LONG", score=7, setup="Pullback",
        entry=100, stop_loss=98, tp1=103, tp2=105,
    )
    assert "&lt;TEST&gt;&amp;" in signal_text(signal)
