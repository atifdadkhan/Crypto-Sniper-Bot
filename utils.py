import json, os, tempfile, time
from pathlib import Path
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, type(default)) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def cooldown_active(last_ts, minutes):
    try:
        return bool(last_ts and time.time() - float(last_ts) < minutes * 60)
    except (TypeError, ValueError):
        return False


def safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default
