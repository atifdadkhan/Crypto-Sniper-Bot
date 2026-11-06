"""Causal WATCH-opportunity audit. No trade orders, PnL or probability claims.

A WATCH is a *non-actionable* candidate. We measure only what price did in
four subsequently completed bars; same-bar high/low ordering is unknowable.
Records are bounded so Render's ephemeral state stays small.
"""
import math


def observe(book, outcomes, symbol, side, bars, should_watch=False,
            reason="Developing setup", atr=None, horizon=4):
    """Update a candidate from complete timestamp/open? bars (ms, high, low, close).

    Returns a completed observation or None. Call on every valid fetch even
    when should_watch=False to resolve an earlier WATCH record.
    """
    if side not in ("LONG", "SHORT") or not bars:
        return None
    key = f"{symbol}|{side}"
    try:
        clean = sorted(
            ((int(t), float(hi), float(lo), float(cl))
             for t, hi, lo, cl in bars),
            key=lambda x: x[0],
        )
        if any(not all(map(math.isfinite, (hi, lo, cl)))
               or hi < max(lo, cl) or lo <= 0 for _, hi, lo, cl in clean):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    previous = book.get(key)
    completed = None
    if isinstance(previous, dict):
        later = [bar for bar in clean if bar[0] > previous.get("at", 0)]
        if len(later) >= horizon:
            subset = later[:horizon]
            entry = float(previous["entry"])
            unit = float(previous["atr"])
            if unit > 0:
                up = max(bar[1] for bar in subset) - entry
                down = entry - min(bar[2] for bar in subset)
                favorable = up if side == "LONG" else down
                adverse = down if side == "LONG" else up
                completed = {
                    "symbol": symbol, "side": side, "reason": previous["reason"],
                    "at": previous["at"], "mfe_atr": round(favorable / unit, 2),
                    "mae_atr": round(adverse / unit, 2),
                    "reached_1atr": bool(favorable >= unit),
                }
                outcomes.append(completed)
                del outcomes[:-100]
            book.pop(key, None)
        elif len(later) == 0 and clean[-1][0] < previous.get("at", 0):
            book.pop(key, None)
    if should_watch and key not in book and len(book) < 200:
        try:
            unit = float(atr)
            if math.isfinite(unit) and unit > 0 and clean[-1][3] > 0:
                book[key] = {
                    "at": clean[-1][0], "entry": clean[-1][3],
                    "atr": unit, "reason": str(reason)[:80],
                }
        except (ValueError, TypeError):
            pass
    return completed


def summary(book, outcomes):
    measured = [o for o in outcomes if isinstance(o, dict)]
    return {
        "watching": len(book),
        "measured": len(measured),
        "favorable_1atr": sum(bool(o.get("reached_1atr")) for o in measured),
    }
