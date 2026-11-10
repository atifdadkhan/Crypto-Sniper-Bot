"""
CRYPTO STRATEGIES — 12 Strategies × 3 Options (A/B/C)
======================================================
Option A : Strict original
Option B : Clean directional (uniform entry/exit)
Option C : Category-organized (type-tagged, wraps A)

User: Spot-only, no leverage, no futures
→ Short signals = ML features; execution = exit to USDT
→ Funding/Basis overlays = ML features only (tradeable_for_user=False)
"""

import numpy as np
import pandas as pd

# ================================================================
# SHARED: INDICATORS + ASSEMBLE (self-contained)
# ================================================================

def _sma(s, n): return s.rolling(n).mean()
def _ema(s, n): return s.ewm(span=n, adjust=False).mean()

def _atr(df, n=14):
    h, l, c = df['high'], df['low'], df['close']; pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def _rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + g/l.replace(0, np.nan))

def _adx(df, n=14):
    up = df['high'].diff(); dn = -df['low'].diff()
    p = np.where((up > dn) & (up > 0), up, 0.0)
    m = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = _atr(df, n)
    pdi = 100 * pd.Series(p, index=df.index).ewm(alpha=1/n, adjust=False).mean() / a
    mdi = 100 * pd.Series(m, index=df.index).ewm(alpha=1/n, adjust=False).mean() / a
    return ((pdi-mdi).abs() / (pdi+mdi).replace(0, np.nan) * 100).ewm(alpha=1/n, adjust=False).mean()

def _chop(df, n=14):
    h, l, c = df['high'], df['low'], df['close']; pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    s = tr.rolling(n).sum(); hh = h.rolling(n).max(); ll = l.rolling(n).min()
    return 100 * np.log10(s / (hh-ll).replace(0, np.nan)) / np.log10(n)

def _bb(s, n=20, k=2):
    m = _sma(s, n); sd = s.rolling(n).std()
    return m-k*sd, m, m+k*sd

def _dc(df, n=20):
    up = df['high'].rolling(n).max(); lo = df['low'].rolling(n).min()
    return lo, (up+lo)/2, up

def _z(s, n=50):
    return (s - _sma(s, n)) / s.rolling(n).std().replace(0, np.nan)

def _regime(df):
    a = _adx(df, 14); ci = _chop(df, 14); c = df['close']; e = _ema(c, 200)
    reg = pd.Series('SIDE', index=df.index)
    reg[(ci > 62) | (a < 20)] = 'CHOP'
    reg[(a > 25) & (c > e) & (ci < 38)] = 'BULL'
    reg[(a > 25) & (c < e) & (ci < 38)] = 'BEAR'
    return reg

def _assemble(df, eL, xL, eS, xS, strength,
              market="CRYPTO", source="AlgoMaya", **kw):
    eL = eL.fillna(False) if eL is not None else pd.Series(False, index=df.index)
    xL = xL.fillna(False) if xL is not None else pd.Series(False, index=df.index)
    eS = eS.fillna(False) if eS is not None else pd.Series(False, index=df.index)
    xS = xS.fillna(False) if xS is not None else pd.Series(False, index=df.index)
    # Preserve the original state-machine semantics without repeated pandas iloc writes.
    eL_values, xL_values = eL.to_numpy(dtype=bool), xL.to_numpy(dtype=bool)
    eS_values, xS_values = eS.to_numpy(dtype=bool), xS.to_numpy(dtype=bool)
    positions = np.zeros(len(df), dtype=np.int8)
    state = 0
    for i in range(len(df)):
        if state == 0:
            if eL_values[i]: state = 1
            elif eS_values[i]: state = -1
        elif state == 1 and xL_values[i]:
            state = 0
        elif state == -1 and xS_values[i]:
            state = 0
        positions[i] = state
    pos = pd.Series(positions, index=df.index)
    out = df.copy()
    out['entry_long'], out['exit_long'] = eL.astype(bool), xL.astype(bool)
    out['entry_short'], out['exit_short'] = eS.astype(bool), xS.astype(bool)
    out['position'] = pos
    out['signal_strength'] = (strength.fillna(0).clip(-1, 1)
                              if strength is not None else pos.astype(float))
    out['tradeable'] = out['entry_long'] | out['exit_long']
    out['market'] = market
    out['source'] = source
    for k, v in kw.items():
        out[k] = v
    return out

