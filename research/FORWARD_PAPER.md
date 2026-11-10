# Orderflow forward paper ledger

The active CVD and Wick Hunt engines had no tracked trade outcomes. Delivered
signals now enter an independent simulated account when a fresh public book is
available. `/paper` shows results to the existing authorized administrator.
No orders, API keys, payments, or new external notifications are introduced.

Fixed assumptions (not optimized): 10,000 USDT initial balance; 0.25% stop-risk
budget including assumed fees/slippage; 10% notional cap per trade; three open
positions maximum; one position per symbol across engines; 2% realized daily
loss stop using UTC days. Gap losses and funding can exceed the sizing budget.

Longs enter at ask and exit at bid; shorts reverse this. Additional adverse
slippage is 0.05% per fill; fees are 0.1% per side. Funding is an assumed adverse
3 bps/8h prorated by holding time, NOT observed funding. TP1 exits half, TP2
exits the remainder; stops use the observed quote, not a guaranteed trigger fill.
Maximum holding time is 24h, exited on the next available quote. Targets use
their trigger prices conservatively even when a quote moves further in favor.

The account uses realized P/L, not marked-to-market equity. Entry fees are
allocated to each exit. Risk limits apply only to this paper account; existing
manual alerts are unchanged. Sizes ignore venue lot/minimum sizes and L2 depth;
results are simulation evidence, not executable order estimates.

SQLite at DATA_DIR/paper.sqlite3 retains positions and rejection reasons.
Configure DATA_DIR on durable storage for uninterrupted collection: `/tmp` and
Render ephemeral filesystems do not survive replacement. Open positions are
retained in the websocket universe when liquid rankings rotate. A removed or
unavailable market may remain unresolved until quotes resume.

Restarted open positions and observed quote gaps above 60 seconds are marked
data_gap. Their P/L remains in account bookkeeping but is excluded from valid
performance statistics. New paper positions are blocked while a gap-affected
position remains open. Sub-minute feed loss, unobserved stop touches, manual
latency and execution impact are not reconstructed. The counter is diagnostic,
not a guarantee of complete market data. No historical results are backfilled
as forward trades. Duplicate signal IDs are recorded once, including rejections.

No statistic auto-enables trading. The existing historical studies failed broad
market profitability; they do not validate the currently active orderflow engine.
Accumulate new forward observations before changing strategy parameters. Report
both engines, all rejections and missing-data counts; do not select only winners.

Offline tests: `python -m unittest discover -s tests -p test_paper_ledger.py -v`.
Full suite: `python -m pytest -q` with requirements.txt installed.
