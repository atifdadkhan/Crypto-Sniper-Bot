"""Forward-only quote simulation. No exchange orders or account credentials.

SQLite persists outcomes; feed gaps invalidate evidence, not magically fill stops.
All positions share one hypothetical account, including opposing strategies.
"""
import json
import math
import sqlite3
import time


class PaperLedger:
    INITIAL = 10000.0
    RISK = 0.0025
    FEE = 0.001       # assumed per side
    SLIP = 0.0005     # adverse per fill, beyond observed spread
    FUNDING = 0.0003  # adverse assumption per 8h, not historical funding

    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute('CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS trade_status ON trades(status)')
        self.db.execute('CREATE TABLE IF NOT EXISTS fills (at REAL, pnl REAL)')
        self.quotes = {}
        self.last_seen = {}
        # An offline interval cannot be reconstructed from a new quote.
        with self.db:
            for row in self.rows(open_only=True):
                if row['status'] == 'OPEN':
                    row['data_gap'] = True
                    self._save(row)

    def rows(self, open_only=False):
        query = 'SELECT data FROM trades'
        if open_only:
            query += " WHERE status='OPEN'"
        return [json.loads(r[0]) for r in self.db.execute(query + ' ORDER BY rowid')]

    def _save(self, row):
        self.db.execute('INSERT OR REPLACE INTO trades VALUES (?, ?, ?)',
                        (row['id'], json.dumps(row, allow_nan=False), row['status']))

    def open_symbols(self):
        return sorted({r['symbol'] for r in self.rows(open_only=True)})

    def balance(self):
        return self.INITIAL + self.db.execute('SELECT COALESCE(SUM(pnl),0) FROM fills').fetchone()[0]

    def record(self, signal, now=None):
        now = time.time() if now is None else now
        key = f'{signal.engine_id}|{signal.symbol}|{signal.side}|{signal.timestamp}'
        if self.db.execute('SELECT 1 FROM trades WHERE id=?', (key,)).fetchone():
            return 'DUPLICATE'
        row = {'id': key, 'engine': signal.engine_id, 'symbol': signal.symbol,
               'side': signal.side, 'at': now, 'status': 'REJECTED', 'net_pnl': 0.0}
        quote = self.quotes.get(signal.symbol)
        opened = self.rows(open_only=True)
        balance = self.balance()
        today = now - now % 86400  # UTC day, explicit across hosts
        day_pnl = self.db.execute('SELECT COALESCE(SUM(pnl),0) FROM fills WHERE at>=?', (today,)).fetchone()[0]
        reason = None
        if not quote or not 0 <= now - quote[2] <= 3:
            reason = 'NO_FRESH_QUOTE'
        elif signal.side not in ('LONG', 'SHORT'):
            reason = 'INVALID_SIDE'
        elif any(r['symbol'] == signal.symbol for r in opened):
            reason = 'SYMBOL_ALREADY_OPEN'
        elif len(opened) >= 3:
            reason = 'POSITION_CAP'
        elif any(r['data_gap'] for r in opened):
            reason = 'UNRESOLVED_FEED_GAP'
        elif balance <= 0 or day_pnl <= -0.02 * (balance - day_pnl):
            reason = 'DAILY_LOSS_LIMIT'
        if reason is None:
            long = signal.side == 'LONG'
            entry = quote[1] * (1 + self.SLIP) if long else quote[0] * (1 - self.SLIP)
            stop, tp1, tp2 = map(float, (signal.stop_loss, signal.tp1, signal.tp2))
            valid = all(math.isfinite(v) and v > 0 for v in (entry, stop, tp1, tp2))
            valid = valid and (stop < entry < tp1 < tp2 if long else stop > entry > tp1 > tp2)
            if not valid or abs(entry - stop) / entry > .03:
                reason = 'INVALID_OR_CHASED_LEVELS'
            else:
                # Include entry/exit fee and stop slippage in the sizing budget.
                unit_risk = abs(entry - stop) + self.FEE * (entry + stop) + stop * self.SLIP
                qty = min(balance * self.RISK / unit_risk, balance * .10 / entry)
                row.update(status='OPEN', entry=entry, stop=stop, tp1=tp1, tp2=tp2,
                           qty=qty, remaining=qty, tp1_hit=False, data_gap=False,
                           last_fill_at=now)
                self.last_seen[key] = quote[2]
        if reason:
            row['reason'] = reason
        with self.db:
            self._save(row)
        return reason or 'OPEN'

    def _fill(self, row, price, qty, now, reason):
        long = row['side'] == 'LONG'
        fill = price * (1 - self.SLIP if long else 1 + self.SLIP)
        gross = (fill - row['entry']) * qty * (1 if long else -1)
        fees = self.FEE * qty * (row['entry'] + fill)
        funding = row['entry'] * qty * self.FUNDING * max(0, now - row['at']) / 28800
        pnl = gross - fees - funding
        row['net_pnl'] += pnl
        row['remaining'] = max(0, row['remaining'] - qty)
        row['last_fill_at'] = now
        self.db.execute('INSERT INTO fills VALUES (?, ?)', (now, pnl))
        if row['remaining'] <= row['qty'] * 1e-9:
            row.update(status='CLOSED', exit=fill, exit_at=now, reason=reason)
        self._save(row)

    def quote(self, symbol, bid, ask, now=None):
        now = time.time() if now is None else now
        if not all(math.isfinite(x) and x > 0 for x in (bid, ask, now)) or bid > ask:
            return
        previous = self.quotes.get(symbol)
        if previous and now <= previous[2]:
            return
        self.quotes[symbol] = (bid, ask, now)
        with self.db:
            for row in self.rows(open_only=True):
                if row['symbol'] != symbol or row['status'] != 'OPEN':
                    continue
                last = self.last_seen.get(row['id'], row['at'])
                if now - last > 60 and not row['data_gap']:
                    row['data_gap'] = True
                    self._save(row)
                self.last_seen[row['id']] = now
                long = row['side'] == 'LONG'
                price = bid if long else ask
                stop_hit = price <= row['stop'] if long else price >= row['stop']
                target2 = price >= row['tp2'] if long else price <= row['tp2']
                target1 = price >= row['tp1'] if long else price <= row['tp1']
                if stop_hit:
                    self._fill(row, price, row['remaining'], now, 'STOP')
                elif now - row['at'] >= 86400:
                    self._fill(row, price, row['remaining'], now, 'TIME_EXIT')
                else:
                    if target1 and not row['tp1_hit']:
                        row['tp1_hit'] = True
                        self._fill(row, row['tp1'], row['qty'] * .5, now, 'TP1')
                    if target2:
                        self._fill(row, row['tp2'], row['remaining'], now, 'TP2')

    def summary(self):
        rows = self.rows()
        closed = [r for r in rows if r['status'] == 'CLOSED']
        valid = [r for r in closed if not r['data_gap']]
        gains = sum(max(0, r['net_pnl']) for r in valid)
        losses = -sum(min(0, r['net_pnl']) for r in valid)
        groups = {}
        for r in valid:
            g = groups.setdefault(r['engine'], {'trades': 0, 'net_pnl': 0.0})
            g['trades'] += 1
            g['net_pnl'] += r['net_pnl']
        return {'status': 'FORWARD_PAPER_ONLY', 'balance_realized': self.balance(),
                'open': sum(r['status'] == 'OPEN' for r in rows),
                'rejected': sum(r['status'] == 'REJECTED' for r in rows),
                'closed': len(closed), 'valid_closed': len(valid),
                'feed_gap_trades': sum(r.get('data_gap', False) for r in rows),
                'valid_net_pnl': gains - losses,
                'profit_factor': gains / losses if losses else None,
                'engines': groups, 'live_approved': False}

    def close(self):
        self.db.close()
