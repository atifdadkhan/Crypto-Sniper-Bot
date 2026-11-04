"""Crash-tolerant, expiring Telegram alert queue for a signals-only bot.

The queue is local JSON: Render free instance storage is ephemeral. The queue
avoids silent loss on transient Telegram failures within the same deployment,
not exactly-once delivery or persistence across redeploys.
"""
import time
from pathlib import Path

from utils import atomic_json, load_json


class AlertOutbox:
    def __init__(self, path, expiry_seconds=900, max_pending=200):
        self.path = Path(path)
        self.expiry_seconds = int(expiry_seconds)
        self.max_pending = int(max_pending)
        raw = load_json(self.path, [])
        self.items = [
            row for row in raw
            if isinstance(row, dict) and all(k in row for k in ("id", "text", "expires_at", "signal"))
        ][:self.max_pending]

    @staticmethod
    def signal_id(signal):
        candle = int(getattr(signal, "candle_close_ms", 0) or 0)
        if not candle:
            # A legacy signal without a candle timestamp should never be queued.
            return None
        return f"{signal.symbol}|{signal.side}|{candle}"

    def persist(self):
        atomic_json(self.path, self.items)

    def prune(self, now=None):
        now = time.time() if now is None else float(now)
        before = len(self.items)
        self.items = [row for row in self.items if float(row.get("expires_at", 0)) > now]
        if len(self.items) != before:
            self.persist()
        return before - len(self.items)

    def add(self, signal, text, now=None):
        now = time.time() if now is None else float(now)
        self.prune(now)
        item_id = self.signal_id(signal)
        if not item_id or any(row["id"] == item_id for row in self.items):
            return False
        close_at = int(signal.candle_close_ms) / 1000
        # A scan completed too late cannot queue an actionable historical entry.
        if close_at > now + 60 or now - close_at > self.expiry_seconds:
            return False
        if len(self.items) >= self.max_pending:
            return False
        self.items.append({
            "id": item_id, "text": text, "signal": signal.to_dict(),
            "expires_at": close_at + self.expiry_seconds,
            "queued_at": now, "attempts": 0,
        })
        self.persist()
        return True

    def remove(self, item_id):
        self.items = [row for row in self.items if row["id"] != item_id]
        self.persist()

    def failed(self, item_id):
        for row in self.items:
            if row["id"] == item_id:
                row["attempts"] = int(row.get("attempts", 0)) + 1
        self.persist()
