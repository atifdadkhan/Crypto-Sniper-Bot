"""Sanitized, fail-closed operational diagnostics for scanner errors.

Only fixed category names leave this module; exception text can include
provider URLs or credentials and must never be logged verbatim.
This module does not alter validation or signal-generation decisions.
"""

_VALUE_ERROR_MARKERS = (
    ("missing/duplicate", "candle_sequence_gap"),
    ("last closed candle is old", "stale_candle"),
    ("stale data: live quote", "stale_quote"),
    ("invalid candle timestamps", "invalid_candle_timestamp"),
    ("too few candles", "insufficient_history"),
    ("closed candles", "insufficient_closed_history"),
    ("duplicate", "duplicate_candle"),
    ("invalid numeric ohlcv", "invalid_candle_numeric"),
    ("invalid ohlcv values", "invalid_candle_value"),
    ("invalid ohlc geometry", "invalid_candle_geometry"),
    ("4h ema300 warmup", "insufficient_4h_warmup"),
    ("4h historical gap", "historical_4h_gap"),
    ("4h history shifted", "shifted_4h_history"),
    ("4h ohlc geometry", "invalid_4h_geometry"),
    ("4h history", "invalid_4h_history"),
    ("missing recent 4h bars", "missing_4h_history"),
    ("no live quote", "missing_quote"),
    ("invalid live quote", "invalid_quote"),
    ("invalid quote timestamp", "invalid_quote_timestamp"),
    ("wide spread", "wide_spread"),
    ("unverified quote", "unverified_quote"),
)


def safe_failure_reason(exc: Exception) -> str:
    """A strict finite label set. Never return raw exception messages."""
    if not isinstance(exc, ValueError):
        return "non_validation_exception"
    detail = str(exc).lower()
    for marker, code in _VALUE_ERROR_MARKERS:
        if marker in detail:
            return code
    return "unclassified_value_error"
