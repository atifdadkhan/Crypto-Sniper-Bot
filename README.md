# A&T TrendHive Crypto Sniper

Telegram market scanner for **Bitget USDT-margined perpetual swap markets**. It publishes LONG/SHORT analysis only. It never submits, amends, cancels, or closes orders and does not use private exchange API keys.

## Signal approach
- 15m entry setup with 1h/4h trend alignment and 1d context.
- Pullback/reclaim or a confirmed breakout retest; the first breakout impulse is not treated as a retest.
- Completed candles only; stale, gapped or insufficient data is rejected.
- Avoids stretched entries using price extension, recent range position, momentum, candle size and trend efficiency.
- Requires a fresh live quote, rejects spreads wider than 0.15%, and cancels entries if price moves more than 0.22 ATR from the setup candle close.
- Uses the current live quote as the displayed entry and recalculates Stop Loss/targets from that price.
- Stop reference uses recent swing structure with a volatility buffer; TP1=1.5R and TP2=2.5R.
- Quality score is an explainable checklist score, not a win probability or guaranteed outcome. Default minimum is 7/10.
- Top/bottom protection rejects LONGs in the top 6% of the recent range and SHORTs in the bottom 6%, unless a confirmed retest is present.

## Telegram commands
`/start`, `/help`, `/scan`, `/status`, `/settings`, `/history`

## Render deployment
- Build: `pip install -r requirements.txt`
- Start: `python main.py`
- Health path: `/health`
- Required environment variables: `TELEGRAM_TOKEN`, `ADMIN_ID`
- Suggested: `DATA_DIR=/tmp/trendhive`, `TOP_N=0` (scan all eligible active USDT perpetual pairs; set a positive number to cap the universe), `MIN_QUOTE_VOLUME=500000`, `MIN_SCORE=7`, `SCAN_SECONDS=900`, `SCAN_CONCURRENCY=3`, `COOLDOWN_MINUTES=180`.

## Safety and limitations
- Signals only. No automatic trading, order placement, or exchange private keys.
- Markets are perpetual swaps (derivatives); they can involve leverage and liquidation risk. This scanner does not set leverage or place orders.
- No strategy can guarantee 100% accuracy or eliminate losses. Backtest with realistic fees/slippage and paper trade before risking capital.
- Render free storage is ephemeral; state/history may reset after restart or redeploy.

## Tests and validation
Run `pytest -q`. The included deterministic unit suite checks candle validation, trend calculation, anti-chase behavior, first-impulse rejection, top-of-range rejection, live-quote entry pricing, and short Telegram formatting.

The tests are code checks, not a historical profitability study. Before using signals with real capital, run a walk-forward backtest with Bitget historical candles, fees, funding, spread and slippage, then paper trade. Do not interpret the 7–10 quality score as a probability of winning.

## Historical Backtesting
See [BACKTESTING_GUIDE.md](BACKTESTING_GUIDE.md) for automatic Bitget public candle downloads, manual CSV/Excel uploads, and the GitHub Actions **Historical Backtest** workflow.
## Full-market historical backtesting

See [BACKTESTING_GUIDE.md](BACKTESTING_GUIDE.md). GitHub Actions can dynamically discover eligible Bitget USDT perpetual markets, download their public OHLCV history, and backtest all complete datasets. BTC/ETH are macro filters, not the only tested coins.


## Institutional Quant Research Lab

The repository now includes a preserved strategy catalog and a full-market research runner:
- [Research protocol](research/RESEARCH_LAB.md)
- [Strategy catalog](research/strategy_catalog.json)
- `python -m backtesting.full_market_runner --data-dir data`

To run the all-market workflow after these changes are merged into `main`, open **Actions → Bitget Perpetual Research and Backtest → Run workflow**, select `all_markets`, and choose the requested history window. It discovers active eligible crypto USDT perpetuals (not spot), downloads public OHLCV, and backtests the five preserved engines. The default 365-day request is subject to each contract's actual listing history and API coverage.

The first full-market stage does not yet apply historical funding to simulated trades. It explicitly reports that limitation; funding-aware carry engines remain gated until timestamped historical funding data is collected. GitHub Actions artifacts are temporary (90-day retention), not permanent archival storage. Do not use an in-sample leaderboard as a live-signal approval.


## Reliability upgrade (audit branch — not yet production verified)

This is a signal-only engineering upgrade. It does not prove signal accuracy
or profitability, and it never submits exchange orders.

- Closed OHLCV data must contain at least 100 complete bars, valid numerical
  prices/volumes, and exact expected timestamps. Missing candles are not filled.
- New live-quote checks reject unverified timestamps/spreads.
- BTC/ETH opposing-trend exceptions require 4H independent coin strength:
  score >= 8, 4H ADX >=18, direction-aligned positive/negative 6-bar return,
  and at least +1 percentage point relative performance versus both BTC and ETH.
  Missing macro returns block an exception. Such alerts carry a risk warning.
- MIN_SCORE=7 remains the default, with supported values 5-10. Lower scores
  are not equivalent to profitable setups. Research baseline remains >=7.
- Completed coin scans are delivered immediately instead of waiting for an
  entire market universe to finish. Messages are queued and retried if Telegram
  is temporarily unavailable. Each alert expires 900 seconds after its
  completed 15m candle by default.
- A rotating all-market scan processes 80 symbols per pass by default, trading
  comprehensive coverage for fresher alerts on Render Free. To scan every
  symbol in a single pass, set SCAN_BATCH_SIZE=0; this may take too long.
  At 320 eligible markets, 80 symbols every 15 minutes means approximately
  1 hour per full rotation, not 15-minute coverage of every coin.
- New environment variables: SCAN_BATCH_SIZE=80 and
  SIGNAL_TTL_SECONDS=900. Existing Telegram token and admin ID settings
  are reused without copying credentials into source.
- The Telegram menu includes Refresh Markets. Bot Status shows rotating
  scan coverage and pending alerts.
- HTTPX/HTTPCore INFO request logging is suppressed to reduce risk of
  exposing Telegram API URLs with tokens.
- /health is a liveness endpoint. /ready returns HTTP 503 on stale or
  failing scanner readiness.
- Render Free local storage is ephemeral. Pending messages may be lost
  across redeploys, and at-least-once/exactly-once delivery is NOT guaranteed.

### Validation gate

Before production deployment: pass GitHub CI, verify the live Bitget candle
timestamps and ticker fields, test real Telegram message sending/retry,
verify Render readiness and polling uniqueness. Then forward paper-test
at least 14 days and evaluate realistic spread, fees, slippage, funding,
out-of-sample expectancy, profit factor, and drawdown. Research results
are not live strategy approval.
