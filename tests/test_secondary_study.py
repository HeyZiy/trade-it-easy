"""固定二级规则、一级对照一致性及短样本导出。"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.studies.broad_index_strategies import run_level2 as s
from research.studies.broad_index_strategies.assets import AssetSpec


def history():
    dates = pd.bdate_range('2018-01-01', periods=500)
    close = np.linspace(10, 13, len(dates)) + np.sin(np.arange(len(dates)) / 8)
    return pd.DataFrame({'open': close, 'high': close + .2,
                         'low': close - .2, 'close': close}, index=dates)


class SecondaryStudyTests(unittest.TestCase):
    def test_both_script_entries_run_with_relocated_shared_modules(self):
        study = Path(s.__file__).resolve().parent
        prices = history().iloc[:260]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            prices.rename_axis('date').to_csv(folder / 'etf_hfq_513180.csv')
            with patch.object(s.base, 'STUDY', folder), patch.object(s.base, 'PE_CACHE', folder), \
                    patch.object(s.base, 'ASSETS', (AssetSpec('513180', '恒生科技ETF', ''),)), \
                    patch.object(sys, 'path', sys.path.copy()), redirect_stdout(io.StringIO()):
                runpy.run_path(str(study / 'run_level1.py'), run_name='__main__')
                runpy.run_path(str(study / 'run_level2.py'), run_name='__main__')
            self.assertTrue((folder / 'results/level1/run_manifest.json').is_file())
            self.assertTrue((folder / 'results/level2/513180/run_manifest.json').is_file())

    def test_fixed_presets_preserve_first_level_accounts(self):
        px_all = history()
        px = px_all.loc[s.base.window_start(px_all):]
        runners = s.build_runners(px_all)
        self.assertEqual(list(runners), list(s.PRESETS))
        self.assertEqual(len(runners), 14)
        for name, run in s.holding.strategy_runners(px_all).items():
            expected, count = run(px)
            actual, actual_count = runners[name](px)
            self.assertEqual(actual_count, count)
            pd.testing.assert_frame_equal(actual, expected)

    def test_export_keeps_fixed_rules_and_leaves_unavailable_horizons_empty(self):
        prices = history()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            prices.rename_axis('date').to_csv(folder / 'etf_hfq_513180.csv')
            with patch.object(s, 'OUT', folder / 'out'), patch.object(s.base, 'PE_CACHE', folder), \
                    patch.object(s.base, 'ASSETS', (AssetSpec('513180', '恒生科技ETF', ''),)), \
                    redirect_stdout(io.StringIO()):
                s.main()
            metrics = pd.read_csv(folder / 'out/metrics.csv', index_col=0)
            self.assertEqual(set(metrics.index), set(s.PRESETS))
            roll = pd.read_csv(folder / 'out/rolling_summary.csv')
            self.assertEqual(set(roll.years), {1})
            self.assertEqual(set(roll.strategy), set(s.PRESETS) - {'长拿'})
            manifest = json.loads((folder / 'out/run_manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(set(manifest['presets']), set(s.PRESETS))
            self.assertFalse(manifest['select_best_parameter'])
            self.assertIn('历史长度不足，没有完整窗口', (folder / 'out/summary.md').read_text(encoding='utf-8'))
            self.assertGreater(len(pd.read_csv(folder / 'out/trades.csv')), 0)


if __name__ == '__main__':
    unittest.main()
