from backtesting.download_bitget import (
    TOKENIZED_STOCK_BASES,
    is_excluded_non_crypto_base,
    slice_market_candidates,
)


def test_candidate_batch_slicing_is_deterministic():
    markets = [(f"COIN{i}/USDT:USDT", float(100 - i), False) for i in range(55)]
    assert slice_market_candidates(markets, 0, 20) == markets[:20]
    assert slice_market_candidates(markets, 20, 20) == markets[20:40]
    assert slice_market_candidates(markets, 40, 20) == markets[40:]
    assert slice_market_candidates(markets, 55, 20) == []


def test_candidate_batch_rejects_negative_offsets():
    try:
        slice_market_candidates([], -1, 20)
    except ValueError:
        pass
    else:
        raise AssertionError("negative candidate offset must be rejected")


def test_tokenized_stocks_are_excluded_by_default_and_included_on_request():
    assert "AAOI" in TOKENIZED_STOCK_BASES
    assert is_excluded_non_crypto_base("AAOI") is True
    assert is_excluded_non_crypto_base("AAOI", include_tokenized_stocks=True) is False


def test_commodity_underlyings_remain_excluded_even_in_stock_mode():
    assert is_excluded_non_crypto_base("XAU", include_tokenized_stocks=True) is True


def test_ccxt_stock_metadata_is_respected():
    stock_market = {"stock": True}
    assert is_excluded_non_crypto_base("UNKNOWN", include_tokenized_stocks=True,
                                       market=stock_market) is False
