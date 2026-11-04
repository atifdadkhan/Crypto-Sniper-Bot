# Crypto Sniper — 4H institutional-style trend continuation (2026-11-05)

**Deployment scope:** Crypto Sniper only. Signals-only. No automatic orders,
private Bitget keys, margin settings or Telegram bot trading commands.

## Research rationale and evidence

The implementation uses published, widely studied systematic investing ideas,
not a claim that we know proprietary trades or positions of individual funds:

- AQR, Hurst/Ooi/Pedersen, "A Century of Evidence on Trend-Following Investing"
  (2017): https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing
- Finance Research Letters (2025), "Cryptocurrency market risk-managed momentum strategies":
  https://www.sciencedirect.com/science/article/pii/S1544612325011377
- Momentum/reversal estimates are implementation-sensitive to execution costs,
  funding and volatile drawdowns; research does not imply guaranteed profit.

Actual institutional accumulation/distribution cannot be proven from public
OHLCV alone. This model labels **breakouts and trend continuation**, not
whale orders, insider positions, tape-reading or order-flow footprints.

## Two separate live scanner engines

1. **15m Trend Pullback / Breakout Retest**: unchanged \`strategy.py\`; same
   1.5R / 2.5R partial targets and configured MIN_SCORE baseline.
2. **4H Trend Engine**, evaluates two *research families* in order:
   - Relative Strength Breakout: unchanged EMA50/EMA200, ADX>=18,
     20-prior-4H breakout, 7-day outperform BTC+ETH by 3 percentage points,
     2ATR stop, single 3R target and 5-day time exit REVIEW.
   - **Slow Momentum Breakout**, only if RS did not qualify: EMA120 over EMA300,
     EMA120 directional slope over 6 bars, 36-prior-4H breakout, ADX>=16,
     relative volume >=0.65, 2.5ATR stop, 3R target, 10-day time exit REVIEW.
   - Only one 4H alert per coin and candle; strategy name displayed on Telegram.
   - The 4H prechecks share one historical candle pagination call only on
     breakout candidates. Each engine retains independent cooldown and stale
     alert TTL. 4H candidate data errors do not suppress 15m signals.

Relative Strength and Slow Momentum results were selected from an **already
examined Bitget public dataset**; the comparison below is post-hoc and not
a fresh untouched holdout or proof of live profitability.

## Exact merged event-driven exploratory historical comparison

17 non-tokenized-asset Bitget USDT perpetual markets. Completed 4H candles
known before next 4H OPEN, stop-first OHLC fills, gap-aware stops, max one
simultaneous position per symbol, Relative Strength precedence on overlap,
0.65ATR anti-chase and 30/60 4H bar max holding by setup.

| Scenario | Period | Trades | PF | Net R | Drawdown in pooled R |
|---|---|---:|---:|---:|---:|
| 0.12% cost | Mar–Jun training | 146 | 1.398 | +32.268 | -11.773 |
| 0.12% cost | Jul–Nov previously viewed | 177 | 1.597 | +58.856 | -22.505 |
| 0.12% cost | Full | 331 | 1.452 | +84.271 | -23.899 |
| 0.25% cost | Mar–Jun training | 146 | 1.344 | +28.663 | -12.680 |
| 0.25% cost | Jul–Nov previously viewed | 177 | 1.540 | +54.581 | -23.712 |
| 0.25% cost | Full | 331 | 1.398 | +76.193 | -25.095 |

This is a pooled trade-level R model, NOT an account-equity or portfolio return.
It uses a currently active market universe (survivorship bias), no point-in-time
delisting data, no historical per-symbol funding, no actual live bid/ask spreads,
no correlated-portfolio exposure constraint and no forward fill logs. Reused
July–November is not an untouched prospective holdout. Significance is unknown.
High total R and PF do not establish achievable capital profits.

**Full-market update, November 5, 2026:** The 193 MB GitHub Actions
download archive from run 41805382357 WAS eventually mounted and tested.
It contained 328 fully downloaded market folders (out of a larger discovered
universe). After excluding tokenized-equity-classified markets and known stock
proxies, 273 crypto or crypto-unverified markets were evaluated. The earlier
17-market favorable sample was NOT representative. See FULL_MARKET_RISK_GATE.md.

This is still a previously viewed period on currently listed markets, not
a clean prospective holdout, and data-derived eligibility choices are
subject to selection bias. Historical artifacts expire according to retention.

## Signal execution / manual risk controls

- 4H alert uses a fresh Bitget quote; historical entry was next 4H open.
  A live quote moved more than 0.65ATR away from the breakout close is rejected.
- The stop is 2.5ATR for Slow Momentum and 2ATR for Relative Strength.
  **Large stops (often several percent of notional) are normal for swing trades.**
- Telegram displays the strategy and stop distance. This is a price-risk
  distance, NOT recommended leverage or proof of a stop fill.
- Daily/weekly losses, market correlations and simultaneous altcoin positions
  still require manual discretion; the bot never knows your actual positions.
- Manual 10-day exit review is NOT a bot-managed auto close.
- Frequent or guaranteed alerts are not promised. Missing candle history
  or invalid quote => DATA INSUFFICIENT; no synthetic candles.

## Verification gate

Run GitHub Python Tests, public Bitget smoke, compare old 15m strategy blob SHA,
confirm Render newest commit \`live\`, then inspect /ready and Telegram alert
delivery separately. Paper monitor each strategy by name. Do not set a
9/10 accuracy claim; a quality score is not a win probability.
