# A&T TrendHive — Full-Market Historical Data & Backtesting

This module is **signals/backtesting only**. It uses Bitget public market-data endpoints and never places orders or needs API credentials.

## Full-market mode (recommended)

GitHub Actions → **Fetch FULL Bitget Market and Backtest** → Run workflow.

- `days`: historical lookback, 30–2000 days. Longer periods require more API requests and storage.
- `min_quote_volume`: minimum 24-hour quote volume per USDT perpetual market. Default `500000` USDT filters thin/illiquid contracts. BTC and ETH are always downloaded as macro benchmarks.
- `max_symbols`: `0` means every eligible market; use a positive cap only for a smaller test run.
- The workflow discovers active linear USDT perpetual swaps from Bitget at run time. No fixed BTC/ETH-only symbol list is used.
- For every discovered market, it downloads closed 15m, 1h, 4h and 1d candles, paginating public OHLCV requests and respecting the exchange rate limit.
- A market is included in backtesting only when its four timeframe files are complete and pass validation. Failed/partial downloads are listed in `data/download_manifest.json` rather than silently treated as valid data.
- BTC and ETH datasets are used as the strategy's macro-direction filters, and all complete downloaded symbol folders (including BTC and ETH) are backtested.
- The artifact contains the data, download manifest, per-symbol report, combined trades CSV and summary JSON.

CLI examples:

```bash
# Discover and download all eligible markets (default liquidity floor)
python -m backtesting.download_bitget --symbols auto --days 365 --min-quote-volume 500000 --max-symbols 0 --out data

# Test every complete downloaded market; BTC/ETH remain macro benchmarks
python -m backtesting.backtest --data-dir data --symbols auto --fee-slippage-pct 0.12
```

To include lower-liquidity contracts, lower `--min-quote-volume`; this can dramatically increase the number of symbols, API calls, gaps and unreliable results. `--max-symbols 0` means no symbol cap. This is the currently listed eligible market universe, not a point-in-time historical listing database; delisted contracts and historical survivorship bias may therefore be absent.

## Manual CSV / Excel data

Place each symbol's files in its own folder, e.g. `data/SOLUSDT/15m.csv`, `1h.csv`, `4h.csv`, `1d.csv`. The BTCUSDT and ETHUSDT benchmark folders are also required. Each file needs timestamp, open, high, low, close and optionally volume. Excel `.xlsx` and `.xls` files are supported by the loader, but the automated folder-discovery backtest currently expects CSV names for reliable auto-discovery.

Then run the **Historical Backtest** GitHub Actions workflow with `symbols=auto`, or explicitly list symbols.

## Interpretation and limitations

- This is a historical simulation, not a profit guarantee or proof of future win rate.
- It uses the existing signal rules and closed candles; entries are modeled at the next 15m candle open.
- Historical live quote is unavailable, so the completed 15m close is used as a proxy during signal qualification.
- Stop is assumed to occur first when stop and target are both touched within one candle (conservative OHLC assumption).
- Fees and slippage are an estimate controlled by `fee_slippage_pct`; funding payments, market impact, partial fills and exact exchange fee tiers are not modeled.
- The market universe is discovered at download time. This does not reconstruct which contracts were listed at every historical date, so survivorship bias is possible.
- Check `download_manifest.json` and each symbol's candle coverage before relying on any result. Missing/gappy data should be treated as DATA INSUFFICIENT.
- Validate out-of-sample and paper-trade results before risking capital.

## Live Telegram scanner universe

The live scanner discovers active linear USDT perpetual contracts from Bitget and refreshes the liquid-pair list on each scan. Set Render `TOP_N=0` to scan every pair meeting `MIN_QUOTE_VOLUME`; a positive `TOP_N` limits scanning to the highest-volume N pairs. Default `MIN_QUOTE_VOLUME=500000` filters very thin markets. `TOP_N=0` can mean hundreds of symbols and more API requests; keep concurrency/rate limits enabled and monitor exchange throttling.
