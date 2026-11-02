"""Load validated historical OHLCV candles from CSV or Excel files."""
from pathlib import Path
import pandas as pd

ALIASES = {
    "timestamp": ("timestamp", "time", "ts", "datetime", "date", "open_time"),
    "open": ("open", "o"), "high": ("high", "h"),
    "low": ("low", "l"), "close": ("close", "c"),
    "volume": ("volume", "vol", "basevolume", "base_volume"),
}

def normalise_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
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
        raise ValueError(f"Missing OHLCV columns: {missing}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    ts = df["timestamp"]
    if pd.api.types.is_numeric_dtype(ts):
        median = pd.to_numeric(ts, errors="coerce").dropna().median()
        unit = "ms" if median > 100_000_000_000 else "s"
        df["timestamp"] = pd.to_datetime(pd.to_numeric(ts, errors="coerce"), unit=unit, utc=True)
    else:
        df["timestamp"] = pd.to_datetime(ts, errors="coerce", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=required).sort_values("timestamp")
    df = df.drop_duplicates("timestamp", keep="last")
    valid = (df[["open", "high", "low", "close"]] > 0).all(axis=1) & (df["volume"] >= 0)
    valid &= df["high"] >= df[["open", "close", "low"]].max(axis=1)
    valid &= df["low"] <= df[["open", "close", "high"]].min(axis=1)
    df = df.loc[valid].reset_index(drop=True)
    if len(df) < 100:
        raise ValueError(f"DATA INSUFFICIENT: only {len(df)} valid candles; minimum is 100")
    return df[["timestamp", "open", "high", "low", "close", "volume"]]

def load_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(path)
    elif suffix in (".xlsx", ".xls"):
        frame = pd.read_excel(path)
    else:
        raise ValueError(f"Unsupported file: {path}")
    return normalise_ohlcv(frame)

def load_symbol_folder(folder: Path) -> dict:
    result = {}
    for tf in ("15m", "1h", "4h", "1d"):
        matches = sorted(list(folder.glob(f"*{tf}*.csv")) + list(folder.glob(f"*{tf}*.xlsx")) + list(folder.glob(f"*{tf}*.xls")))
        if len(matches) != 1:
            raise ValueError(f"{folder}: expected exactly one file for {tf}, found {len(matches)}")
        result[tf] = load_file(matches[0])
    return result
