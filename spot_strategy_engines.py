"""Spot-only adapters for the 12 strategy families supplied to Crypto Sniper.

The original A/B/C source is preserved in research/supplied_spot_strategy_library.py.
This adapter makes one independent engine per family, maps bearish/short cues to
EXIT_TO_USDT, and refuses to invent missing OI/CVD/funding/basis data.
No exchange orders are placed.
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

from research import supplied_spot_strategy_library as legacy


class DataInsufficient(ValueError):
    pass


STRATEGY_SPECS = {
    "donchian_oi": {"name": "Donchian Breakout + OI", "type": "directional_spot", "required": ("oi",), "min_bars": 25, "tradeable": True},
    "keltner_trend": {"name": "Keltner Channel Trend", "type": "directional_spot", "required": (), "min_bars": 25, "tradeable": True},
    "funding_arb": {"name": "Funding Rate Arbitrage Overlay", "type": "overlay", "required": ("funding_rate",), "min_bars": 25, "tradeable": False},
    "grid": {"name": "Grid Trading", "type": "directional_spot", "required": (), "min_bars": 55, "tradeable": True},
    "zscore_reversion": {"name": "Z-Score Statistical Reversion", "type": "directional_spot", "required": (), "min_bars": 55, "tradeable": True},
    "funding_meanrev": {"name": "Funding Rate Mean Reversion", "type": "overlay", "required": ("funding_rate",), "min_bars": 35, "tradeable": False},
    "cvd_short": {"name": "CVD Trend-Following Short", "type": "exit_only", "required": ("cvd", "oi"), "min_bars": 205, "tradeable": True},
    "basis_trade": {"name": "Spot/Perpetual Basis Overlay", "type": "overlay", "required": ("spot_price", "futures_price", "expiry_days"), "min_bars": 25, "tradeable": False},
    "donchian_ema_short": {"name": "Donchian Breakdown / EMA Short", "type": "exit_only", "required": (), "min_bars": 205, "tradeable": True},
    "stablecoin_lockout": {"name": "Stablecoin Lockout", "type": "filter", "required": (), "min_bars": 25, "tradeable": True},
    "funding_meanrev_chop": {"name": "Funding Mean Reversion in Chop", "type": "overlay", "required": ("funding_rate",), "min_bars": 35, "tradeable": False},
    "compression_breakout": {"name": "Volatility Compression Breakout", "type": "directional_spot", "required": ("oi",), "min_bars": 60, "tradeable": True},
}
STRATEGY_IDS = tuple(STRATEGY_SPECS)
OVERLAY_ONLY = frozenset(k for k, v in STRATEGY_SPECS.items() if v["type"] == "overlay")


def _validate(frame, strategy_id):
    if strategy_id not in STRATEGY_SPECS:
        raise ValueError(f"unknown strategy engine: {strategy_id}")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise DataInsufficient("empty OHLCV frame")
    spec = STRATEGY_SPECS[strategy_id]
    columns = ("open", "high", "low", "close", "volume", *spec["required"])
    x = frame.copy()
    for column in columns:
        if column not in x:
            raise DataInsufficient(f"missing required column: {column}")
        x[column] = pd.to_numeric(x[column], errors="coerce")
        if not np.isfinite(x[column].to_numpy(dtype=float)).all():
            raise DataInsufficient(f"invalid or missing values in {column}")
    if len(x) < spec["min_bars"]:
        raise DataInsufficient(f"requires {spec['min_bars']} bars; received {len(x)}")
    if (x[["open", "high", "low", "close"]] <= 0).any().any() or (x.volume < 0).any():
        raise DataInsufficient("invalid OHLCV values")
    if ((x.high < x[["open", "low", "close"]].max(axis=1)) |
            (x.low > x[["open", "high", "close"]].min(axis=1))).any():
        raise DataInsufficient("invalid OHLC geometry")
    if "timestamp" in x:
        ts = pd.to_datetime(x.timestamp, utc=True, errors="coerce")
        if ts.isna().any() or ts.duplicated().any() or not ts.is_monotonic_increasing:
            raise DataInsufficient("timestamps must be valid, unique and increasing")
        x["timestamp"] = ts
    if strategy_id == "basis_trade" and ((x.spot_price <= 0).any() or (x.expiry_days <= 0).any()):
        raise DataInsufficient("spot_price and expiry_days must be positive")
    return x.reset_index(drop=True)


def _spot_adapt(frame, raw, strategy_id, variant):
    spec = STRATEGY_SPECS[strategy_id]
    out = raw.copy().reset_index(drop=True)
    idx = out.index
    long_entry = out.get("entry_long", pd.Series(False, index=idx)).fillna(False).astype(bool)
    # In spot mode a short entry/breakdown is only an exit/avoid signal.
    bearish_exit = (
        out.get("exit_long", pd.Series(False, index=idx)).fillna(False).astype(bool)
        | out.get("entry_short", pd.Series(False, index=idx)).fillna(False).astype(bool)
        | out.get("exit_short", pd.Series(False, index=idx)).fillna(False).astype(bool)
    )
    positions, actions, position = [], [], 0
    for i in range(len(out)):
        if position == 0 and long_entry.iloc[i] and not bearish_exit.iloc[i]:
            position, action = 1, "BUY"
        elif bearish_exit.iloc[i]:
            position, action = 0, "EXIT_TO_USDT"
        else:
            action = "HOLD"
        positions.append(position)
        actions.append(action)
    out["strategy_id"] = strategy_id
    out["strategy_type"] = spec["type"]
    out["variant"] = variant
    out["research_position"] = out.get("position", pd.Series(0, index=idx))
    out["entry_long"] = long_entry & ~bearish_exit
    out["exit_long"] = bearish_exit
    out["position"] = pd.Series(positions, index=idx, dtype="int8")
    out["action"] = actions
    out["tradeable"] = (out["entry_long"] | out["exit_long"]).astype(bool)
    out["tradeable_for_user"] = bool(spec["tradeable"])
    return out


def _overlay(frame, strategy_id, features, variant):
    out = frame.copy()
    out["strategy_id"] = strategy_id
    out["strategy_type"] = "overlay"
    out["variant"] = variant
    out["position"] = 0
    out["action"] = "FEATURE_ONLY"
    out["tradeable"] = False
    out["tradeable_for_user"] = False
    out["signal_strength"] = 0.0
    for key, value in features.items():
        out[key] = value
    return out


def evaluate_strategy(frame, strategy_id, variant="A"):
    """Evaluate one family on closed candles; return spot BUY/EXIT/HOLD events."""
    if variant not in {"A", "B", "C"}:
        raise ValueError("variant must be A, B or C")
    df = _validate(frame, strategy_id)
    if strategy_id == "donchian_oi":
        raw = legacy.crypto_b1_donchian_oi_a(df)
    elif strategy_id == "keltner_trend":
        raw = legacy.crypto_b2_keltner_trend_a(df)
    elif strategy_id == "funding_arb":
        raw = legacy.crypto_b3_funding_arb_a(df.funding_rate)
        return _overlay(df, strategy_id, {
            "funding_rate": raw.funding_rate,
            "would_short_perp": raw.would_short_perp,
            "overlay_signal": np.where(raw.would_short_perp, "PERP_SHORT_BIAS", "NEUTRAL"),
        }, variant)
    elif strategy_id == "grid":
        raw = legacy.crypto_s1_grid_a(df)
    elif strategy_id == "zscore_reversion":
        raw = legacy.crypto_s2_zscore_a(df)
    elif strategy_id == "funding_meanrev":
        raw = legacy.crypto_s3_funding_meanrev_a(df.funding_rate)
        return _overlay(df, strategy_id, {
            "funding_rate": raw.funding_rate, "funding_z": raw.funding_z,
            "would_short_perp": raw.would_short_perp,
            "would_long_perp": raw.would_long_perp,
            "overlay_signal": np.where(raw.would_short_perp, "PERP_SHORT_BIAS",
                                       np.where(raw.would_long_perp, "PERP_LONG_BIAS", "NEUTRAL")),
        }, variant)
    elif strategy_id == "cvd_short":
        raw = legacy.crypto_r1_cvd_short_a(df)
    elif strategy_id == "basis_trade":
        basis = (df.futures_price - df.spot_price) / df.spot_price
        annualized = basis * (365 / df.expiry_days)
        return _overlay(df, strategy_id, {
            "basis": basis, "annualized_basis": annualized,
            "overlay_signal": np.where(basis > 0, "POSITIVE_BASIS", "NEGATIVE_BASIS"),
        }, variant)
    elif strategy_id == "donchian_ema_short":
        raw = legacy.crypto_r3_donchian_ema_short_a(df)
    elif strategy_id == "stablecoin_lockout":
        raw = legacy.crypto_c1_stablecoin_lockout_a(df)
    elif strategy_id == "funding_meanrev_chop":
        raw = legacy.crypto_c2_funding_meanrev_a(df.funding_rate)
        return _overlay(df, strategy_id, {
            "funding_z": raw.funding_z, "would_short_perp": raw.would_short_perp,
            "size_mult": raw.size_mult,
            "overlay_signal": np.where(raw.would_short_perp, "PERP_SHORT_BIAS", "NEUTRAL"),
        }, variant)
    elif strategy_id == "compression_breakout":
        raw = legacy.crypto_c3_compression_breakout_a(df)
    else:
        raise AssertionError(f"unhandled strategy: {strategy_id}")
    return _spot_adapt(df, raw, strategy_id, variant)


def evaluate_all(frame, variant="A"):
    results = {}
    for strategy_id in STRATEGY_IDS:
        try:
            results[strategy_id] = evaluate_strategy(frame, strategy_id, variant)
        except DataInsufficient as exc:
            results[strategy_id] = pd.DataFrame([{
                "strategy_id": strategy_id, "status": "DATA INSUFFICIENT",
                "reason": str(exc), "tradeable_for_user": STRATEGY_SPECS[strategy_id]["tradeable"],
            }])
    return results