# ================================================================
# OPTION A — STRICT ORIGINAL
# ================================================================

def crypto_b1_donchian_oi_a(df, n=20, vm=1.2, oit=0.02):
    """C-B1 Donchian Breakout + OI [AlgoMaya + Institutional OI]"""
    lo, _, up = _dc(df, n)
    v = df['volume'] >= vm * df['volume'].rolling(n).mean()
    oi = df['oi'].pct_change(5) > oit if 'oi' in df.columns else pd.Series(True, index=df.index)
    eL = (df['close'] > up.shift(1)) & v & oi; xL = df['close'] < lo.shift(1)
    eS = (df['close'] < lo.shift(1)) & oi; xS = df['close'] > up.shift(1)
    return _assemble(df, eL, xL, eS, xS,
                     (df['close']-up.shift(1))/(_atr(df, 14)+1e-9),
                     source="AlgoMaya")

def crypto_b2_keltner_trend_a(df, n=20, k=2):
    """C-B2 Keltner Channel Trend [AlgoMaya]"""
    mid = _ema(df['close'], n); a = _atr(df, n)
    lb = mid - k*a; ub = mid + k*a
    eL = df['close'] > ub; xL = df['close'] < mid
    eS = df['close'] < lb; xS = df['close'] > mid
    return _assemble(df, eL, xL, eS, xS,
                     (df['close']-mid)/(a+1e-9), source="AlgoMaya")

def crypto_b3_funding_arb_a(funding_series, perp_size=10000, fee_bps=4):
    """C-B3 Funding Rate Arbitrage [Institutional] — overlay, ML feature only."""
    fr = funding_series.copy()
    return pd.DataFrame({
        'funding_rate': fr,
        'would_short_perp': (fr > 0.0001),
        'expected_pnl': fr.mean() * (perp_size / 10000),
        'net_after_fee': fr.mean()*(perp_size/10000) - (fee_bps/10000)*perp_size*2,
        'tradeable_for_user': False
    })

def crypto_s1_grid_a(df, n=50, n_grids=10):
    """C-S1 Grid Trading [Institutional]"""
    hi = df['high'].rolling(n).max(); lo = df['low'].rolling(n).min()
    step = (hi-lo)/n_grids; lvl = ((df['close']-lo)/step).round()
    eL = (lvl <= 1) & (lvl > 0); xL = lvl >= n_grids-1
    eS = lvl >= n_grids-1; xS = lvl <= 1
    return _assemble(df, eL, xL, eS, xS,
                     (lvl-n_grids/2)/(n_grids/2), source="Institutional",
                     grid_level=lvl, hard_stop_low=lo-step, hard_stop_high=hi+step)

def crypto_s2_zscore_a(df, n=50, ez=2.0):
    """C-S2 Z-Score Statistical Reversion [AlgoMaya]"""
    z = _z(df['close'], n)
    eL = z < -ez; xL = z > 0
    eS = z > ez; xS = z < 0
    return _assemble(df, eL, xL, eS, xS, -z.clip(-3, 3)/3,
                     source="AlgoMaya", z=z)

def crypto_s3_funding_meanrev_a(funding_series, zw=30, ez=2.0):
    """C-S3 Funding Rate Mean Reversion [Institutional] — overlay, ML only."""
    fz = ((funding_series - funding_series.rolling(zw).mean()) /
          funding_series.rolling(zw).std().replace(0, np.nan))
    return pd.DataFrame({
        'funding_rate': funding_series,
        'funding_z': fz,
        'would_short_perp': (fz > ez),
        'would_long_perp': (fz < -ez),
        'tradeable_for_user': False
    })

