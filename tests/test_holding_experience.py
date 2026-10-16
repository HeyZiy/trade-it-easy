"""回撤恢复、未完成区间及收益/风险联合比较的边界测试。"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.studies.broad_index_strategies.common import backtest as b
from research.studies.broad_index_strategies.common import experience as e
from research.studies.broad_index_strategies import run_level1 as h
from research.studies.broad_index_strategies.experiments import grid_reanchor as grid
from research.studies.broad_index_strategies.assets import AssetSpec


def nav(values):
    return pd.Series(values, index=pd.bdate_range('2020-01-02', periods=len(values)), dtype=float)


class HoldingExperienceTests(unittest.TestCase):
    def test_recovered_and_unfinished_periods_both_count(self):
        curve = nav([100, 90, 100, 110, 88, 90])
        metrics = e.drawdown_metrics(curve, 100)
        self.assertEqual(metrics['最长水下交易日'], 2)
        self.assertEqual(metrics['最长水下日历天'], 4)
        self.assertEqual(metrics['当前水下交易日'], 2)
        self.assertEqual(metrics['期末未修复'], 1)
        self.assertAlmostEqual(metrics['深回撤时间占比%'], 100 / 6)
        periods = e.drawdown_periods(curve, 100)
        self.assertEqual(periods['水下交易日'].tolist(), [1, 2])
        self.assertEqual(periods['跨度日历天'].tolist(), [4, 2])
        self.assertEqual(periods['已修复'].tolist(), [True, False])
        self.assertEqual(periods['恢复日'].iloc[0], curve.index[2])
        self.assertTrue(pd.isna(periods['恢复日'].iloc[1]))

    def test_first_day_loss_counts_against_initial_capital(self):
        metrics = e.drawdown_metrics(nav([80]), 100)
        self.assertEqual(metrics['最长水下交易日'], 1)
        self.assertEqual(metrics['当前水下日历天'], 0)
        self.assertEqual(metrics['深回撤时间占比%'], 100)
        self.assertEqual(metrics['期末未修复'], 1)
        self.assertTrue(e.drawdown_periods(nav([80]), 100)['前高为期初资金'].iloc[0])

    def test_equal_high_recovers_and_flat_cash_has_no_drawdown(self):
        metrics = e.drawdown_metrics(nav([100, 90, 100, 100]), 100)
        self.assertEqual(metrics['最长水下交易日'], 1)
        self.assertEqual(metrics['当前水下交易日'], 0)
        self.assertEqual(metrics['期末未修复'], 0)
        flat = nav([100, 100, 100])
        self.assertEqual(e.drawdown_metrics(flat, 100)['最长水下交易日'], 0)
        self.assertTrue(e.drawdown_periods(flat, 100).empty)
        self.assertEqual(e.relative_metrics(flat, flat)['最长连续落后长拿交易日'], 0)

    def test_missing_invalid_or_misaligned_nav_is_rejected(self):
        for curve in (nav([100, np.nan]), nav([100, np.inf]), nav([100, 0]), nav([])):
            with self.assertRaises(ValueError):
                e.drawdown_metrics(curve, 100)
        with self.assertRaises(ValueError):
            e.relative_metrics(nav([100, 110]), nav([100]))
        duplicate = nav([100, 110])
        duplicate.index = pd.DatetimeIndex([duplicate.index[0]] * 2)
        with self.assertRaises(ValueError):
            e.drawdown_metrics(duplicate, 100)

    def test_relative_lag_includes_flat_cash_during_market_rise(self):
        metrics = e.relative_metrics(nav([100, 100, 100, 120]), nav([100, 110, 120, 120]))
        self.assertEqual(metrics['落后长拿时间占比%'], 50)
        self.assertEqual(metrics['最长连续落后长拿交易日'], 2)
        self.assertAlmostEqual(metrics['最大相对落后%'], -100 / 6)
        self.assertEqual(metrics['期末相对长拿差%'], 0)

    def test_joint_summary_keeps_return_risk_tradeoffs_and_ties(self):
        cohorts = pd.DataFrame({
            'strategy': ['择时'] * 5, '较长拿年化差pp': [1, -1, 1, -1, 0],
            '较长拿回撤改善pp': [2, 2, -2, -2, 0],
            '较长拿水下缩短交易日': [1, 1, -1, -1, 0],
            '最大回撤%': [-10] * 5, '最长水下交易日': [10] * 5,
            '期末未修复': [0, 0, 1, 1, 0], '深回撤时间占比%': [0] * 5,
            '落后长拿时间占比%': [50] * 5,
        })
        row = e.rolling_summary(cohorts).loc['择时']
        self.assertEqual(row['收益回撤均改善比例%'], 20)
        self.assertEqual(row['收益降低但回撤改善比例%'], 20)
        self.assertEqual(row['收益增加但回撤恶化比例%'], 20)
        self.assertEqual(row['收益回撤均恶化比例%'], 20)
        self.assertEqual(row['三项均不差比例%'], 40)
        self.assertEqual(row['回撤改善时年化差中位数pp'], 0)
        self.assertEqual(list(e.rolling_summary(pd.DataFrame()).columns), e.SUMMARY_COLS)

    def test_all_window_runners_match_existing_strategy_execution(self):
        dates = pd.bdate_range('2019-01-01', periods=320)
        close = np.linspace(10, 14, len(dates)) + np.sin(np.arange(len(dates)) / 10)
        history = pd.DataFrame({'open': close, 'high': close + .2,
                                'low': close - .2, 'close': close}, index=dates)
        pe = pd.Series(np.linspace(20, 5, 100), index=pd.date_range('2010-01-31', periods=100, freq='ME'))
        runners = h.strategy_runners(history, pe)
        px = history.iloc[230:]
        actual, _ = runners['估值'](px)
        expected, _ = b.sim_target(px, b.build_val_target(px, pe))
        pd.testing.assert_frame_equal(actual, expected)
        self.assertGreater(actual.shares.iloc[0], 0)
        for name, params in [('唐奇安20/10', (20, 10)), ('唐奇安55/20', (55, 20))]:
            actual, _ = runners[name](px)
            expected, _ = b.sim_target(px, h.dc.build_target(history, *params, start=px.index[0]))
            pd.testing.assert_frame_equal(actual, expected)
        metrics, curves = h.evaluate_window(px, runners)
        self.assertEqual(metrics.loc['长拿', '较长拿水下缩短交易日'], 0)
        self.assertEqual(metrics.loc['长拿', '落后长拿时间占比%'], 0)
        self.assertEqual(set(metrics.index), set(curves))

    def test_rolling_windows_preserve_horizon_and_do_not_carry_accounts(self):
        dates = pd.bdate_range('2018-01-01', '2021-12-31')
        close = np.linspace(10, 20, len(dates))
        history = pd.DataFrame({k: close for k in ['open', 'high', 'low', 'close']}, index=dates)
        cohorts = h.rolling_cohorts(history, {'长拿': b.sim_buyhold, '对照': b.sim_buyhold}, years=1)
        self.assertGreater(len(cohorts), 0)
        np.testing.assert_allclose(cohorts['较长拿年化差pp'], 0)
        np.testing.assert_allclose(cohorts['较长拿水下缩短交易日'], 0)
        self.assertTrue((cohorts.end < cohorts.start + pd.DateOffset(years=1)).all())
        self.assertTrue((cohorts.end >= cohorts.start + pd.DateOffset(months=11)).all())

    def test_short_asset_exports_full_metrics_without_inventing_long_windows(self):
        dates = pd.bdate_range('2019-01-01', periods=260)
        close = np.linspace(10, 12, len(dates))
        history = pd.DataFrame({'open': close, 'high': close + .1,
                                'low': close - .1, 'close': close}, index=dates)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            history.rename_axis('date').to_csv(folder / 'etf_hfq_510001.csv')
            with patch.object(b, 'ASSETS', (AssetSpec('510001', '测试ETF', '000999'),)), \
                    patch.object(b, 'PE_CACHE', folder), patch.object(h, 'OUT', folder / 'experience'), \
                    patch.object(grid, 'OUT', folder / 'grid'), \
                    patch.object(b, 'load_pe', side_effect=AssertionError('无PE配置不得读取')), \
                    redirect_stdout(io.StringIO()):
                h.main()
                grid.main()
            metrics = pd.read_csv(folder / 'experience/metrics_all.csv')
            self.assertNotIn('估值', set(metrics.strategy))
            self.assertIn('最长水下交易日', metrics.columns)
            self.assertTrue(pd.read_csv(folder / 'experience/rolling_summary.csv').empty)
            self.assertIn('窗口：', (folder / 'experience/summary.md').read_text(encoding='utf-8'))
            manifest = json.loads((folder / 'experience/run_manifest.json').read_text(encoding='utf-8'))
            self.assertIn('experience.py', manifest['hashes'])
            frequencies = pd.read_csv(folder / 'grid/metrics_by_frequency.csv')
            self.assertEqual(len(frequencies), 3)
            self.assertIn('较长拿水下缩短交易日', frequencies.columns)


if __name__ == '__main__':
    unittest.main()
