# Sniper Production Execution Plan
Scope: ONLY this Sniper repository and its corresponding Render service. No scanner or Binance projects. No historical data downloads/backtests; manual signal execution only.

## Execution queue (work in order; do not mark done without evidence)
- [x] Merge tested candle-pattern explanations and concise signal headings.
- [x] Install push/PR triggered sequential syntax -> synthetic candle -> readiness workflow.
- [ ] P0: Confirm latest main SHA has completed successful CI and exact SHA is LIVE on Render; investigate missing triggers, deploy failures and runtime logs.
- [ ] P0: Ensure all signal calculations use closed candles; add deterministic no-repaint tests for 15m, 1h, 4h, 1d.
- [ ] P0: Fix missed alerts: persist signal delivery only after Telegram send success; test retry and duplicate prevention.
- [ ] P1: Integrate independent SMC sweep, trend pullback, breakout-retest, candle confluence and volume anomaly engines with per-engine identifiers, no invented winning probabilities.
- [ ] P1: Multi-timeframe and macro trend display, valid entry range, SL, TP1, TP2; avoid late/chased entries.
- [ ] P1: Dynamic full eligible market coverage, bounded API rate limits, rotation/coverage accounting and data-gap safeguards.
- [ ] P1: Admin-only Analyze Asset, Market Overview, Signal History, Settings, Status and clean navigation smoke tests.
- [ ] P1: Synthetic tests of scoring, invalid OHLCV, missing market data, HTF/macro exception, alert delivery and fail-closed behavior.
- [ ] P2: Production health endpoint, runtime logs and deploy verification; record actual LIVE SHA and alert delivery smoke result.
- [ ] P2: Keep this file and ChatGPT Library workspace updated with checked-off evidence.

## Automated workflow behavior
GitHub Actions automatically chains validation jobs on push/PR. GitHub Actions does NOT independently design/write/repair strategy code. Development and safe fixes require an agent run or code commits. Do not auto-merge failing or unreviewed code. Do not run historical backtests.

## Progress log
2026-11-06: Existing PR merged. PSX Render latest live SHA 457b70dfbf763f6fa566cced45567636dc5f391f; Crypto latest commit a6fa1fef201957cb2af7900f7b1c36d663319a43 had manual Render deployment initiated and is awaiting verification. Hourly ChatGPT Sniper production watch is enabled.
