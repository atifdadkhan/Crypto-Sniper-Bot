# Unified Bitget Live Strategy Service

## What is live in the same bot

One Windows \`main.py\` Telegram process runs **two different Bitget public-data feeds concurrently**, not two bots:

- **Real-time websocket**: \`CVD_DIVERGENCE\` and \`WICK_HUNT\` (L2/trades for up to \`ORDERFLOW_TOP_N\` liquid Bitget USDT perpetual symbols; default 8). The Telegram orderflow mode \`both\` enables both.
- **Scheduled closed-candle research signals**: \`CORE_15M\` (15-minute pullback, breakout and selected confirmed setup), \`RS_4H\` and \`SLOW_4H\` (4-hour relative strength and slow momentum). The existing 15m scanner rotates eligible liquid markets; the 4H scanner checks the top \`FOUR_HOUR_TOP_N\` each scan. The pre-existing 4H policy picks at most one RS/Slow signal per symbol/candle, not both simultaneously.

They keep their **original, independent eligibility/confirmations**. A strategy may generate no signals for long periods; activation is not a profitability claim. If \`RS_ENGINE_ENABLED=false\`, the 4H pair is deliberately gated; otherwise it is enabled.

\`CRYPTO_UNIFIED_LIVE=true\` is the default on the new version. Setting \`CRYPTO_UNIFIED_LIVE=false\` reverts to the previous separation: \`CRYPTO_ORDERFLOW_ONLY=true\` (default) runs only websocket engines; \`false\` runs only the scheduled closed-candle engines. This rollback toggle avoids changing existing service setup or Telegram credentials.

## Central presentation, delivery, tracking

The single central registry in \`central_bitget_strategies.py\` lists the Bitget live IDs and keeps the **12 Binance Spot research strategies out of the Bitget Futures live path**. Binance Spot research uses a different market type, SELL/EXIT semantics, missing historical OI/funding/CVD/basis data for some strategies, and cannot safely be relabeled or auto-enabled as Bitget Futures LONG/SHORT signals.

All Bitget live engines share the same English alert plan: strategy, LONG/SHORT, observed entry and Entry Price Range, original stop, TP1, TP2/Moonshot only if preceding CLOSED Bitget 1h trend is aligned and **STRONG**. In CAUTIOUS conditions, the paper model uses TP1 only. Where an original 4H strategy supplies a single target, that remains the single TP1 exit; no extra target is invented.

All confirmed delivered alerts enter the existing \`<DATA_DIR>/history.json\` and **one SQLite paper ledger**, \`<DATA_DIR>/bitget_orderflow_paper.sqlite3\`. The 15m/4H scheduled scanner retains the existing expiring \`telegram_outbox.json\` and de-duplication; the websocket engines deliver directly to prevent delayed stale tape alerts. Both paths converge on \`_confirmed_signal_history\` **after Telegram acknowledges sending**, with per-engine cooldowns. The paper ledger stores 40 closed 1h candles (when available) and subsequent decisive OHLCV event snapshots, marks unverified data gaps rather than fictional profits, and uses a conservative stop-first paper model with estimated friction. Outcomes are checked every 15 minutes from Bitget closed candles and summarized in the Telegram history panel.

**NOT real trading:** No exchange orders, private exchange credentials, authenticated trading, real fills or verified account P&L. Live means real-time signal research and Telegram delivery, not automated execution.

## Health and Windows upgrade

\`GET /ready\` reports \`orderflow\`, \`central_strategies\`, \`candle_scanner_fresh\`, \`candle_scan\`, and \`candle_scan_error\` in unified mode. Websocket data freshness alone is not proof of candle scanner activity; check both. The candle scanner starts shortly after startup and recurs at the configured 15m cadence.

Do not kill both Python projects. The existing Windows Crypto and PSX bots use distinct Python PIDs, and a \`git pull\` does **not** replace a running Python process. Fetch/merge and test in a separate setup window, then stop only the Crypto process pair and restart **one** Crypto instance. Keep Cloudflare and PSX untouched. Back up \`C:\tmp\trendhive\history.json\` and \`state.json\` and preserve the new SQLite database. On startup use one Crypto bot process to avoid Telegram polling conflicts.

Offline regression command:

\`\`\`powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_central_bitget_live.py tests/test_bitget_orderflow_journal.py tests/test_orderflow_engines.py tests/test_telegram_ui.py
\`\`\`

\`/scan\` triggers a nonblocking Bitget closed-candle scan, and \`/history\` includes paper outcome counts. The Telegram strategy menu allows the original orderflow selections plus 15m style selections. None of these change the exchange market type.
