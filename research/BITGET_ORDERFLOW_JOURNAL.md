# Bitget orderflow: English alerts and candle-based PAPER journal

The live Bitget USDT perpetual CVD Divergence / Wick Hunt bot remains **signals only**, without private exchange API keys or order placement.

## Market-aware English alerts

Each delivered orderflow signal identifies its original LONG or SHORT direction, engine ID and strategy name. Alerts include the observed price, nearby **Entry Price Range**, original stop, TP1, and a closed-1H-candle market-condition label:

- **CAUTIOUS**: TP1 only, paper model closes 100% at TP1.
- **STRONG**: TP1 (50%), TP2 (30%), Moonshot (20%) with a 1R trailing stop after TP2.
- STRONG requires preceding, *already closed* Bitget 1-hour candles to align in SMA20 direction and eight-candle momentum with the trade direction. If there are too few or stale trend candles, default to CAUTIOUS.
- TP1 and TP2 remain the levels produced by the original CVD/Wick engines. The entry range is guidance around an observed trade, not a guaranteed fill.

## Permanent event history

Telegram-confirmed delivered signals are written to the existing \`history.json\` and a separate SQLite journal at:

\`<DATA_DIR>/bitget_orderflow_paper.sqlite3\`

On Windows the user's DATA_DIR currently resolves to \`C:\tmp\trendhive\`. **Do not delete or reset this directory**. Preserve the existing \`history.json\` and \`state.json\` backups; the new database does not overwrite them.

The SQLite tables are \`paper_signals\` and \`paper_events\`, each keyed to a deduplicated Bitget signal. Each signal stores up to 40 actual closed one-hour OHLCV candles available at signal creation. Transition events store the candle responsible for TP1, TP2, stop, trailing stop, time exit or validation failure. The journal tracks simulated net profit or loss after an estimated combined 0.12% round-trip fee/slippage.

The Telegram \`/history\` panel shows paper win/loss/open/unverified-gap counts alongside existing delivered-signal history. Outcome refreshes run every 15 minutes against validated 1H closed Bitget candles. The first *partly formed* signal hour is skipped because OHLCV cannot tell which price action preceded the actual signal. Missing subsequent hourly candles produce \`UNVERIFIABLE_DATA_GAP\`, never fictitious wins. Intrabar stop/TP ambiguities assume the stop came first. Outcomes are **paper estimates**, not verified exchange fills or account P&L. This system is not auto execution.

## Offline test

From the Crypto Sniper repository root with a configured Python environment:

\`\`\`powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_bitget_orderflow_journal.py tests/test_orderflow_engines.py tests/test_telegram_ui.py
\`\`\`

Deploy only after automated tests pass and the single existing local bot process can be cleanly restarted. GitHub merge is **not** proof of a Windows restart. Cloudflare and auto-start setup require no modification for this feature.