def crypto_r1_cvd_short_a(df, sma_p=50):
    """C-R1 CVD Trend-Following Short [Institutional] — user: exit to USDT."""
    c = df['close']; e50 = _ema(c, sma_p); down = c < _ema(c, 200)
    near = (c-e50).abs()/c < 0.01
    cvd_neg = df['cvd'].diff(5) < 0 if 'cvd' in df.columns else pd.Series(False, index=df.index)
    oi_up = df['oi'].diff(5) > 0 if 'oi' in df.columns else pd.Series(False, index=df.index)
    eS = down & near & cvd_neg & oi_up; xS = c > _ema(c, 200)
    eL = pd.Series(False, index=df.index); xL = pd.Series(False, index=df.index)
    return _assemble(df, eL, xL, eS, xS, -down.astype(float)*0.5,
                     source="Institutional", stop=c+2*_atr(df, 14))

def crypto_r2_basis_trade_a(spot_price, fut_price, expiry_days, size_usdt):
    """C-R2 Basis Trade [Institutional] — overlay, ML only."""
    basis = (fut_price - spot_price) / spot_price
    return {
        'basis': basis,
        'annualized_basis': basis * (365/expiry_days),
        'action': 'BUY_FUT_SHORT_SPOT' if basis < 0 else 'BUY_SPOT_SHORT_FUT',
        'expected_pnl': abs(basis) * size_usdt,
        'tradeable_for_user': False
    }

def crypto_r3_donchian_ema_short_a(df, n=20):
    """C-R3 Donchian Breakdown / EMA Short [AlgoMaya] — user: exit to USDT."""
    lo, _, up = _dc(df, n); c = df['close']
    e12 = _ema(c, 12); e26 = _ema(c, 26); bl = c < _sma(c, 200)
    ema_s = (e12 < e26) & bl & (e12.shift(1) >= e26.shift(1))
    dc_s = (c < lo.shift(1)) & bl
    eS = ema_s | dc_s; xS = c > up.shift(1)
    eL = pd.Series(False, index=df.index); xL = pd.Series(False, index=df.index)
    return _assemble(df, eL, xL, eS, xS, -bl.astype(float),
                     source="AlgoMaya", stop=c+2*_atr(df, 14))

def crypto_c1_stablecoin_lockout_a(df):
    """C-C1 Stablecoin Lockout [Institutional] — boolean flag."""
    lock = (_adx(df, 14) < 20) & (_chop(df, 14) > 62)
    f = pd.Series(False, index=df.index)
    return _assemble(df, f, lock, f, lock, -lock.astype(float),
                     source="Institutional", lockout=lock)

def crypto_c2_funding_meanrev_a(funding_series, zw=30, ez=2.5):
    """C-C2 Funding Mean Reversion Chop [Institutional] — overlay, ML only."""
    fz = ((funding_series - funding_series.rolling(zw).mean()) /
          funding_series.rolling(zw).std().replace(0, np.nan))
    return pd.DataFrame({
        'funding_z': fz,
        'would_short_perp': (fz > ez),
        'size_mult': 0.25,
        'tradeable_for_user': False
    })

def crypto_c3_compression_breakout_a(df, n=20, oit=0.02):
    """C-C3 Volatility Compression Breakout [AlgoMaya derived]"""
    a = _atr(df, 14); ap = a/df['close']
    comp = ap < ap.rolling(50).quantile(0.25)
    lo, _, up = _dc(df, n)
    oi = df['oi'].pct_change(3) > oit if 'oi' in df.columns else pd.Series(True, index=df.index)
    eL = comp.shift(1) & (df['close'] > up.shift(1)) & oi
    xL = df['close'] < up.shift(1) - 1.5*a
    eS = comp.shift(1) & (df['close'] < lo.shift(1)) & oi
    xS = df['close'] > lo.shift(1) + 1.5*a
    s = np.where(df['close'] > up.shift(1), 1.0,
                 np.where(df['close'] < lo.shift(1), -1.0, 0.0))
    return _assemble(df, eL, xL, eS, xS, pd.Series(s, index=df.index),
                     source="AlgoMaya (derived)")

# ================================================================
# OPTION B — CLEAN DIRECTIONAL
# ================================================================

def crypto_b1_donchian_oi_b(df, **kw): return crypto_b1_donchian_oi_a(df, **kw)
def crypto_b2_keltner_trend_b(df, **kw): return crypto_b2_keltner_trend_a(df, **kw)

