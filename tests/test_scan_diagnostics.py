"""Regression checks for safe operational diagnostics; never log raw errors."""

import pytest

from scan_diagnostics import safe_failure_reason


@pytest.mark.parametrize(("message", "reason"), [
    ("DATA INSUFFICIENT: AKE/USDT:USDT 15m has too few candles", "insufficient_history"),
    ("DATA INSUFFICIENT: AKE/USDT:USDT 15m closed candles", "insufficient_closed_history"),
    ("DATA INSUFFICIENT: missing/duplicate 15m candles; gaps_ms=[...]", "candle_sequence_gap"),
    ("STALE DATA: AKE/USDT:USDT 15m last closed candle is old", "stale_candle"),
    ("DATA INSUFFICIENT: invalid numeric OHLCV", "invalid_candle_numeric"),
    ("DATA INSUFFICIENT: invalid OHLC geometry", "invalid_candle_geometry"),
    ("STALE DATA: live quote for AKE/USDT:USDT", "stale_quote"),
    ("WIDE SPREAD: AKE/USDT:USDT spread 0.200%", "wide_spread"),
    ("UNVERIFIED QUOTE: AKE/USDT:USDT no timestamp or valid bid/ask", "unverified_quote"),
    ("DATA INSUFFICIENT: AKE/USDT:USDT 4H EMA300 warmup (200 bars)", "insufficient_4h_warmup"),
    ("DATA INSUFFICIENT: AKE/USDT:USDT 4H historical gap", "historical_4h_gap"),
])
def test_known_market_data_failures_have_stable_safe_labels(message, reason):
    assert safe_failure_reason(ValueError(message)) == reason


def test_unknown_error_does_not_leak_url_or_token():
    # An exception may accidentally include a credential-bearing request URL.
    message = "https://api.telegram.org/bot123456:TOP_SECRET/getUpdates"
    value = safe_failure_reason(ValueError(message))
    assert value == "unclassified_value_error"
    assert "TOP_SECRET" not in value and "https" not in value


def test_arbitrary_provider_exception_is_not_logged_verbatim():
    assert safe_failure_reason(RuntimeError("private_exchange_key=secret")) == "non_validation_exception"


def test_reason_labels_are_bounded_even_with_embedded_known_marker():
    message = "missing/duplicate: https://example.test/key/SECRET"
    assert safe_failure_reason(ValueError(message)) == "candle_sequence_gap"
