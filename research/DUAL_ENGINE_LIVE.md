# A&T TrendHive — Crypto Sniper Dual Signal Engines

**Signals only. No exchange orders.** This change does not affect the Crypto
Bot, PSX Bot, or PSX Sniper Bot. Crypto Sniper remains a manual-decision tool.

## Engine A — unchanged 15m Pullback / Breakout Retest

- The existing \`strategy.evaluate\` logic, score >= MIN_SCORE (default 7),
  closed 15m signals, 1H + 4H trend and 1D opposition rules stay unchanged.
- Original swing/ATR stops and 1.5R + 2.5R targets remain unchanged.
- Every message shows "15m Trend Pullback" or "15m Breakout Retest".
- 15m alert TTL remains SIGNAL_TTL_SECONDS=900 by default.

## Engine B — experimental 4H Relative Strength Breakout

- Closed and time-aligned 4H candles only. An independent 4H signal can appear
  even when BTC and ETH have an opposing direction.
- EMA50 > EMA200 and price > EMA200 for LONG (reverse for SHORT).
  EMA300 historical warmup is required, matching the research gate.
- Breakout above 20 completed prior 4H highs (or breakdown below lows).
- ADX14 >= 18; signal volume >= 0.7 times prior 20-bar average.
- 7-day return versus BOTH BTC and ETH over exactly 42 completed 4H bars:
  for LONG coin return > max(BTC, ETH) + 3 percentage points; SHORT <
  min(BTC, ETH) - 3 percentage points.
- Market quote must be fresh/spread-checked and within 0.65 ATR of the
  just-closed 4H candle. Stop is 2ATR away from the fresh quote;
  one 3R target (NO partial TP1 is claimed). Time-exit REVIEW after 5 days is
  advice, not automatic execution or position tracking.
- If the cheap breakout prefilter succeeds, page additional historical real
  4H candles. Incomplete histories, gaps, non-crypto markets and misaligned
  BTC/ETH candles fail closed.
- RS alerts use per-engine cooldown and distinct outbox IDs; the experimental
  signal cannot hide a simultaneously valid old 15m signal.
- RS_SIGNAL_TTL_SECONDS=3600 default; no stale 4H alert after one hour.
  Existing 15m alert TTL unchanged.
- Both engines use the existing rotating SCAN_BATCH_SIZE and 15m scheduler.
  The 4H check runs on each scheduled batch, but only freshly closed 4H
  breakouts may signal. If the full universe needs >1 hour to rotate, some
  RS signals may expire without notification. /status shows both counts.
- No fabricated quality score: 4H alert says "Experimental" and has no
  0–10 win-probability rating.

## Config (no new secrets required)

- RS_ENGINE_ENABLED=true (default); set false to disable Engine B.
- RS_SIGNAL_TTL_SECONDS=3600 (default; valid 120–14400).
- All other previously configured Render environment variables continue.

## Telegram

A typical older signal identifies **15m Trend Pullback** and uses its
original Entry / SL / TP1 / TP2. An experimental alert says
**4H Relative Strength Breakout**, 2ATR SL and single 3R target.
Signal history includes the exact strategy name.

## Safety, limitations and release checklist

Historical Bitget research showed encouraging Relative Strength results on a
previously inspected sample, not untouched future data. Live expectancy is
**unknown**. Both are perpetual derivatives; manual trading carries leverage,
liquidation, and funding risk. No order or private exchange API is used.

Deploy on Render ONLY after GitHub CI and Bitget public smoke pass. Verify
/health liveness, /ready scanner freshness, poller uniqueness, new 4H macro
snapshot fields, scan errors, and Telegram strategy labels.
Trade outcomes must be tracked separately for each engine in forward paper
monitoring. 4H expiry doesn't imply a position auto-exit; this bot does not
know if you executed a trade.

On Render Free, pending alert queue and history on ephemeral /tmp storage are
not durable across instance replacements. Rotate any Telegram token previously
exposed in logs using BotFather and update its existing Render env var privately.
