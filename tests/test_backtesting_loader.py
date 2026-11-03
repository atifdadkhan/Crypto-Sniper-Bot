import numpy as np
import pandas as pd
import pytest

from backtesting.data_loader import normalise_ohlcv, validate_timeframe
from backtesting.download_bitget import (
    discover_usdt_swaps, is_excluded_non_crypto_base, recover_missing_candles,
)


def _frame(n=100, start=1760000400000, step=900000):
    stamps = [start + i * step for i in range(n)]
    return pd.DataFrame({
        "timestamp": stamps, "open": [100.0] * n, "high": [102.0] * n,
        "low": [99.0] * n, "close": [101.0] * n, "volume": [10.0] * n,
    })


def test_normalise_unix_milliseconds():
    out = normalise_ohlcv(_frame())
    assert len(out) == 100
    assert str(out.timestamp.dt.tz) == "UTC"
    assert list(out.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_rejects_non_finite_and_invalid_ohlc():
    frame = _frame()
    frame.loc[0, "high"] = np.inf
    frame.loc[1, "low"] = 105.0
    out = normalise_ohlcv(frame, min_candles=1)
    assert len(out) == 98


def test_timeframe_validation_accepts_contiguous_utc_candles():
    out = normalise_ohlcv(_frame(start=1760000400000))
    validate_timeframe(out, "15m", "TESTUSDT")


def test_timeframe_validation_rejects_gap():
    frame = _frame()
    frame.loc[51:, "timestamp"] += 900000
    out = normalise_ohlcv(frame)
    with pytest.raises(ValueError, match="timestamp gap"):
        validate_timeframe(out, "15m", "TESTUSDT")


def test_timeframe_validation_rejects_misaligned_open_time():
    frame = _frame(start=1760000400001)
    out = normalise_ohlcv(frame)
    with pytest.raises(ValueError, match="off the UTC timeframe boundary"):
        validate_timeframe(out, "15m", "TESTUSDT")


def test_non_crypto_filter_blocks_known_stock_and_commodity_symbols():
    for base in ("SOXL", "SNDK", "MU", "INTC", "CL", "BZ", "AAPL", "PAXG"):
        assert is_excluded_non_crypto_base(base)
    for base in ("BTC", "ETH", "SOL", "XRP", "PEPE"):
        assert not is_excluded_non_crypto_base(base)


def test_market_discovery_excludes_non_crypto_underlyings():
    class FakeExchange:
        def load_markets(self):
            return {
                f"{base}/USDT:USDT": {
                    "active": True, "swap": True, "linear": True,
                    "quote": "USDT", "settle": "USDT", "base": base,
                }
                for base in ("BTC", "SOXL", "SNDK", "CL", "SOL")
            }

        def fetch_tickers(self):
            return {symbol: {"quoteVolume": 2_000_000} for symbol in self.load_markets()}

    symbols = [item[0] for item in discover_usdt_swaps(FakeExchange(), 500_000, 0)]
    assert "BTC/USDT:USDT" in symbols
    assert "SOL/USDT:USDT" in symbols
    assert not any(base in symbol for base in ("SOXL", "SNDK", "CL") for symbol in symbols)


def test_gap_recovery_requests_exact_missing_timestamp_and_merges_it():
    start = 1760000400000
    frame = _frame(n=3, start=start)
    frame = frame.drop(index=1).reset_index(drop=True)
    # Production download path supplies UTC-aware timestamps to the recovery helper.
    frame = normalise_ohlcv(frame, min_candles=1)

    class FakeExchange:
        rateLimit = 0
        def __init__(self):
            self.requested = []
        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            self.requested.append(since)
            return [[since, 100, 102, 99, 101, 10]]

    exchange = FakeExchange()
    out = recover_missing_candles(exchange, "BTC/USDT:USDT", "15m", frame, 900000)
    validate_timeframe(out, "15m", "BTC")
    assert exchange.requested == [start + 900000]
    assert len(out) == 3


def test_gap_recovery_uses_bitget_history_endpoint_when_ccxt_current_endpoint_misses():
    start = 1760000400000
    frame = _frame(n=3, start=start).drop(index=1).reset_index(drop=True)
    frame = normalise_ohlcv(frame, min_candles=1)

    class FakeExchange:
        rateLimit = 0
        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            return []
        def market(self, symbol):
            return {"id": "BTCUSDT"}
        def publicMixGetV2MixMarketHistoryCandles(self, params):
            assert params["symbol"] == "BTCUSDT"
            assert params["productType"] == "USDT-FUTURES"
            assert params["startTime"] == str(start + 900000)
            return {"data": [[start + 900000, 100, 102, 99, 101, 10, 1010]]}

    out = recover_missing_candles(FakeExchange(), "BTC/USDT:USDT", "15m", frame, 900000)
    validate_timeframe(out, "15m", "BTC")
    assert len(out) == 3


def test_gap_recovery_never_synthesizes_unavailable_candle():
    start = 1760000400000
    frame = _frame(n=3, start=start).drop(index=1).reset_index(drop=True)
    # Keep the fixture consistent with the UTC-aware production downloader.
    frame = normalise_ohlcv(frame, min_candles=1)

    class FakeExchange:
        rateLimit = 0
        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            return []

    out = recover_missing_candles(FakeExchange(), "BTC/USDT:USDT", "15m", frame, 900000)
    with pytest.raises(ValueError, match="timestamp gap"):
        validate_timeframe(out, "15m", "BTC")



def test_timeframe_validation_allows_integral_gaps_only_when_requested():
    frame = _frame(n=5)
    frame.loc[3:, "timestamp"] += 900000
    out = normalise_ohlcv(frame, min_candles=1)
    with pytest.raises(ValueError, match="timestamp gap"):
        validate_timeframe(out, "15m", "TESTUSDT")
    validate_timeframe(out, "15m", "TESTUSDT", allow_gaps=True)


def test_gap_recovery_retries_before_leaving_gap_unresolved():
    start = 1760000400000
    frame = _frame(n=3, start=start).drop(index=1).reset_index(drop=True)
    frame = normalise_ohlcv(frame, min_candles=1)

    class FakeExchange:
        rateLimit = 0
        def __init__(self):
            self.calls = 0
        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            self.calls += 1
            if self.calls == 2:
                return [[since, 100, 102, 99, 101, 10]]
            return []
        def market(self, symbol):
            return {"id": "BTCUSDT"}
        def publicMixGetV2MixMarketHistoryCandles(self, params):
            return {"data": []}

    exchange = FakeExchange()
    out = recover_missing_candles(exchange, "BTC/USDT:USDT", "15m",
                                  frame, 900000, max_attempts=2)
    validate_timeframe(out, "15m", "BTC")
    assert exchange.calls == 2
