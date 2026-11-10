# Telegram Signal Format and Future Research

## Telegram alert

All Bitget Futures signal engines use one HTML-formatted Telegram alert. It deliberately uses everyday BUY / LONG and SELL / SHORT language, a bold coin symbol, emoji emphasis, an entry range, TP1, conditional TP2 and Moonshot, and the stop loss last. Strategy name remains visible; internal engine IDs, candle jargon and raw indicator reasons are omitted from the user-facing alert.

Telegram does not support arbitrary font colours in ordinary messages. The visual hierarchy uses Telegram-supported bold HTML plus green/red/yellow emoji markers.

- Strong closed-hour momentum aligned with the signal direction: show TP1, TP2 and a trailing final 20% Moonshot only when the original strategy provides two distinct targets.
- Cautious or unconfirmed market support: show TP1 only; TP2 and Moonshot are OFF.
- An original single-target 4H strategy remains single-target even in a strong market. Never invent TP2 or Moonshot levels.

## What is recorded

Only after Telegram confirms delivery, the central journal records the signal, engine, strategy name, coin, side, market condition, original plan, available preceding closed 1H candles (up to 40), and a paper-entry event. Subsequent complete Bitget 1H OHLCV bars are recorded as paper outcome events, including target/stop/time exits. Signal and event data persist in \`bitget_orderflow_paper.sqlite3\`.

The journal distinguishes wins, losses, open signals and unverifiable data gaps. Gaps are not silently counted as wins or losses. Paper estimates use conservative candle sequencing and estimated friction; they are not actual fills, exchange account P&L or proof a user could have entered inside the displayed range.

## Research loop

Use Telegram \`/research\` to review:
- outcomes and average paper return by engine;
- coin, LONG/SHORT direction and recorded market condition groups;
- open and unverifiable samples separately from closed wins/losses.

Research process:
1. Compare like with like: engine × coin × direction × market condition.
2. Do not rank a strategy from a tiny sample. Twenty closed outcomes is an initial screening threshold, not statistical proof.
3. Inspect the stored pre-signal and post-signal candles to understand why a setup won, lost, hit TP1/TP2, stopped out or became unverifiable.
4. Form one hypothesis at a time and test it with out-of-sample / walk-forward backtesting, realistic fees/slippage and the same market universe.
5. Keep proposed changes in paper validation before promoting them to live alerts. The bot does not automatically rewrite or optimize its own strategies from a small number of outcomes.

Live operation remains signals-only. No exchange order placement is enabled.
