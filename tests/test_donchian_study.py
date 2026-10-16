"""Signal-time and execution-time checks for the independent Donchian study."""
import unittest

import numpy as np
import pandas as pd

from research.studies.broad_index_strategies.common import donchian as d


def prices():
    return pd.DataFrame([(10, 11, 9, 10), (10, 12, 9.5, 11),
                         (12, 13, 11.5, 12.5), (12.5, 13, 11, 12),
                         (10, 10.5, 8, 9), (9, 9.5, 8.5, 9)],
                        columns=['open', 'high', 'low', 'close'],
                        index=pd.bdate_range('2020-01-02', periods=6))


class DonchianStudyTests(unittest.TestCase):
    def test_daily_ma_can_use_an_explicit_period_without_changing_default(self):
        px = prices()
        self.assertEqual(d.daily_ma_target(px), {})
        self.assertEqual(d.daily_ma_target(px, window=2),
                         {px.index[2]: 1., px.index[3]: 1., px.index[4]: 0., px.index[5]: 0.})
        with self.assertRaises(ValueError):
            d.daily_ma_target(px, window=0)

    def test_prior_channel_next_open_and_hysteresis(self):
        px = prices()
        signals = d.channel_signals(px, 2, 2)
        target = d.targets_from_signals(signals)
        self.assertEqual(target, {px.index[3]: 1., px.index[5]: 0.})
        self.assertEqual(signals.entry_upper.iloc[2], 12.)
        ledger = []
        curve, count = d.base.sim_target(px, target, ledger=ledger)
        self.assertEqual(count, 2)
        self.assertEqual([t['price'] for t in ledger], [12.5, 9.])
        self.assertGreater(curve.shares.iloc[4], 0)
        self.assertEqual(curve.shares.iloc[5], 0)

    def test_pre_window_prices_are_available_without_carrying_positions(self):
        px = prices()
        target = d.build_target(px, 2, 2, start=px.index[3])
        self.assertEqual(target[px.index[3]], 1.)
        self.assertNotIn(px.index[2], target)

    def test_future_changes_do_not_change_existing_signals(self):
        px = prices()
        changed = px.copy()
        changed.loc[changed.index[-1], ['open', 'high', 'low', 'close']] = [50, 60, 40, 55]
        pd.testing.assert_frame_equal(d.channel_signals(px, 2, 2).iloc[:-1],
                                      d.channel_signals(changed, 2, 2).iloc[:-1])

    def test_equal_boundary_is_not_breakout(self):
        px = prices()
        px.loc[px.index[2], 'close'] = 12.
        self.assertTrue(np.isnan(d.channel_signals(px, 2, 2).target.iloc[2]))

    def test_close_channel_is_distinct_from_high_low_channel(self):
        px = prices()
        px.loc[px.index[2], 'close'] = 11.5
        self.assertTrue(np.isnan(d.channel_signals(px, 2, 2).target.iloc[2]))
        self.assertEqual(d.channel_signals(px, 2, 2, 'close').target.iloc[2], 1.)

    def test_rolling_accounts_use_same_start_and_fixed_horizon(self):
        idx = pd.bdate_range('2018-01-01', '2022-12-30')
        close = np.linspace(10, 20, len(idx))
        px = pd.DataFrame({k: close for k in ['open', 'high', 'low', 'close']}, index=idx)
        targets = {name: {t: 1. for t in idx} for name in d.ROLL_CASES}
        rows = d.rolling_cohorts(px, targets, years=1)
        self.assertGreater(len(rows), 0)
        np.testing.assert_allclose(rows['较长拿年化差pp'], 0.)
        self.assertTrue((rows.end < rows.start + pd.DateOffset(years=1)).all())
        self.assertTrue((rows.end >= rows.start + pd.DateOffset(months=11)).all())


if __name__ == '__main__':
    unittest.main()
