import pandas as pd
import pytest

from backtesting import download_binance
from backtesting.download_bitget import is_excluded_non_crypto_base


class FakeSpotExchange:
    rateLimit = 0
    def __init__(self, listing_days=10):
        self.step = 15 * 60
        self.end_ms = (1_800_000_000_000 // (self.step*1000)) * (self.step*1000)
        self.start_ms = self.end_ms - listing_days * 86_400_000
    def parse_timeframe(self, timeframe): return self.step
    def milliseconds(self): return self.end_ms
    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        step_ms=self.step*1000
        rows=[]
        stamp=max(self.start_ms,since)
        while stamp<self.end_ms and len(rows)<limit:
            close=100.0+(stamp-self.start_ms)/step_ms*0.001
            rows.append([stamp,close,close+1,close-1,close+0.1,1000.0])
            stamp+=step_ms
        return rows


def test_fetch_history_accepts_shorter_verified_listing(monkeypatch):
    monkeypatch.setattr(download_binance.time,"sleep",lambda *_:None)
    frame=download_binance.fetch_history(FakeSpotExchange(10),"NEW/USDT","15m",365,min_history_days=7)
    assert len(frame)>500
    assert 9.9<=frame.attrs["actual_history_days"]<=10.1
    assert frame.timestamp.is_monotonic_increasing


def test_fetch_history_rejects_history_below_declared_floor(monkeypatch):
    monkeypatch.setattr(download_binance.time,"sleep",lambda *_:None)
    with pytest.raises(RuntimeError,match="minimum is 30d"):
        download_binance.fetch_history(FakeSpotExchange(10),"NEW/USDT","15m",365,min_history_days=30)


def test_spot_universe_excludes_known_equity_and_commodity_underlyings():
    assert is_excluded_non_crypto_base("AAPL")
    assert is_excluded_non_crypto_base("XAU")
    assert not is_excluded_non_crypto_base("BTC")
    assert not is_excluded_non_crypto_base("ETH")
