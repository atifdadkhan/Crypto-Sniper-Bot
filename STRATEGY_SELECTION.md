# Three 15-minute signal styles — review build

This branch is an implementation of publicly described strategy **families**, not a copy of proprietary AlgoMaya code. It is SIGNALS ONLY: no new exchange/broker credentials or automated order placement.

## Telegram experience
- **⭐ Best Match** (default): selects the first confirmed setup in this order: Breakout + Volume, EMA Trend Change, RSI + MACD.
- **🚀 Breakout + Volume**: recently broken 20-bar range, >=1.5x previous average volume, no chase beyond 1 ATR.
- **📈 Early Trend (EMA)**: EMA 12/26 upward/downward crossover within recent 8 completed candles, directional slope, no stretched entry.
- **🛡️ Extra Check (RSI + MACD)**: trend-aligned MACD histogram + bounded RSI momentum.
- Current choice is stored in bot settings. A Telegram signal displays its selected style; a setup score is NOT a winning percentage.

## Unchanged safety boundaries
- 15-minute completed candles are supplied by the existing market-data pipeline; this module does not close candles by itself.
- Existing higher timeframe trend, risk gates and signal-only restrictions remain in place.
- No success rates have been measured or guaranteed. The old backtest/report does NOT validate these new strategy rules.
- Preserve watch lists, signal history and existing user risk settings.
- Do NOT merge into production without review, CI, market-specific replay, and forward paper testing.

## Tests and acceptance
1. GitHub CI: `compileall`, candle unit tests, `tests/test_strategy_modes.py`.
2. Verify Telegram buttons, saved settings across restarts, and independent scan/mode-selection paths.
3. Validate each style on real market data with walk-forward out-of-sample tests, realistic spread/slippage/fees, no look-ahead, and no duplicate stale alerts.
4. Forward paper-test for at least 14 sessions/days before considering promotion.

## Crypto-specific notes
- These modes filter the existing **15m** Bitget USDT perpetual scanner; they do not replace the existing 1h/4h/1d alignment, live quote, spread, macro, or anti-chase logic.
- Existing experimental **4h** alerts remain separate and are not controlled by the 15m style button.
- At large market universes, rotating batches still impose scan latency. Do not call this websocket-fast or assume full 15m coverage.
- Historical derivatives funding, commissions and execution-quality testing remain a release blocker.
