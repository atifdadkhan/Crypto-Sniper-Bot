"""Conservative relative-strength exception to the BTC/ETH macro veto.

This is a rule-based candidate filter, NOT a trained or profitable model.
Both macro returns must be known to waive opposing macro direction.
"""


def direction_policy(side, btc_trend, eth_trend, score, asset_return_6_pct,
                     btc_return_6_pct=None, eth_return_6_pct=None,
                     adx_4h=None, min_outperformance_pp=1.0):
    """Return (allow_candidate, independent_macro_flag).

    An independently trending coin can qualify despite opposing BTC/ETH, but
    only on confirmed relative strength, good quality and a clean 4H trend.
    No fake performance estimates or future bars are used.
    """
    if side not in ("LONG", "SHORT"):
        return False, False
    opposite = "BEARISH" if side == "LONG" else "BULLISH"
    if btc_trend != opposite and eth_trend != opposite:
        return True, False
    try:
        asset = float(asset_return_6_pct)
        btc = float(btc_return_6_pct)
        eth = float(eth_return_6_pct)
        adx = float(adx_4h)
    except (TypeError, ValueError):
        return False, False

    import math
    if not all(math.isfinite(x) for x in (asset, btc, eth, adx)):
        return False, False
    if int(score) < 8 or adx < 18:
        return False, False

    if side == "LONG":
        independent = asset > 0 and asset - max(btc, eth) >= min_outperformance_pp
    else:
        independent = asset < 0 and min(btc, eth) - asset >= min_outperformance_pp
    return bool(independent), bool(independent)
