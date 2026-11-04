# Full-market Bitget risk gate — 2026-11-05

**Status: experimental/manual signal monitoring; NO verified profitable system.**

## Why the liquidity gate was added

A prior 17-coin research screen favored combining 4H Relative Strength with
Slow Momentum. We then loaded the existing 193 MB Bitget GitHub Actions
artifact (source run 41805382357) and checked 328 downloaded market folders.
273 crypto/crypto-unverified markets remained after excluding tokenized-equity
labels and known stock proxies.

Across all 273, the signal rules were NOT profitable:

| Method, full period | Trades | PF @ 0.12% costs | Net R |
|---|---:|---:|---:|
| Relative Strength alone | 3,676 | 0.885 | -273.960 |
| Slow Momentum alone | 2,859 | 1.002 | +3.533 |
| RS-priority + Slow fallback | 4,302 | 0.945 | -154.325 |

These are 4H completed candle -> next 4H open simulations using stop-first
ambiguous OHLC fills, same-symbol no-overlap and 30/60 bar time exits. None of
these broad-market strategies is production-approved on performance grounds.

## Point-in-time *historical* liquidity check

We computed each coin's trailing **20 completed 4H-bar average close × base volume**
and ranked all available non-stock markets at each time. This uses no future
candles within the selected price sequence. It does **not** remove
survivorship/selection bias, and CCXT OHLCV volume may not equal exact USDT
turnover on every contract. The live public ticker's 24h USDT turnover rank
is a useful but **not identical** proxy for this historical rolling rank.

**Combined RS-priority + Slow fallback, only if historical liquid rank <=20:**

| Assumptions | March–June 2026 training | July–November previously viewed |
|---|---|---|
| 0.12% round trip, 0 bps funding | 144 trades, PF 1.322, +26.33R | 177 trades, PF 1.347, +35.77R |
| 0.25% round trip, 0 bps funding | PF 1.272, +22.80R | PF 1.299, +31.60R |
| 0.25% round trip, adverse 3bps/8h funding | PF 1.161, +14.11R | PF 1.207, +22.69R |

Funding here is an assumed **adverse constant** charged by holding duration,
not timestamped historical Bitget funding. At 0.25% cost with funding, PF is
below the desired 1.30 screening threshold. The rank=20 threshold was explored
*after seeing outcomes among ranks 10,20,30,40,50,80,100,150,273*, creating
multiple-testing/selection bias. The July–November period has been examined
repeatedly. Therefore **none of these figures proves live profitability**.

R is stop-risk per hypothetical position, not capital return. Correlated
portfolio risk, actual manual fill latency, spreads, fee tier, funding sign,
delistings and market impact remain unmodeled.

## Operational controls implemented

- Keep the existing 15m Pullback/Retest engine scanning the **original
  rotating all-market universe** and preserving its exact entry/exit logic.
- Limit BOTH 4H breakout families to **TOP 20 current live public Bitget
  USDT turnover markets**. This is a capital-preservation quality focus
  supported by a liquidity rationale but *not the same filter as the
  historical rank study*. No hardcoded winner coin list.
- Always include the top-20 watch on **every 15m scan**. If some top-20 coins
  are outside the current 15m batch, evaluate them as **4H-only extra jobs**.
  This prevents the broad rotating scanner from missing most of the one-hour
  post-4H-close alert window.
- 4H signals prioritize Relative Strength when both qualify, otherwise
  Slow Momentum. One 4H trade idea per coin and completed 4H candle.
- Original 15m score and risk, 4H 2/2.5ATR stops and single 3R targets,
  no-chase, quote freshness, missing-candle rejection, separate cooldowns and
  Telegram engine names are retained.
- Alerts are **suggestions only**. User executes every trade manually.
  No brokerage orders or private keys.
- New config: FOUR_HOUR_TOP_N=20 by default. RS_ENGINE_ENABLED=true retains
  the parent 4H engine switch. The 4H top-N count is visible in /status.

## What this does not claim

- No "9 out of 10 winning trades", daily guaranteed profit or institutional
  certification. Top traders' proprietary accumulation/order flow cannot
  be inferred from OHLCV or public price alone.
- Wider full-market research actually falsified the claim that the previously
  tested combination was robust across all coins.
- These historical conditions have not yet been validated on an untouched
  prospective interval or verified as actual Telegram fills.

Monitoring should record *per strategy* signals and realized hypothetical
outcomes for at least several weeks, with exposure/correlation caps and
conservative cost assumptions before financial commitment.
