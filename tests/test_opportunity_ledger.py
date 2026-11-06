from opportunity_ledger import observe, summary


def test_watch_requires_future_closed_bars_and_does_not_claim_profit():
    book, outcomes = {}, []
    first = [(1000, 101, 99, 100)]
    assert observe(book, outcomes, "TEST", "LONG", first,
                   should_watch=True, atr=2.0) is None
    assert summary(book, outcomes) == {
        "watching": 1, "measured": 0, "favorable_1atr": 0
    }
    bars = first + [
        (2000, 100.6, 99.2, 100.2),
        (3000, 102.4, 99.5, 102.1),
        (4000, 103.0, 101.0, 102.0),
    ]
    assert observe(book, outcomes, "TEST", "LONG", bars) is None
    assert len(outcomes) == 0
    bars.append((5000, 101.5, 100.0, 101.0))
    result = observe(book, outcomes, "TEST", "LONG", bars)
    assert result is not None
    assert result["reached_1atr"] is True
    assert summary(book, outcomes)["measured"] == 1
    assert summary(book, outcomes)["watching"] == 0


def test_watch_rejects_invalid_candles():
    book, outcomes = {}, []
    assert observe(book, outcomes, "X", "LONG", [(1000, 99, 100, 100)],
                   should_watch=True, atr=2.0) is None
    assert book == {}
    assert observe(book, outcomes, "X", "BUY", [(1000, 101, 99, 100)],
                   should_watch=True, atr=2.0) is None
    assert book == {}
