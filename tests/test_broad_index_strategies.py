"""Regression checks for signal history and feasible grid fills."""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.studies.broad_index_strategies.common import backtest as b


def prices(rows, start='2020-01-02'):
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'],
                        index=pd.bdate_range(start, periods=len(rows)))


class BroadIndexRegressionTests(unittest.TestCase):
    def test_run_keeps_pre_window_trend_history(self):
        index = pd.bdate_range('2019-01-01', periods=270)
        close = np.linspace(10, 20, len(index))
        px = pd.DataFrame({k: close for k in ['open', 'high', 'low', 'close']}, index=index)
        pe = pd.Series(10., index=pd.date_range('2010-01-31', periods=130, freq='ME'))
        with patch.object(b, 'load_etf', return_value=px):
            met, _, _ = b.run_etf('510300', pe)
        self.assertGreater(met.loc['趋势', '平均仓位%'], 50)

    def test_grid_starts_half_invested(self):
        res, _ = b.sim_grid(prices([(10, 10, 10, 10)]))
        self.assertAlmostEqual(res.exposure.iloc[0], .5, delta=.01)

    def test_grid_cannot_sell_newly_bought_inventory(self):
        ledger = []
        px = prices([(10, 10, 10, 10), (10, 10.1, 7, 8)])
        b.sim_grid(px, ledger=ledger)
        day = [t for t in ledger if t['date'] == px.index[1]]
        overnight = sum(t['quantity'] for t in ledger if t['date'] == px.index[0])
        self.assertLessEqual(sum(t['quantity'] for t in day if t['side'] == 'sell'), overnight)
        self.assertTrue(all(t['sellable_after'] >= 0 for t in day))

    def test_opening_gap_fills_at_open(self):
        ledger = []
        px = prices([(10, 10, 10, 10), (8, 8.1, 7.9, 8)])
        b.sim_grid(px, ledger=ledger)
        gap = [t for t in ledger if t['phase'] == 'open_gap']
        self.assertTrue(gap)
        self.assertTrue(all(t['price'] == 8 for t in gap))
        for trade in ledger:
            bar = px.loc[trade['date']]
            self.assertLessEqual(bar.low, trade['price'])
            self.assertGreaterEqual(bar.high, trade['price'])

    def test_grid_path_is_independent_of_close_direction(self):
        common = [(10, 10, 10, 10), (10, 11, 9, 10)]
        low_close = prices(common + [(10, 11, 9, 9.5)])
        high_close = prices(common + [(10, 11, 9, 10.5)])
        ledgers = []
        for px in (low_close, high_close):
            ledger = []
            b.sim_grid(px, ledger=ledger, intraday_path='low_first')
            ledgers.append([(t['side'], t['price']) for t in ledger
                            if t['date'] == px.index[-1] and t['phase'] != 'close_segment'])
        self.assertEqual(*ledgers)

    def test_no_ma_history_is_rejected(self):
        with self.assertRaises(ValueError):
            b.window_start(prices([(10, 10, 10, 10)]))

    def test_open_gap_up_sells_at_open(self):
        ledger = []
        px = prices([(10, 10, 10, 10), (12, 12.1, 11.9, 12)])
        b.sim_grid(px, ledger=ledger)
        gap = [t for t in ledger if t['phase'] == 'open_gap']
        self.assertTrue(gap)
        self.assertTrue(all(t['side'] == 'sell' and t['price'] == 12 for t in gap))

    def test_initial_day_inventory_cannot_be_sold(self):
        ledger = []
        b.sim_grid(prices([(10, 12, 9, 11)]), ledger=ledger)
        self.assertFalse(any(t['side'] == 'sell' for t in ledger))

    def test_nav_includes_first_day_loss(self):
        px = prices([(10, 10, 8, 8)])
        res, count = b.sim_buyhold(px)
        met = b.metrics(res.nav, res.exposure, count)
        self.assertLess(met['最大回撤%'], -19)
        annual = b.annual_returns({'hold': res.nav})
        self.assertAlmostEqual(annual.iloc[0, 0], res.nav.iloc[0] / b.CAPITAL - 1)

    def test_target_retries_after_missing_open(self):
        px = prices([(np.nan, 10, 10, 10), (10, 10, 10, 10)])
        ledger = []
        _, count = b.sim_target(px, {px.index[0]: 1.}, ledger=ledger)
        self.assertEqual(count, 1)
        self.assertEqual(ledger[0]['date'], px.index[1])

    def test_price_path_accounting_and_t1_across_months(self):
        rng = np.random.default_rng(42)
        previous = 10.
        rows = []
        for _ in range(80):
            o = previous * rng.uniform(.88, 1.12)
            c = o * rng.uniform(.92, 1.08)
            rows.append((o, max(o, c) * 1.06, min(o, c) * .94, c))
            previous = c
        px = prices(rows)
        for path in ('low_first', 'high_first'):
            ledger = []
            res, count = b.sim_grid(px, intraday_path=path, ledger=ledger)
            self.assertEqual(count, len(ledger))
            np.testing.assert_allclose(res.nav, res.cash + res.shares * px.close)
            self.assertTrue((res.cash >= -1e-7).all())
            overnight = 0.
            for date in px.index:
                day = [t for t in ledger if t['date'] == date]
                self.assertLessEqual(sum(t['quantity'] for t in day if t['side'] == 'sell'), overnight)
                for trade in day:
                    self.assertLessEqual(px.loc[date, 'low'], trade['price'])
                    self.assertGreaterEqual(px.loc[date, 'high'], trade['price'])
                    self.assertGreaterEqual(trade['sellable_after'], 0)
                overnight = res.loc[date, 'shares']


if __name__ == '__main__':
    unittest.main()
