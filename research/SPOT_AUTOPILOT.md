# Crypto Sniper — Spot Strategy Autopilot

## Scope and separation

This is a separate **Binance Spot research and signal pipeline** inside Crypto Sniper. It does not replace the existing Bitget perpetual signal service, its current 15m/4H engines, or the live CVD/Wick-Hunt order-flow service. The two market scopes must not be mixed in a backtest or in a signal label.

The supplied 314-line strategy library is preserved at `research/supplied_spot_strategy_library.py`. The adapter in `spot_strategy_engines.py` exposes 12 independent strategy IDs. The source file's A/B/C options are adapter/output variants of 12 families, not 36 separate engines.

## Twelve independent engines

| ID | Strategy | Type | Spot behaviour |
|---|---|---|---|
| `donchian_oi` | Donchian Breakout + OI | Directional | BUY breakout; bearish breakdown exits to USDT |
| `keltner_trend` | Keltner Channel Trend | Directional | BUY trend breakout; exit below channel mid |
| `funding_arb` | Funding Rate Arbitrage | Overlay | Feature only; never a spot order |
| `grid` | Grid Trading | Directional | BUY lower grid zone; exit upper zone |
| `zscore_reversion` | Z-Score Mean Reversion | Directional | BUY negative z-score; exit at mean |
| `funding_meanrev` | Funding Mean Reversion | Overlay | Feature only; never a spot order |
| `cvd_short` | CVD Trend-Following Short | Exit-only | Bearish condition exits to USDT; no standalone short backtest |
| `basis_trade` | Spot/Perpetual Basis | Overlay | Feature only; no futures leg |
| `donchian_ema_short` | Donchian Breakdown / EMA Short | Exit-only | Bearish condition exits to USDT; no standalone short backtest |
| `stablecoin_lockout` | Stablecoin Lockout | Filter | Blocks entries and emits an exit-to-USDT cue in chop |
| `funding_meanrev_chop` | Funding Mean Reversion in Chop | Overlay | Feature only; never a spot order |
| `compression_breakout` | Volatility Compression Breakout | Directional | BUY upside break; downside break exits to USDT |

All bearish/short signals are mapped to `EXIT_TO_USDT`; the spot adapter never returns a short position. Funding and basis engines remain feature-only. OI/CVD/funding/basis are not invented when absent.

## Data requirements and honest coverage

The existing Binance downloader now supports a requested lookback plus a separate minimum actual-history floor. Newly listed coins can be retained with shorter history, while each timeframe is independently validated and gaps remain visible.

Current Binance Spot OHLCV download supplies open, high, low, close and volume only. Therefore:
- `keltner_trend`, `grid`, `zscore_reversion`, `donchian_ema_short` and `stablecoin_lockout` can run on OHLCV.
- `donchian_oi` and `compression_breakout` require aligned open-interest history.
- `cvd_short` requires aligned CVD and open-interest history.
- Funding and basis overlays require real timestamped funding/basis inputs.

Until those feature histories are attached to the same coin/timeframe/candle timestamps, affected engines report **DATA INSUFFICIENT**. Current public spot OHLCV is not a substitute for historical funding, CVD or open interest.

The discovery filter uses active Binance Spot USDT markets, a configurable 24h USDT turnover floor, and the Crypto Sniper non-crypto-underlying denylist. This is the current eligible universe, not a claim to include illiquid, delisted or point-in-time historical listings.

## Walk-forward backtest policy

`backtesting/spot_strategy_runner.py` runs each coin and timeframe independently:
- 15m, 1h, 4h and 1d are downloaded; 30m and 2h are derived only from complete source buckets.
- Missing data splits the history into independent continuous segments.
- Each segment is split chronologically 60% in-sample / 40% out-of-sample.
- A signal on a completed candle enters no earlier than the next candle open.
- Spot BUY positions only; no short selling, leverage or futures orders.
- Research exit model: 1.5 ATR stop, 2.5R target, 24-hour maximum holding period (configurable).
- Combined round-trip fees/slippage are deducted; a same-candle stop/target ambiguity assumes stop first.
- Open trades at a split boundary or data end are censored rather than marked to a favourable close.
- Per-coin and global OOS metrics include trade count, win rate, profit factor, net R and drawdown R.

A per-coin result is not automatically transferable to another coin. A BTC result cannot qualify ETH. Global rankings are trade-level R summaries, not capital-weighted portfolio returns. A minimum of 30 OOS trades is only a screening floor; promising results still require untouched walk-forward validation and paper trading.

