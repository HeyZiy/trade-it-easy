"""Regression checks for signal history and feasible grid fills."""
import unittest

import numpy as np
import pandas as pd

from research.studies.timing.common import backtest as b
from research.studies.timing import run_level1 as h


def prices(rows, start='2020-01-02'):
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'],
                        index=pd.bdate_range(start, periods=len(rows)))


class TimingEngineRegressionTests(unittest.TestCase):
    def test_run_keeps_pre_window_trend_history(self):
        index = pd.bdate_range('2019-01-01', periods=270)
        close = np.linspace(10, 20, len(index))
        px = pd.DataFrame({k: close for k in ['open', 'high', 'low', 'close']}, index=index)
        runners = h.strategy_runners(px, None)
        met, _, _ = h.evaluate_window(px.loc[h.base.window_start(px):], runners)
        self.assertGreater(met.loc['月末MA200', '平均仓位%'], 50)

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


    def test_round_trip_stats_uses_fifo_and_charges_both_sides(self):
        cost = (1 - b.COST) / (1 + b.COST)
        ledger = [{'side': 'buy', 'price': 10., 'quantity': 100},
                  {'side': 'sell', 'price': 9., 'quantity': 100},
                  {'side': 'buy', 'price': 10., 'quantity': 100},
                  {'side': 'sell', 'price': 12., 'quantity': 100}]
        stats = b.round_trip_stats(ledger)
        self.assertEqual(stats['完整回合数'], 2)
        self.assertAlmostEqual(stats['回合胜率%'], 50.)
        self.assertAlmostEqual(stats['平均盈利%'], (12 / 10 * cost - 1) * 100)
        self.assertAlmostEqual(stats['平均亏损%'], (9 / 10 * cost - 1) * 100)
        self.assertAlmostEqual(stats['盈亏比'], (12 / 10 * cost - 1) / (1 - 9 / 10 * cost))
        self.assertEqual(stats['期末未平批次'], 0)

    def test_round_trip_stats_matches_earliest_unsold_lot(self):
        ledger = [{'side': 'buy', 'price': 10., 'quantity': 300},
                  {'side': 'sell', 'price': 15., 'quantity': 100}]
        stats = b.round_trip_stats(ledger)
        self.assertEqual(stats['完整回合数'], 1)
        self.assertAlmostEqual(stats['回合胜率%'], 100.)
        self.assertEqual(stats['期末未平批次'], 1)
        self.assertTrue(np.isnan(stats['平均亏损%']))
        self.assertTrue(np.isnan(stats['盈亏比']))

    def test_buyhold_has_no_closed_round_trip(self):
        px = prices([(10, 10, 10, 10), (10, 11, 9, 11)])
        ledger = []
        b.sim_buyhold(px, ledger=ledger)
        stats = b.round_trip_stats(ledger)
        self.assertEqual(stats['完整回合数'], 0)
        self.assertEqual(stats['期末未平批次'], 1)
        self.assertTrue(np.isnan(stats['回合胜率%']))

    def test_grid_round_trips_cover_every_sold_unit(self):
        rng = np.random.default_rng(7)
        previous = 10.
        rows = []
        for _ in range(60):
            o = previous * rng.uniform(.9, 1.1)
            c = o * rng.uniform(.94, 1.06)
            rows.append((o, max(o, c) * 1.05, min(o, c) * .95, c))
            previous = c
        ledger = []
        b.sim_grid(prices(rows), ledger=ledger)
        sold = sum(t['quantity'] for t in ledger if t['side'] == 'sell')
        unit = min(t['quantity'] for t in ledger)
        stats = b.round_trip_stats(ledger)
        self.assertEqual(stats['完整回合数'] * unit, sold)
        self.assertGreater(stats['期末未平批次'], 0)

    def test_attach_round_trips_keeps_one_row_per_strategy(self):
        px = prices([(10, 10, 10, 10), (10, 11, 9, 11)])
        ledgers, rows = {}, {}
        for name, run in (('长拿', b.sim_buyhold), ('网格', b.sim_grid)):
            ledgers[name] = []
            curve, count = run(px, ledger=ledgers[name])
            rows[name] = b.metrics(curve.nav, curve.exposure, count)
        met = b.attach_round_trips(pd.DataFrame(rows).T, ledgers)
        self.assertEqual(list(met.columns[-len(b.ROUND_TRIP_COLS):]), b.ROUND_TRIP_COLS)
        self.assertEqual(met.loc['长拿', '完整回合数'], 0)
        self.assertGreater(met.loc['网格', '完整回合数'], 0)


if __name__ == '__main__':
    unittest.main()
