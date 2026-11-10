import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from paper_ledger import PaperLedger


class PaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'paper.sqlite3'
        self.ledger = PaperLedger(self.path)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def signal(self, symbol='BTC', side='LONG', stamp=100):
        return SimpleNamespace(engine_id='CVD', symbol=symbol, side=side,
                               timestamp=stamp, stop_loss=98 if side == 'LONG' else 102,
                               tp1=103 if side == 'LONG' else 97,
                               tp2=105 if side == 'LONG' else 95)

    def enter(self, symbol='BTC', side='LONG', stamp=100):
        self.ledger.quote(symbol, 99.9, 100.1, now=100)
        return self.ledger.record(self.signal(symbol, side, stamp), now=100)

    def test_requires_fresh_quote_and_deduplicates_rejected_signal(self):
        self.assertEqual(self.ledger.record(self.signal(), now=100), 'NO_FRESH_QUOTE')
        self.assertEqual(self.enter(), 'DUPLICATE')
        self.assertEqual(len(self.ledger.rows()), 1)

    def test_cost_inclusive_risk_and_notional_caps(self):
        self.assertEqual(self.enter(), 'OPEN')
        r = self.ledger.rows()[0]
        self.assertLessEqual(r['entry'] * r['qty'], 1000.000001)
        unit_risk = r['entry'] - r['stop'] + .001 * (r['entry'] + r['stop']) + .0005 * r['stop']
        self.assertLessEqual(unit_risk * r['qty'], 25.000001)
        self.assertGreater(r['entry'], 100.1)

    def test_gap_stop_uses_observed_worse_price_and_all_costs(self):
        self.enter()
        r = self.ledger.rows()[0]
        self.ledger.quote('BTC', 95, 95.1, now=110)
        result = self.ledger.rows()[0]
        fill = 95 * .9995
        expected = (fill - r['entry']) * r['qty'] - .001 * r['qty'] * (fill + r['entry'])
        expected -= r['entry'] * r['qty'] * .0003 * 10 / 28800
        self.assertAlmostEqual(result['net_pnl'], expected)
        self.assertEqual(result['reason'], 'STOP')
        self.assertAlmostEqual(self.ledger.balance(), 10000 + expected)

    def test_partial_target_not_counted_twice(self):
        self.enter()
        self.ledger.quote('BTC', 103.2, 103.3, now=110)
        r = self.ledger.rows()[0]
        self.assertAlmostEqual(r['remaining'], r['qty'] / 2)
        self.ledger.quote('BTC', 103.3, 103.4, now=120)
        self.assertEqual(self.ledger.rows()[0]['net_pnl'], r['net_pnl'])
        self.ledger.quote('BTC', 105.1, 105.2, now=130)
        self.assertEqual(self.ledger.summary()['valid_closed'], 1)
        self.assertEqual(self.ledger.rows()[0]['reason'], 'TP2')

    def test_short_executes_exit_at_ask(self):
        self.enter(side='SHORT')
        self.ledger.quote('BTC', 102, 103, now=110)
        r = self.ledger.rows()[0]
        self.assertAlmostEqual(r['exit'], 103 * 1.0005)
        self.assertLess(r['net_pnl'], 0)

    def test_overlapping_engines_share_symbol_and_account_caps(self):
        self.enter()
        s = self.signal(side='SHORT')
        s.engine_id = 'WICK'
        self.assertEqual(self.ledger.record(s, now=101), 'SYMBOL_ALREADY_OPEN')
        self.enter('ETH')
        self.enter('SOL')
        self.assertEqual(self.enter('XRP'), 'POSITION_CAP')

    def test_feed_gap_excluded_and_blocks_new_positions(self):
        self.enter()
        self.ledger.quote('BTC', 100, 100.1, now=161)
        self.ledger.quote('ETH', 99.9, 100.1, now=161)
        self.assertEqual(self.ledger.record(self.signal('ETH'), now=161), 'UNRESOLVED_FEED_GAP')
        self.ledger.quote('BTC', 105.1, 105.2, now=162)
        s = self.ledger.summary()
        self.assertEqual(s['closed'], 1)
        self.assertEqual(s['valid_closed'], 0)
        self.assertEqual(s['valid_net_pnl'], 0)

    def test_restart_preserves_position_but_invalidates_evidence(self):
        self.enter()
        self.ledger.close()
        self.ledger = PaperLedger(self.path)
        self.assertEqual(self.ledger.open_symbols(), ['BTC'])
        self.assertTrue(self.ledger.rows()[0]['data_gap'])
        self.ledger.quote('BTC', 95, 95.1, now=110)
        self.assertEqual(self.ledger.summary()['valid_closed'], 0)
        self.assertLess(self.ledger.balance(), 10000)

    def test_bad_and_out_of_order_quotes_do_not_fill(self):
        self.enter()
        for bid, ask, now in [(float('nan'), 99, 110), (90, 89, 110), (90, 91, 99)]:
            self.ledger.quote('BTC', bid, ask, now=now)
        self.assertEqual(self.ledger.rows()[0]['status'], 'OPEN')

    def test_daily_loss_gate_and_next_day_reset(self):
        self.enter()
        self.ledger.quote('BTC', 50, 50.1, now=110)
        self.ledger.quote('ETH', 99.9, 100.1, now=111)
        self.assertEqual(self.ledger.record(self.signal('ETH'), now=111), 'DAILY_LOSS_LIMIT')
        self.ledger.quote('ETH', 99.9, 100.1, now=86500)
        self.assertEqual(self.ledger.record(self.signal('ETH', stamp=86500), now=86500), 'OPEN')

    def test_invalid_levels_and_stale_quote_reject(self):
        self.ledger.quote('BTC', 99.9, 100.1, now=100)
        s = self.signal()
        s.stop_loss = float('nan')
        self.assertEqual(self.ledger.record(s, now=100), 'INVALID_OR_CHASED_LEVELS')
        self.assertEqual(self.ledger.record(self.signal(stamp=105), now=105), 'NO_FRESH_QUOTE')

    def test_time_exit_and_gap_are_explicit(self):
        self.enter()
        self.ledger.quote('BTC', 100, 100.1, now=86501)
        self.assertEqual(self.ledger.rows()[0]['reason'], 'TIME_EXIT')
        self.assertTrue(self.ledger.rows()[0]['data_gap'])


if __name__ == '__main__':
    unittest.main()
