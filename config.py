import os
import math
from dataclasses import dataclass


def _int(name, default, minimum=None, maximum=None):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _float(name, default, minimum=None, maximum=None):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _bool(name, default=True):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name} must be true/false")
    return value in {"true", "1", "yes"}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    admin_id: int
    data_dir: str
    scan_seconds: int
    top_n: int
    min_quote_volume: float
    risk_percent: float
    max_position_notional: float
    min_score: int
    cooldown_minutes: int
    exchange_timeout_ms: int
    request_rate_limit: bool
    test_mode: bool
    concurrency: int
    scan_batch_size: int
    signal_ttl_seconds: int
    rs_engine_enabled: bool
    four_hour_top_n: int
    rs_signal_ttl_seconds: int


def load_settings():
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    admin = os.getenv("ADMIN_ID", "").strip()
    if not token:
        raise ValueError("Missing TELEGRAM_TOKEN environment variable")
    if not admin.isdigit():
        raise ValueError("ADMIN_ID must be your numeric Telegram user ID")
    return Settings(
        telegram_token=token, admin_id=int(admin),
        data_dir=os.getenv("DATA_DIR", "/tmp/trendhive"),
        scan_seconds=_int("SCAN_SECONDS", 900, 60, 86400),
        top_n=_int("TOP_N", 0, 0),
        min_quote_volume=_float("MIN_QUOTE_VOLUME", 500000, 0),
        risk_percent=_float("RISK_PERCENT", 1.0, 0.1, 5.0),
        max_position_notional=_float("MAX_POSITION_NOTIONAL_USDT", 500, 1),
        min_score=_int("MIN_SCORE", 7, 5, 10),
        cooldown_minutes=_int("COOLDOWN_MINUTES", 180, 1, 10080),
        exchange_timeout_ms=_int("EXCHANGE_TIMEOUT_MS", 20000, 5000, 120000),
        request_rate_limit=_bool("REQUEST_RATE_LIMIT", True),
        test_mode=_bool("TEST_MODE", False),
        concurrency=_int("SCAN_CONCURRENCY", 3, 1, 8),
        scan_batch_size=_int("SCAN_BATCH_SIZE", 80, 0, 1000),
        signal_ttl_seconds=_int("SIGNAL_TTL_SECONDS", 900, 120, 1800),
        rs_engine_enabled=_bool("RS_ENGINE_ENABLED", True),
        four_hour_top_n=_int("FOUR_HOUR_TOP_N", 20, 1, 100),
        rs_signal_ttl_seconds=_int("RS_SIGNAL_TTL_SECONDS", 3600, 120, 14400),
    )
