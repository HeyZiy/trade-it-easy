"""Exercise asset configuration through fetch and level1 entrypoints."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.studies.timing.assets import ASSETS, AssetSpec, ValuationSource
from research.studies.timing import run_level1 as l1
from research.studies.timing import fetch_data as f


class TimingAssetTests(unittest.TestCase):
    def test_existing_fetch_routes_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(f, 'ETF_DIR', Path(tmp)), \
                patch.object(f, 'PE_DIR', Path(tmp)), patch.object(f, 'write_manifests'), \
                patch.object(f, 'fetch_etf_hfq') as etf, \
                patch.object(f, 'fetch_pe_akshare') as ak, patch.object(f, 'fetch_pe_direct') as direct:
            f.main(tuple(a for a in ASSETS if a.etf_code in {'510300', '510500', '159915'}))
        self.assertEqual([call.args for call in etf.call_args_list],
                         [('510300',), ('510500',), ('159915',)])
        self.assertEqual([call.args for call in ak.call_args_list],
                         [('000300', '沪深300'), ('000905', '中证500')])
        direct.assert_called_once_with('399006', '399006.SZ')

    def test_new_assets_skip_optional_pe_and_share_index_fetch(self):
        source = ValuationSource('direct', '000999.SH')
        assets = (AssetSpec('510001', '测试ETF甲', '000999', source),
                  AssetSpec('510002', '测试ETF乙', '000999', source),
                  AssetSpec('510003', '测试ETF丙', '000998'))
        with tempfile.TemporaryDirectory() as tmp, patch.object(f, 'ETF_DIR', Path(tmp)), \
                patch.object(f, 'PE_DIR', Path(tmp)), patch.object(f, 'write_manifests'), \
                patch.object(f, 'fetch_etf_hfq') as etf, \
                patch.object(f, 'fetch_pe_akshare') as ak, patch.object(f, 'fetch_pe_direct') as direct:
            f.main(assets)
        self.assertEqual(etf.call_count, 3)
        ak.assert_not_called()
        direct.assert_called_once_with('000999', '000999.SH')

    def test_conflicting_index_sources_fail_before_fetch(self):
        assets = (AssetSpec('510001', '测试ETF甲', '000999', ValuationSource('direct', '000999.SH')),
                  AssetSpec('510002', '测试ETF乙', '000999', ValuationSource('akshare', '测试指数')))
        with patch.object(f, 'fetch_etf_hfq') as etf:
            with self.assertRaisesRegex(ValueError, '不同的估值'):
                f.main(assets)
        etf.assert_not_called()

    def test_asset_without_pe_runs_price_strategies_only(self):
        dates = pd.bdate_range('2019-01-01', periods=260)
        close = np.linspace(10, 12, len(dates))
        history = pd.DataFrame({'open': close, 'high': close + .1,
                                'low': close - .1, 'close': close}, index=dates)
        runners = l1.strategy_runners(history, None)
        met, _, _ = l1.evaluate_window(history.loc[l1.base.window_start(history):], runners)
        self.assertEqual(set(met.index), {'长拿', '网格', '月末MA200', '唐奇安20/10',
                                          '唐奇安55/20', l1.ATR_CASE})


if __name__ == '__main__':
    unittest.main()