## Commands

Download a selected Binance Spot USDT batch (the PowerShell scheduler can dynamically discover symbols and split them into batches):

```powershell
python -m backtesting.download_binance download --market-type spot --symbols BTC/USDT,ETH/USDT --days 365 --min-history-days 7 --out data
```

Run the independent strategy backtests:

```powershell
python -m backtesting.spot_strategy_runner --data-dir data --output-dir backtest_results/spot_strategy_engines --fee-slippage-pct 0.12 --minimum-oos-trades 30
```

Generate the latest completed-candle signals for every validated coin in the local data folder:

```powershell
python -m scripts.spot_signal_scan --data-dir data --output-dir signal_results --timeframes 15m,1h,4h,1d
```

Outputs:
- `backtest_results/spot_strategy_engines/spot_strategy_summary.json`
- `backtest_results/spot_strategy_engines/per_coin_strategy_leaderboard.csv`
- `backtest_results/spot_strategy_engines/global_oos_strategy_leaderboard.csv`
- `backtest_results/spot_strategy_engines/spot_strategy_oos_trades.csv`
- `signal_results/spot_signals.json`
- `signal_results/spot_signals.csv`
- `signal_results/spot_engine_status.csv`

## Scheduled research

`.github/workflows/crypto_sniper_spot_autopilot.yml` discovers the current Binance Spot USDT universe, downloads independent symbol batches, runs all 12 engines, generates latest-candle signals, and aggregates the per-coin/global OOS reports. It is scheduled weekly and can also be started manually. GitHub artifacts are retained for 90 days; they are not a permanent historical database.

## Execution boundary

"Autopilot" here means autonomous market discovery, data collection, validation, analysis, backtesting, ranking and signal-file generation. It does **not** mean automatic buying/selling. No exchange private keys, order endpoints, order placement, short selling, leverage or futures execution are part of this pipeline.

### Source-rule audit note

The supplied C-C2 `funding_meanrev_chop` function currently thresholds funding z-score and emits a 0.25 size multiplier; it does not itself gate the signal on CHOP. The adapter exposes CHOP regime as a separate context feature and does not silently change the source rule. This overlay remains non-executable.

## English trade messages and persistent outcome history

The spot scanner generates `signal_results/spot_signals_english.txt` and includes each formatted English message under `english_message` in the signal JSON. BUY messages show the strategy, Entry Price Range, TP1, TP2 (only if STRONG), Moonshot trailing instructions (only if STRONG), Stop Loss, and a brief plan. EXIT_TO_USDT signals say SELL / EXIT, never SHORT.

`spot_signal_journal.py` stores the original 40 pre-signal OHLCV candles, each deduplicated signal and transition event, entry/exit candle snapshots, and a paper-outcome record in SQLite. Each scan rechecks new completed candles for the coin/timeframe. Trade status can progress from PENDING_ENTRY to OPEN_PAPER, TP1_PARTIAL, MOONSHOT_PAPER or CLOSED; unfilled entries can EXPIRE/CANCEL. Conservative stop-first order is used for overlapping target/stop candle ranges. Strong setups use paper fractions of 50% TP1 / 30% TP2 / 20% Moonshot, if conditions remain strong. Weak/cautious setups exit at TP1 only.

**Persistent local PowerShell command (run from the Crypto Sniper project root):**

```powershell
python -m scripts.spot_signal_scan --data-dir data --output-dir signal_results --journal-db '.\state\spot_trade_journal.sqlite3' --timeframes 15m,1h,4h,1d
```

Never delete or recreate the `state` folder between scans. The journal database is `state/spot_trade_journal.sqlite3` and a CSV snapshot is created in `signal_results/spot_trade_journal.csv`. The event history is in the SQLite `signal_events` table and contains the candle that caused each transition. Signals are keyed by symbol, timeframe, strategy, action and candle timestamp, so rerunning a scan does not duplicate a signal.

**Important limitations:** These are conservative OHLCV-based *paper signal outcomes*, not actual executed-account trades or verified P&L. Exchange fills and exact fees must be connected separately for actual P&L. The scanner creates local text/JSON/CSV files; it does not itself deliver Telegram messages or place orders. The GitHub Actions runner is ephemeral, with 90-day artifact retention, so it cannot be relied on as a permanent ledger across scheduled runs. Keep the journal on local persistent storage, or attach a permanent external database. Preserve losing trades and candle snapshots, and use chronological out-of-sample evaluation before changing any strategy.
