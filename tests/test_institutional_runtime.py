import time
from types import SimpleNamespace

from delivery import AlertOutbox
from macro_policy import direction_policy


def test_counter_macro_long_requires_real_relative_strength():
    assert direction_policy("LONG", "BEARISH", "BEARISH", 8,
                            5.0, -2.0, -3.0, 23) == (True, True)
    assert direction_policy("LONG", "BEARISH", "BEARISH", 7,
                            5.0, -2.0, -3.0, 23) == (False, False)
    assert direction_policy("LONG", "BEARISH", "BEARISH", 8,
                            0.1, 0.0, -2.0, 23) == (False, False)
    assert direction_policy("LONG", "BEARISH", "BEARISH", 8,
                            5.0, None, -3.0, 23) == (False, False)
    assert direction_policy("LONG", "BEARISH", "BEARISH", 8,
                            5.0, -2.0, -3.0, 12) == (False, False)


def test_counter_macro_short_and_normal_macro():
    assert direction_policy("SHORT", "BULLISH", "BULLISH", 8,
                            -6.0, 2.0, 3.0, 26) == (True, True)
    assert direction_policy("SHORT", "NEUTRAL", "BEARISH", 7,
                            -1.0, None, None, 16) == (True, False)
    assert direction_policy("LONG", "BEARISH", "BEARISH", 8,
                            float("nan"), -2.0, -3.0, 26) == (False, False)


class DummySignal:
    def __init__(self, when):
        self.symbol = "TEST/USDT:USDT"
        self.side = "LONG"
        self.candle_close_ms = int(when * 1000)

    def to_dict(self):
        return {"symbol": self.symbol, "side": self.side, "score": 8,
                "timestamp": "test", "candle_close_ms": self.candle_close_ms}


def test_outbox_persists_deduplicates_and_expires(tmp_path):
    path = tmp_path / "outbox.json"
    now = 1_800_000_000.0
    queue = AlertOutbox(path, expiry_seconds=900)
    sig = DummySignal(now - 100)
    assert queue.add(sig, "alert", now=now)
    assert not queue.add(sig, "duplicate", now=now)
    assert len(AlertOutbox(path).items) == 1
    assert queue.items[0]["expires_at"] == now + 800
    queue.failed(queue.items[0]["id"])
    assert queue.items[0]["attempts"] == 1
    assert queue.prune(now=now + 900) == 1
    assert not queue.items


def test_outbox_rejects_stale_or_fake_candle(tmp_path):
    q = AlertOutbox(tmp_path / "a.json")
    now = time.time()
    assert not q.add(DummySignal(now - 950), "stale", now=now)
    missing = DummySignal(now)
    missing.candle_close_ms = 0
    assert not q.add(missing, "missing", now=now)
