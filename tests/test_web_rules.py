# -*- coding: utf-8 -*-
"""网页大表规则集：预热起点、指数后缀、各机制目标生成器与条件组装。"""
import unittest

import numpy as np
import pandas as pd

from research.studies.timing import web_rules as wr
from research.studies.timing import web_report as wrp


def history(closes, volume=False):
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    c = np.asarray(closes, dtype=float)
    frame = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c}, index=idx)
    if volume:
        frame["volume"] = np.full(len(c), 2e6)
    return frame


def rising(n=600, volume=False):
    return history(10 * np.exp(np.linspace(0, 0.5, n)), volume=volume)


class WindowAndSymbolTests(unittest.TestCase):
    def test_window_start_is_272nd_close(self):
        h = rising(400)
        self.assertEqual(wr.window_start(h), h.index[271])

    def test_insufficient_history_rejected(self):
        with self.assertRaises(ValueError):
            wr.window_start(rising(271))

    def test_index_symbol_suffixes(self):
        self.assertEqual(wr.index_symbol("000300"), "000300.SH")
        self.assertEqual(wr.index_symbol("399006"), "399006.SZ")
        self.assertEqual(wr.index_symbol("930000"), "930000.CSI")

    def test_unsupported_index_segment_rejected(self):
        with self.assertRaises(ValueError):
            wr.index_symbol("888888")


class AssemblyTests(unittest.TestCase):
    def test_base_row_count_without_pe_or_volume(self):
        runners = wr.strategy_runners(rising())
        self.assertEqual(len(runners), 24)
        self.assertNotIn("PE分位", runners)
        self.assertNotIn("放量突破", runners)

    def test_volume_adds_breakout_row(self):
        runners = wr.strategy_runners(rising(volume=True))
        self.assertEqual(len(runners), 25)
        self.assertIn("放量突破", runners)

    def test_pe_adds_valuation_row(self):
        h = rising()
        pe = pd.Series(np.linspace(10, 30, len(h)), index=h.index)
        runners = wr.strategy_runners(h, pe=pe)
        self.assertEqual(len(runners), 25)
        self.assertIn("PE分位", runners)

    def test_family_covers_every_runner_and_star_is_subset(self):
        h = rising(volume=True)
        pe = pd.Series(np.linspace(10, 30, len(h)), index=h.index)
        runners = wr.strategy_runners(h, pe=pe)
        self.assertEqual(len(runners), 26)
        for name in runners:
            self.assertIn(name, wr.FAMILY)
        for name in wr.STAR:
            self.assertIn(name, runners)


class GeneratorTests(unittest.TestCase):
    def test_reversal_buys_next_open_and_sells_after_hold(self):
        h = history([10] * 10 + [9] * 20)
        target = wr.reversal_target(h)
        self.assertEqual(target, {h.index[11]: 1.0, h.index[16]: 0.0})

    def test_reversal_holds_to_end_when_expiry_exceeds_sample(self):
        h = history([10] * 10 + [9] * 3)  # n=13, buy idx11, sell idx16 >= n
        target = wr.reversal_target(h)
        self.assertEqual(target, {h.index[11]: 1.0})

    def test_dual_ma_is_stateless_and_executes_next_day(self):
        h = rising(120)
        target = wr.dual_ma_target(h, 5, 20)
        self.assertTrue(set(target.values()) <= {0.0, 1.0})
        self.assertEqual(min(target), h.index[20])
        self.assertEqual(target[h.index[-1]], 1.0)

    def test_ma_confirm_only_emits_zero_one_on_execution_days(self):
        h = rising(400)
        target = wr.ma_confirm_target(h)
        self.assertTrue(set(target.values()) <= {0.0, 1.0})
        self.assertTrue(all(key in h.index[1:] for key in target))

    def test_chandelier_has_entry_and_trailing_exit(self):
        target = wr.chandelier_target(history([10] * 60 + [15] * 60 + [10] * 60))
        self.assertIn(1.0, target.values())
        self.assertIn(0.0, target.values())

    def test_donchian_ma_filter_entries_subset_of_plain(self):
        h = rising(400)
        plain = wr.dc.build_target(h, 55, 20)
        filtered = wr.donchian_ma_filter_target(h)
        plain_entries = {k for k, v in plain.items() if v == 1.0}
        filtered_entries = {k for k, v in filtered.items() if v == 1.0}
        self.assertTrue(filtered_entries <= plain_entries)

    def test_volume_breakout_requires_volume_column(self):
        with self.assertRaises(ValueError):
            wr.volume_breakout_target(rising())
        self.assertIsInstance(wr.volume_breakout_target(rising(volume=True)), dict)

    def test_mom_targets_are_zero_one(self):
        target = wr.mom_target(rising(400), 252)
        self.assertTrue(set(target.values()) <= {0.0, 1.0})

    def test_rv_regime_targets_are_zero_one(self):
        target = wr.rv_regime_target(rising(400))
        self.assertTrue(set(target.values()) <= {0.0, 1.0})


class ReportIntegrationTests(unittest.TestCase):
    def test_full_report_with_pe_surfaces_family_and_index(self):
        h = rising(volume=True)
        pe = pd.Series(np.linspace(10, 30, len(h)), index=h.index)
        group, runners = wrp.full_report(h, "测试源", pe=pe, index_code="000300", pe_as_of="2024-01-01")
        self.assertIn("PE分位", group["series"])
        self.assertIn("放量突破", group["series"])
        self.assertEqual(group["index_code"], "000300")
        self.assertEqual(group["pe_as_of"], "2024-01-01")
        self.assertEqual(group["start"], h.index[271].date().isoformat())
        self.assertTrue(all("族" in row for row in group["metrics"]))
        self.assertEqual(len(group["series"]), 26)

    def test_rolling_cohorts_use_web_window_start(self):
        h = rising(700)
        runners = wr.strategy_runners(h)
        star = {name: runners[name] for name in wr.STAR}
        cohorts = wr.rolling_cohorts(h, star, 1)
        self.assertFalse(cohorts.empty)
        self.assertEqual(wr.window_start(h), h.index[271])


if __name__ == "__main__":
    unittest.main()