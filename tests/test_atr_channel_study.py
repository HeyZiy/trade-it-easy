import unittest

import numpy as np
import pandas as pd

from research.studies.broad_index_strategies.common import atr_channel as a


def prices(rows):
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'],
                        index=pd.bdate_range('2020-01-02', periods=len(rows)))


class AtrChannelStudyTests(unittest.TestCase):
    def test_atr_includes_gap_and_uses_wilder_seed(self):
        px = prices([(10, 10.5, 9.5, 10), (12, 12.5, 11.5, 12), (12, 12.5, 11.5, 12)])
        atr = a.average_true_range(px, 2)
        self.assertTrue(np.isnan(atr.iloc[0]))
        np.testing.assert_allclose(atr.iloc[1:], [1.75, 1.375])

    def test_signal_known_at_close_executes_next_open(self):
        px = prices([(10, 10, 10, 10), (10, 10, 10, 10),
                     (10, 12, 10, 12), (13, 13, 13, 13)])
        sig = a.channel_signals(px, 2, 2, .1)
        target = a.dc.targets_from_signals(sig)
        ledger=[]
        a.base.sim_target(px, target, ledger=ledger)
        self.assertEqual(ledger[0]['date'], px.index[3])
        self.assertEqual(ledger[0]['price'], 13)

    def test_midline_and_lower_exit_are_separate_rules(self):
        px = prices([(10, 12, 8, 10), (10, 12, 8, 10), (10, 11, 8, 9)])
        lower = a.channel_signals(px, 2, 2, 1.5, 'lower')
        middle = a.channel_signals(px, 2, 2, 1.5, 'middle')
        self.assertTrue(np.isnan(lower.target.iloc[-1]))
        self.assertEqual(middle.target.iloc[-1], 0.)

    def test_future_prices_do_not_change_old_channel(self):
        px = prices([(10, 11, 9, 10), (11, 12, 10, 11), (12, 13, 11, 12), (13, 14, 12, 13)])
        changed = px.copy()
        changed.iloc[-1] = [30, 40, 20, 35]
        pd.testing.assert_frame_equal(a.channel_signals(px, 2, 2).iloc[:-1],
                                      a.channel_signals(changed, 2, 2).iloc[:-1])


if __name__ == '__main__':
    unittest.main()
