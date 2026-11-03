"""Load and validate historical OHLCV candles from CSV or Excel files."""
from pathlib import Path
import numpy as np
import pandas as pd

TIMEFRAME_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
ALIASES = {
    "timestamp": ("timestamp", "time", "ts", "datetime", "date", "open_time", "timestamp_ms"),
    "open": ("open", "o"), "high": ("high", "h"),
    "low": ("low", "l"), "close": ("close", "c"),
    "volume": ("volume", "vol", "basevolume", "base_volume"),
}


def normalise_ohlcv(frame: pd.DataFrame, min_candles: int = 100) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError("DATA INSUFFICIENT: empty OHLCV file")
    lookup = {str(c).strip().lower().replace(" ", "_"): c for c in frame.columns}
    rename = {}
    for target, aliases in ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                rename[lookup[alias]] = target
                break
    df = frame.rename(columns=rename).copy()
    required = ["timestamp", "open", "high", "low", "close"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"DATA INSUFFICIENT: missing OHLCV columns {missing}")
    if "volume" not in df.columns:
        df["volume"] = 0.0

    ts = df["timestamp"]
    if pd.api.types.is_numeric_dtype(ts):
        numeric_ts = pd.to_numeric(ts, errors="coerce")
        valid_ts = numeric_ts.dropna()
        if valid_ts.empty:
            raise ValueError("DATA INSUFFICIENT: no valid timestamps")
        median = float(valid_ts.median())
        unit = "ms" if median > 100_000_000_000 else "s"
        df["timestamp"] = pd.to_datetime(numeric_ts, unit=unit, errors="coerce", utc=True)
    else:
        df["timestamp"] = pd.to_datetime(ts, errors="coerce", utc=True)

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=required + ["volume"]).sort_values("timestamp")
    df = df.drop_duplicates("timestamp", keep="last")
    valid = np.isfinite(df[["open", "high", "low", "close", "volume"]]).all(axis=1)
    valid &= (df[["open", "high", "low", "close"]] > 0).all(axis=1) & (df["volume"] >= 0)
    valid &= df["high"] >= df[["open", "close", "low"]].max(axis=1)
    valid &= df["low"] <= df[["open", "close", "high"]].min(axis=1)
    df = df.loc[valid].reset_index(drop=True)
    if len(df) < min_candles:
        raise ValueError(f"DATA INSUFFICIENT: only {len(df)} valid candles; minimum is {min_candles}")
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def validate_timeframe(frame: pd.DataFrame, timeframe: str, symbol: str = "dataset",
                       allow_gaps: bool = False) -> None:
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    if len(frame) < 2:
        raise ValueError(f"DATA INSUFFICIENT: {symbol} {timeframe} needs at least 2 candles")
    stamps = pd.DatetimeIndex(frame["timestamp"])
    if stamps.tz is None:
        raise ValueError(f"DATA INSUFFICIENT: {symbol} {timeframe} timestamps must be timezone-aware")
    interval = TIMEFRAME_MS[timeframe]
    values = stamps.asi8 // 1_000_000
    misaligned = values % interval != 0
    if bool(np.any(misaligned)):
        raise ValueError(f"DATA INSUFFICIENT: {symbol} {timeframe} has candle timestamps off the UTC timeframe boundary")
    gaps = np.diff(values)
    bad = np.flatnonzero(gaps != interval)
    if len(bad):
        # Gap-tolerant mode is used only for explicitly segmented backtests.
        # It still rejects off-grid timestamps and non-integral interval gaps.
        if allow_gaps and bool(np.all(gaps[bad] % interval == 0)):
            return
        first = int(bad[0])
        missing = max(int(gaps[first] // interval) - 1, 0)
        raise ValueError(
            f"DATA INSUFFICIENT: {symbol} {timeframe} has {len(bad)} timestamp gap(s); "
            f"first gap {stamps[first]} -> {stamps[first + 1]} ({missing} candle(s) missing)"
        )


def load_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path)
    elif suffix in (".xlsx", ".xls"):
        frame = pd.read_excel(path)
    else:
        raise ValueError(f"Unsupported file: {path}")
    try:
        return normalise_ohlcv(frame)
    except Exception as exc:
        raise ValueError(f"{path}: {exc}") from exc


def load_symbol_folder(folder: Path, allow_15m_gaps: bool = False) -> dict:
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"DATA INSUFFICIENT: missing symbol folder {folder}")
    result = {}
    for tf in TIMEFRAME_MS:
        matches = sorted(
            list(folder.glob(f"*{tf}*.csv")) +
            list(folder.glob(f"*{tf}*.xlsx")) +
            list(folder.glob(f"*{tf}*.xls"))
        )
        if len(matches) != 1:
            raise ValueError(f"{folder}: expected exactly one file for {tf}, found {len(matches)}")
        result[tf] = load_file(matches[0])
        validate_timeframe(result[tf], tf, folder.name,
                           allow_gaps=(allow_15m_gaps and tf == "15m"))
    return result
