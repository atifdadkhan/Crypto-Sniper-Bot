# Live Bitget Orderflow Quant Engines — Deployment Specification

## Scope
Public Bitget USDT-perpetual **Telegram signals**, not account trading.
The production default is `CRYPTO_ORDERFLOW_ONLY=true`: prior RSI, MACD and
EMA scanners are not scheduled. Each independent engine emits its own
`engine_id`, signal name, side, USDT entry/SL/TP1/TP2 and rationale.

### Public data
- `ccxt.pro.bitget`: free `watch_trades(symbol)` and `watch_order_book(symbol, limit=5)`.
- Select dynamically from the most liquid eligible Bitget USDT linear futures;
  default `ORDERFLOW_TOP_N=8`, BTC prioritized; rotate the universe every 4h.
- Observe actual exchange aggressor-side field and base quantity, not candles
  or signed returns. Reject missing trade IDs, duplicates, invalid numbers,
  out-of-order events and stale quotes. Reset CVD on stream gaps >60 seconds or
  reconnections; require warmup before making a call.
- 3-second local book/trade freshness; max spread 0.15%; do not trust stale
  depth or assume visible book liquidity represents actual filled orders.
- L2 depth imbalance (quote notional): `I = Σ_5(bid_price * bid_qty) /
  Σ_5(ask_price * ask_qty)`. LONG wick requires I > 3; SHORT wick requires
  inverse imbalance >3. This is not a spoofing guarantee.

### Independent engine: `CVD_DIVERGENCE`
`CVD_t = CVD_(t-1) + q_t` for aggressive buyer, `-q_t` for seller.
Maintain 15-second nonempty price/delta buckets, warm up for at least 32
buckets, and compare two 16-bucket windows:
- BEAR: recent price max exceeds older max by 0.04%, while minimum cumulative
  delta is lower in the recent window, and price has rejected from the high.
- BULL: recent price min is below older min by 0.04%, while recent *maximum*
  cumulative delta is higher, and price has reclaimed the low.
These are the user's intentionally strict HH-vs-LL / LL-vs-HH conditions,
not classical HH-vs-lower-high swing divergences.
Only emit if protective stop distance is 0.08–1.8% and TP2 >=1R.

### Independent engine: `WICK_HUNT`
The previous 12 completed 1H candles, excluding the last two, define swing
stop-pool levels. A transient price sweep must first cross the prior swing and
then reclaim/reject the zone by 0.02%. Require opposite-side top-five-level
book notional >3:1, a 3-second fresh book and a validated 8-minute rolling
trade VWAP `VWAP = Σ(price * amount) / Σ(amount)`.
Use the wick extreme + a modest buffer for SL; only emit if VWAP lies in the
profit direction and at least 1R away. No guarantee of fills.

### Risk and execution limitations
`fractional_kelly(p,b)` computes max(0,min(2%,0.25*(p-(1-p)/b))).
It must be fed **actual settled trade outcomes**; those do not exist in the
current signal-only app. Never infer win probabilities from a setup score,
nor enter an order from an unverified Kelly estimate.
No private account API keys, no IOC order placement, no fill reconciliation,
no native stop placement and no TP/SL-hit lifecycle are deployed. Even an
exchange-accepted IOC does not eliminate adverse fills within its limit price.

### Configuration and operations
Set `ORDERFLOW_TOP_N` to a realistic live WebSocket subscription budget.
Retain production WebSocket reconnect backoffs; monitor `/ready` for fresh
trades **and** L2 events. Render suspension, bot-token polling conflicts or
stale data mean that *live verified* status must not be claimed.
Mathematical tests are in `tests/test_orderflow_engines.py`; these do not
replace forward fill/slippage testing.
