# A&T Institutional Quant Research Lab

## Objective
Build a reproducible research and signal-generation system for Bitget linear USDT-margined perpetual crypto contracts. This project is research and signals only. It must never submit, amend, cancel, or close exchange orders.

## Current implementation
- The existing five independent engines are preserved: trend pullback, breakout retest, momentum continuation, range reversal, and volatility expansion.
- `backtesting/download_bitget.py` discovers active linear USDT-settled swap contracts, ranks them by 24-hour USDT turnover, and can download all eligible candidates when `--max-symbols 0` is used.
- The market filter excludes known tokenised equities, indices, commodities and other non-crypto underlyings using a maintained heuristic denylist. This is not a perfect classification guarantee; the discovered market list must be audited.
- `backtesting/full_market_runner.py` applies the preserved engines across all downloaded market folders and six timeframes. 30m and 2h are derived only from complete 15m/1h buckets.
- A market/timeframe with insufficient or invalid data is recorded rather than fabricated.

## First full-universe collection
Run after the full-market workflow is available on the repository default branch:

```bash
python -m backtesting.download_bitget \
  --symbols auto --days 365 --min-history-days 120 \
  --min-quote-volume 500000 \
  --absolute-min-quote-volume 250000 \
  --max-symbols 0 --out data

python -m backtesting.full_market_runner \
  --data-dir data --fee-slippage-pct 0.12 --minimum-trades 30
```

The first collection uses 365 days as a practical baseline, not a claim that every contract has 365 days of history. Increase the lookback in later runs (up to the downloader's supported 2,000-day request) only after measuring API throughput, coverage and storage. The downloader writes a market manifest with per-market failures and actual candle coverage. A later incremental collector should append only new candles and revalidate overlaps before replacing the canonical dataset.

## Data policy
1. Discover contracts from Bitget public market metadata; require active, linear, USDT-quoted, USDT-settled perpetual swaps.
2. Exclude known non-crypto underlyings and manually review uncertain bases.
3. Preserve original exchange symbol, market ID, contract metadata, discovery time and 24h turnover.
4. Store UTC candle-open timestamps and raw OHLCV; never fill missing candles with synthetic data.
5. Track first/last timestamp, row count, duplicate count, gap count, invalid OHLC rows, checksum and source endpoint.
6. Compare strategies across matched date windows when listings have different start dates.
7. Maintain a point-in-time listing universe in a later phase to reduce survivorship bias.
8. Keep historical funding in a separate timestamped dataset. Do not treat a current funding rate as historical funding.
9. Order-book, liquidation and positioning data have different history/retention limits and must be labeled with their actual coverage.

## Strategy library
See [strategy_catalog.json](strategy_catalog.json). The catalog separates engines already implemented from research candidates and strategies requiring special datasets. A catalog entry is not proof that an engine has been coded or found profitable.

Priority research families:
- CTA / time-series trend and channel breakout
- Cross-sectional momentum and relative strength
- Mean reversion, VWAP and z-score reversal
- Pairs trading and cointegration
- Funding and basis carry
- Volatility compression/expansion and volatility targeting
- Volume imbalance, CVD and liquidity sweeps
- Open-interest and crowding/sentiment filters
- Market structure and failed breakouts
- Regime-conditioned ensembles and factor models

HFT market making, latency arbitrage and options volatility arbitrage are not valid OHLCV-only experiments. They require tick/order-book/queue, latency or options-chain data respectively.

## Backtest acceptance protocol
- Use completed candles only; model entry no earlier than the next candle open.
- Simulate LONG and SHORT separately.
- Include taker/maker fee assumptions only where the execution model justifies them; include spread and slippage.
- Apply historical funding at actual settlement timestamps and side, not a constant guessed rate.
- Model stop gaps conservatively; if stop and target occur within the same OHLC candle, use the conservative stop-first assumption unless lower-timeframe data resolves ordering.
- Segment simulations at missing data; never carry a trade through unknown candles.
- Use chronological train, validation and untouched out-of-sample partitions, followed by rolling walk-forward evaluation.
- Keep parameter search inside training windows; report the number of trials and apply multiple-testing controls.
- Report net return, profit factor, expectancy, Sharpe/Sortino, max drawdown, turnover, exposure, trade count and stability by regime.
- A minimum of 30 trades is only a screening floor, not production approval. Sparse results remain DATA INSUFFICIENT.
- No strategy is promoted to Telegram signals solely on an in-sample leaderboard. Require robust untouched OOS results and paper-trading validation.

## Known limitations of this first stage
- Current universe discovery is based on today's active listings, not a historical point-in-time universe.
- The denylist is heuristic and requires review.
- Per-market available history varies; newer contracts cannot provide long backtests.
- Current full-market runner uses the five existing OHLCV engines. The remaining catalog entries are planned, not yet implemented.
- Historical funding integration and incremental durable storage are follow-up milestones.
- GitHub Actions artifacts are temporary research outputs, not permanent archival storage. For long-term retention, configure an object-storage archive or periodically export verified data snapshots.

## Milestones
1. Preserve and validate the five-engine BTC/ETH baseline.
2. Complete full-market candle discovery and initial snapshot.
3. Audit symbol classification, coverage and data quality.
4. Add historical funding collection and apply funding-aware simulation.
5. Implement independent strategy families in small reviewed batches.
6. Add chronological OOS and walk-forward evaluation, then multiple-testing controls.
7. Promote only strategies that pass OOS and paper-trading gates to signal-only Telegram integration.


## Separate spot-only strategy autopilot

The Binance Spot pipeline is documented separately in [SPOT_AUTOPILOT.md](SPOT_AUTOPILOT.md). It preserves the original Bitget perpetual scanner and five-engine research path. The supplied 12-family strategy library is preserved at [supplied_spot_strategy_library.py](supplied_spot_strategy_library.py), with spot-safe adapters in the repository root at [spot_strategy_engines.py](../spot_strategy_engines.py). Do not combine Binance Spot fills with Bitget perpetual performance. Bearish cues map to EXIT_TO_USDT; funding/basis are feature-only; missing OI/CVD/funding/basis history remains DATA INSUFFICIENT. The scheduled spot workflow does research and signal-file generation only, never exchange orders.
