# Project maintenance brief

Maintain A&T TrendHive Crypto Sniper as a **signal-only** Telegram scanner for Bitget USDT-margined perpetual markets. Never add exchange order placement, private account access, leverage controls, or automatic execution. Keep `main.py` as the Render entry point (`python main.py`).

Preserve 15m entries with 1h/4h/1d context, BTC/ETH macro context, EMA/RSI/ATR/volume checks, clear LONG/SHORT alerts, entry/SL/TP1/TP2 references, data-quality rejection, admin-only Telegram commands, and Render `/health` support. Do not invent market data or promise profitability. Any strategy changes must include deterministic tests and explain trade-offs in README.
