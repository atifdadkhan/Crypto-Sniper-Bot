"""Offline regression checks for live orderflow CVD and wick engines."""
from orderflow_engines import Tape, fractional_kelly


def _fed_tape(price=100.5):
    tape = Tape("BTC/USDT:USDT")
    for i in range(110):
        assert tape.add_trade({
            "id": str(i), "timestamp": 1_780_000_000_000 + i * 2000,
            "side": "buy" if i % 2 else "sell",
            "price": price, "amount": 0.1
        }, now=1000 + i * 2)
    tape.set_book({
        "bids": [[price - .05, 12]],
        "asks": [[price + .05, 3]],
    }, now=1218)
    return tape


def test_no_missing_trade_id_or_duplicate_cvd():
    tape = _fed_tape()
    original = tape.cvd
    assert not tape.add_trade({
        "id": "109", "timestamp": 1_780_000_000_000 + 109 * 2000,
        "side": "buy", "price": 100.5, "amount": 0.2
    }, now=1220)
    assert not tape.add_trade({
        "timestamp": 1_780_000_000_000 + 220000,
        "side": "buy", "price": 100.5, "amount": 0.2
    }, now=1220)
    assert tape.cvd == original


def test_book_imbalance_must_have_fresh_valid_spread():
    tape = _fed_tape()
    assert tape.market_ok(now=1218)
    assert tape.imbalance() > 3
    assert not tape.market_ok(now=1222)  # stale book


def test_invalid_book_clears_previous_quote():
    tape = _fed_tape()
    tape.set_book({'bids': [[101, 2]], 'asks': [[100, 3]]}, now=1219)
    assert tape.book is None
    assert not tape.market_ok(now=1219)


def test_bearish_price_hh_and_cvd_ll():
    tape = _fed_tape()
    tape.buckets.clear()
    for i in range(16):
        tape.buckets.append({"t": i, "high": 100.0, "low": 99.5, "cvd": -10 - i * .05})
    for i in range(16):
        tape.buckets.append({"t": i + 16, "high": 101.1, "low": 100.2, "cvd": -22 - i * .05})
    result = tape.cvd_divergence(now=1218)
    assert result is not None
    assert result.engine_id == "CVD_DIVERGENCE"
    assert result.side == "SHORT"
    assert result.stop_loss > result.entry > result.tp2


def test_liquidity_wick_reclaim_requires_three_to_one_book():
    tape = Tape("BTC/USDT:USDT")
    tape.swing_low = 95.0
    tape.swing_high = 105.0
    tape.swing_refreshed = 1210
    for i in range(110):
        px = 94.5 if i == 80 else 96.0 if i == 109 else 100.0
        tape.add_trade({
            "id": str(i), "timestamp": 1_780_000_000_000 + i * 2000,
            "side": "sell" if i == 80 else "buy",
            "price": px, "amount": 0.1,
        }, now=1000 + 2 * i)
    tape.set_book({"bids": [[95.95, 12]], "asks": [[96.05, 3]]}, now=1218)
    result = tape.wick_hunt(now=1218)
    assert result is not None
    assert result.engine_id == "WICK_HUNT"
    assert result.side == "LONG"
    assert result.tp2 > result.entry > result.stop_loss


def test_kelly_is_never_forced_or_full_kelly():
    assert fractional_kelly(.4, 1.0) == 0
    assert 0 <= fractional_kelly(.9, 2, cap=.02) <= .02
    assert fractional_kelly(.9, 0) == 0
