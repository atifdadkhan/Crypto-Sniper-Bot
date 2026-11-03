# A&T TrendHive — Historical Data & Backtesting

This module is **signals/backtesting only**. It uses Bitget public market-data endpoints and never places, amends, cancels, or closes orders. No private exchange API credentials are required.

## Full-market download and backtest

GitHub Actions → **Fetch FULL Bitget Market and Backtest** → Run workflow.

- `days`: requested history, 30–2000 days (default 365).
- `min_quote_volume`: 24-hour quote-volume floor in USDT; default `500000`.
- `max_symbols`: `0` means all eligible markets.
- Discovers currently active linear USDT-settled perpetual markets. BTC and ETH are retained as macro benchmarks.
- Common tokenised equity, index and commodity bases are excluded by a denylist. This is a practical filter, not a complete asset-class classification service.
- Downloads closed 15m, 1h, 4h and 1d candles through Bitget historical-candles API windows (maximum 200 candles and 90 days per request), with exact timestamp pagination. The workflow defaults to 365 days.
- Each timeframe must have UTC-aligned, strictly continuous candle-open timestamps. Missing bars, malformed OHLCV, and misaligned timestamps cause the dataset to be rejected as **DATA INSUFFICIENT**.
- Newly listed markets may have less than the requested lookback. The manifest records per-timeframe first/last candle and candle count.
- A symbol is backtested only when all four timeframe files validate. Inspect `data/download_manifest.json` and `backtest_results/report.md`.

CLI:

```bash
python -m backtesting.download_bitget --symbols auto --days 365 --min-quote-volume 500000 --max-symbols 0 --out data

python -m backtesting.backtest --data-dir data --symbols auto --fee-slippage-pct 0.12 --funding-bps-per-8h 0
```

`--fee-slippage-pct` is the estimated combined round-trip fee plus slippage, expressed as a percentage of traded notional. It is converted to R using the trade's entry-to-stop risk.

`--funding-bps-per-8h` is an optional funding-cost sensitivity assumption. It is **not actual historical Bitget funding data**. Zero means funding is omitted; test several plausible positive and negative assumptions separately. The default must not be interpreted as funding-neutral proof.

## Execution assumptions

- Strategy rules use completed candles only; a signal is evaluated after the 15m candle closes.
- Entry is modeled at the next 15m candle open. Historical live quotes are unavailable, so the completed signal close is the live-price proxy during signal qualification.
- A next-bar opening price already beyond the stop invalidates the trade.
- A stop crossed by an opening gap is filled at the worse opening price, not the stop trigger.
- If stop and target are both touched in one OHLC candle, stop is assumed first.
- TP1 realizes 50% at 1.5R; the remaining 50% targets 2.5R. No break-even stop is assumed after TP1.
- Fees/slippage and funding are estimates; market impact, liquidity-dependent fills, partial fills, exact fee tiers and historical funding-rate series are not modeled.
- The simulator is a simplified OHLC model. It cannot know the true intrabar path or queue position.

## Validation and limitations

- Run deterministic unit tests with `pytest -q`. Tests validate OHLCV normalization, timestamp continuity, stop gaps and cost conversion; they do not establish strategy profitability.
- Current active-market discovery is not a point-in-time historical listing database. Delisted contracts and historical universe membership may be absent (survivorship bias).
- Common non-crypto contracts are excluded by a base-symbol denylist, which cannot guarantee perfect classification of every newly listed token.
- Do not interpret a high score as a probability of winning.
- Use out-of-sample / walk-forward periods, realistic cost and funding sensitivity, and paper trading before considering any live decision.
- Treat incomplete, stale, malformed or gappy data as **DATA INSUFFICIENT**, never as a losing or winning trade.

## Live Telegram scanner

The live scanner remains separate from this historical simulator. It provides LONG/SHORT analysis only and does not submit broker/exchange orders. Changes in this backtesting branch do not alter `main.py`, the Telegram signal logic, or Render deployment configuration.