def crypto_b3_funding_arb_b(funding, **kw):
    out = crypto_b3_funding_arb_a(funding, **kw)
    out['entry_long'] = out['funding_rate'] > 0.0001
    out['exit_long'] = out['funding_rate'] < -0.0001
    out['entry_short'] = pd.Series(False, index=out.index)
    out['exit_short'] = out['exit_long']
    return out

def crypto_s1_grid_b(df, **kw): return crypto_s1_grid_a(df, **kw)
def crypto_s2_zscore_b(df, **kw): return crypto_s2_zscore_a(df, **kw)

def crypto_s3_funding_meanrev_b(funding, **kw):
    out = crypto_s3_funding_meanrev_a(funding, **kw)
    out['entry_long'] = out['would_long_perp']
    out['exit_long'] = ~out['would_long_perp']
    out['entry_short'] = out['would_short_perp']
    out['exit_short'] = ~out['would_short_perp']
    return out

def crypto_r1_cvd_short_b(df, **kw): return crypto_r1_cvd_short_a(df, **kw)

def crypto_r2_basis_trade_b(spot, fut, days, size):
    out = crypto_r2_basis_trade_a(spot, fut, days, size)
    out['entry_long'] = out['basis'] < 0
    out['exit_long'] = out['basis'] > 0
    out['entry_short'] = out['basis'] > 0
    out['exit_short'] = out['basis'] < 0
    return out

def crypto_r3_donchian_ema_short_b(df, **kw): return crypto_r3_donchian_ema_short_a(df, **kw)
def crypto_c1_stablecoin_lockout_b(df, **kw): return crypto_c1_stablecoin_lockout_a(df, **kw)

def crypto_c2_funding_meanrev_b(funding, **kw):
    out = crypto_c2_funding_meanrev_a(funding, **kw)
    out['entry_long'] = out['funding_z'] < -2.5
    out['exit_long'] = out['funding_z'] > 0
    out['entry_short'] = out['funding_z'] > 2.5
    out['exit_short'] = out['funding_z'] < 0
    return out

def crypto_c3_compression_breakout_b(df, **kw): return crypto_c3_compression_breakout_a(df, **kw)

# ================================================================
# OPTION C — CATEGORY-ORGANIZED (type-tagged)
# ================================================================

def crypto_b1_donchian_oi_c(df, **kw):
    out = crypto_b1_donchian_oi_a(df, **kw); out['strategy_type'] = 'directional_long'; return out

def crypto_b2_keltner_trend_c(df, **kw):
    out = crypto_b2_keltner_trend_a(df, **kw); out['strategy_type'] = 'directional_long'; return out

def crypto_b3_funding_arb_c(funding, **kw):
    out = crypto_b3_funding_arb_a(funding, **kw); out['strategy_type'] = 'overlay'; return out

def crypto_s1_grid_c(df, **kw):
    out = crypto_s1_grid_a(df, **kw); out['strategy_type'] = 'directional_both'; return out

def crypto_s2_zscore_c(df, **kw):
    out = crypto_s2_zscore_a(df, **kw); out['strategy_type'] = 'directional_both'; return out

def crypto_s3_funding_meanrev_c(funding, **kw):
    out = crypto_s3_funding_meanrev_a(funding, **kw); out['strategy_type'] = 'overlay'; return out

def crypto_r1_cvd_short_c(df, **kw):
    out = crypto_r1_cvd_short_a(df, **kw); out['strategy_type'] = 'directional_short'; return out

def crypto_r2_basis_trade_c(spot, fut, days, size):
    out = crypto_r2_basis_trade_a(spot, fut, days, size); out['strategy_type'] = 'overlay'; return out

def crypto_r3_donchian_ema_short_c(df, **kw):
    out = crypto_r3_donchian_ema_short_a(df, **kw); out['strategy_type'] = 'directional_short'; return out

def crypto_c1_stablecoin_lockout_c(df, **kw):
    out = crypto_c1_stablecoin_lockout_a(df, **kw); out['strategy_type'] = 'filter'; return out

def crypto_c2_funding_meanrev_c(funding, **kw):
    out = crypto_c2_funding_meanrev_a(funding, **kw); out['strategy_type'] = 'overlay'; return out

def crypto_c3_compression_breakout_c(df, **kw):
    out = crypto_c3_compression_breakout_a(df, **kw); out['strategy_type'] = 'directional_both'; return out