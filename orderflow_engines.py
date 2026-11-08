"""Independent public-tape CVD and liquidity-wick engines; NO technical indicators.

Only market-data signals. Never place orders: free public CCXT Pro has no trader
credentials, and an IOC limit order cannot promise zero slippage or stop fill.
Every event uses venue trade IDs to avoid double counting reconnect snapshots.
"""
from collections import deque
from dataclasses import dataclass
import math
import time

@dataclass(frozen=True)
class OrderflowSignal:
    symbol: str
    engine_id: str
    strategy_name: str
    side: str
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    timestamp: int
    score: int
    reason: str


class Tape:
    def __init__(self, symbol, horizon=480):
        self.symbol = symbol
        self.horizon = horizon
        self.trades = deque()
        self.seen = set()
        self.seen_order = deque()
        self.buckets = deque(maxlen=96)
        self.last_bucket = None
        self.cvd = 0.0
        self.book = None
        self.book_received = 0.0
        self.last_trade_at = 0.0
        self.last_trade_ts = 0
        self.swing_high = None
        self.swing_low = None
        self.swing_refreshed = 0.0
        self.sweep_above = False
        self.sweep_below = False
        self.last_alert = {}

    def reset_flow(self):
        """Discard incomplete CVD after a socket gap; warm up before signals."""
        self.trades.clear()
        self.seen.clear()
        self.seen_order.clear()
        self.buckets.clear()
        self.last_bucket = None
        self.cvd = 0.0
        self.last_trade_at = 0.0
        self.last_trade_ts = 0
        self.sweep_above = self.sweep_below = False
        self.last_alert.clear()

    def add_trade(self, trade, now=None):
        now = time.monotonic() if now is None else now
        identifier = trade.get("id")
        if identifier is None:
            # If the exchange has no trade ID, do not risk double counting.
            return False
        key = str(identifier)
        if key in self.seen:
            return False
        side = str(trade.get("side", "")).lower()
        if side not in ("buy", "sell"):
            return False
        try:
            price, volume = float(trade["price"]), float(trade["amount"])
            stamp = int(trade["timestamp"])
        except (ValueError, TypeError, KeyError):
            return False
        if not (math.isfinite(price) and math.isfinite(volume) and price > 0 and volume > 0):
            return False
        if self.last_trade_ts and stamp < self.last_trade_ts - 1000:
            return False
        if self.last_trade_ts and stamp - self.last_trade_ts > 60_000:
            # Disconnected feeds have an unknown missing signed volume.
            self.reset_flow()
        self.last_trade_ts = max(stamp, self.last_trade_ts)
        self.seen.add(key)
        self.seen_order.append(key)
        if len(self.seen_order) > 10000:
            self.seen.remove(self.seen_order.popleft())
        self.last_trade_at = now
        signed = volume if side == "buy" else -volume
        self.cvd += signed
        self.trades.append((now, price, volume, signed))
        while self.trades and self.trades[0][0] < now - self.horizon:
            self.trades.popleft()
        bucket_start = stamp // 15000
        if self.last_bucket is None or self.last_bucket["t"] != bucket_start:
            self.last_bucket = {"t": bucket_start, "high": price, "low": price,
                                "cvd": self.cvd, "delta": signed}
            self.buckets.append(self.last_bucket)
        else:
            b = self.last_bucket
            b["high"] = max(b["high"], price)
            b["low"] = min(b["low"], price)
            b["cvd"] = self.cvd
            b["delta"] += signed
        if self.swing_high and price > self.swing_high:
            self.sweep_above = True
        if self.swing_low and price < self.swing_low:
            self.sweep_below = True
        return True

    def set_book(self, book, now=None):
        now = time.monotonic() if now is None else now
        try:
            bid = [(float(p), float(q)) for p, q in book["bids"][:5]]
            ask = [(float(p), float(q)) for p, q in book["asks"][:5]]
            if not bid or not ask or bid[0][0] >= ask[0][0]:
                return
            if any(p <= 0 or q <= 0 or not math.isfinite(p * q) for p, q in bid + ask):
                return
            self.book = (bid, ask)
            self.book_received = now
        except (ValueError, TypeError, KeyError, IndexError):
            return

    def set_swings(self, bars, now=None):
        """Use completed 1-hour candles only; exclude most recent two swing bars."""
        now = time.monotonic() if now is None else now
        if len(bars) < 12:
            return
        high = max(float(x) for x in bars[-12:-2]["high"])
        low = min(float(x) for x in bars[-12:-2]["low"])
        if math.isfinite(high) and math.isfinite(low) and low > 0 and high > low:
            self.swing_high, self.swing_low = high, low
            self.swing_refreshed = now
            self.sweep_above = self.sweep_below = False

    def market_ok(self, now=None):
        now = time.monotonic() if now is None else now
        if not self.book or now - self.book_received > 3 or now - self.last_trade_at > 3:
            return False
        if not self.trades or now - self.trades[0][0] < 90:
            return False
        bids, asks = self.book
        mid = (bids[0][0] + asks[0][0]) / 2
        return 0 < (asks[0][0] - bids[0][0]) / mid <= 0.0015

    def imbalance(self):
        if not self.book:
            return 0.0
        bids, asks = self.book
        b = sum(price * qty for price, qty in bids)
        a = sum(price * qty for price, qty in asks)
        return b / a if a > 0 else 0.0

    def vwap(self):
        if not self.trades:
            return None
        total = sum(v for _, _, v, _ in self.trades)
        return sum(p * v for _, p, v, _ in self.trades) / total if total > 0 else None

    def _make(self, name, engine, side, stop, target, reason, now=None):
        now = time.monotonic() if now is None else now
        if not self.market_ok(now):
            return None
        entry = self.trades[-1][1]
        if not (math.isfinite(stop) and math.isfinite(target)):
            return None
        if (side == "LONG" and not (stop < entry < target)) or (
                side == "SHORT" and not (target < entry < stop)):
            return None
        distance = abs(entry - stop)
        if distance / entry < .0008 or distance / entry > .018:
            return None
        if abs(target - entry) < distance:
            return None
        if now - self.last_alert.get(engine + side, -1e9) < 1800:
            return None
        self.last_alert[engine + side] = now
        return OrderflowSignal(
            self.symbol, engine, name, side, entry, stop,
            entry + (distance if side == "LONG" else -distance),
            target, self.last_trade_ts, 8, reason)

    def cvd_divergence(self, now=None):
        """Price HH with CVD LL / price LL with CVD HH on two separated windows.

        Use cum delta anchored at observed trade-stream start; require >=32
        nonempty 15-second buckets (>=8 min) to reject cold-start guesses.
        """
        now = time.monotonic() if now is None else now
        if not self.market_ok(now) or len(self.buckets) < 32:
            return None
        seq = list(self.buckets)[-32:]
        old, recent = seq[:16], seq[16:]
        high1, high2 = max(x["high"] for x in old), max(x["high"] for x in recent)
        low1, low2 = min(x["low"] for x in old), min(x["low"] for x in recent)
        cvd1 = min(x["cvd"] for x in old)
        cvd2 = min(x["cvd"] for x in recent)
        peak1 = max(x["cvd"] for x in old)
        peak2 = max(x["cvd"] for x in recent)
        current = self.trades[-1][1]
        volatility = max(high2 - low2, current * .001)
        if high2 > high1 * 1.0004 and cvd2 < cvd1 - 0.01 and current < high2 * .9996:
            return self._make("CVD Divergence / Bearish Absorption", "CVD_DIVERGENCE",
                              "SHORT", high2 + .2 * volatility,
                              current - 2 * (high2 + .2 * volatility - current),
                              "Price HH with CVD LL; rejection after absorption", now)
        if low2 < low1 * .9996 and peak2 > peak1 + .01 and current > low2 * 1.0004:
            return self._make("CVD Divergence / Bullish Absorption", "CVD_DIVERGENCE",
                              "LONG", low2 - .2 * volatility,
                              current + 2 * (current - (low2 - .2 * volatility)),
                              "Price LL with CVD HH; reclaim after absorption", now)
        return None

    def wick_hunt(self, now=None):
        now = time.monotonic() if now is None else now
        if not self.market_ok(now) or now - self.swing_refreshed > 4200:
            return None
        entry = self.trades[-1][1]
        vwap = self.vwap()
        if vwap is None or len(self.trades) < 90:
            return None
        im = self.imbalance()
        if self.sweep_below and self.swing_low and entry > self.swing_low * 1.0002 and im > 3.0:
            wick_low = min(t[1] for t in self.trades)
            return self._make("Liquidity Wick Hunt / Buy-Side Reclaim", "WICK_HUNT",
                              "LONG", wick_low * .9995, vwap,
                              f"1H sell-side sweep reclaimed; bid/ask notional={im:.2f}", now)
        if self.sweep_above and self.swing_high and entry < self.swing_high * .9998 and im > 0 and (1 / im) > 3.0:
            wick_high = max(t[1] for t in self.trades)
            return self._make("Liquidity Wick Hunt / Sell-Side Rejection", "WICK_HUNT",
                              "SHORT", wick_high * 1.0005, vwap,
                              f"1H buy-side sweep rejected; ask/bid notional={1 / im:.2f}", now)
        return None


def fractional_kelly(win_rate, win_loss_ratio, cap=0.02):
    """Risk fraction suggestion only, never a trade executor or bet size."""
    p, b = float(win_rate), float(win_loss_ratio)
    if not (0 <= p <= 1 and math.isfinite(b) and b > 0):
        return 0.0
    raw = (b * p - (1 - p)) / b
    return max(0.0, min(float(cap), raw * 0.25))
