"""Exercise asset configuration through fetch and backtest entrypoints."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.studies.broad_index_strategies.assets import ASSETS, AssetSpec, ValuationSource
from research.studies.broad_index_strategies.common import backtest as b
from research.studies.broad_index_strategies import fetch_data as f


class BroadIndexAssetTests(unittest.TestCase):
    def test_existing_fetch_routes_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(f, 'CACHE', Path(tmp)), \
                patch.object(f, 'fetch_etf_hfq') as etf, \
                patch.object(f, 'fetch_pe_akshare') as ak, patch.object(f, 'fetch_pe_direct') as direct:
            f.main(tuple(a for a in ASSETS if a.etf_code in {'510300', '510500', '159915'}))
        self.assertEqual([call.args for call in etf.call_args_list],
                         [('510300',), ('510500',), ('159915',)])
        self.assertEqual([call.args for call in ak.call_args_list],
                         [('000300', '沪深300'), ('000905', '中证500')])
        direct.assert_called_once_with('399006', '399006.SZ')
        self.assertEqual(b.ETF_INDEX, {asset.etf_code: asset.index_code for asset in ASSETS})

    def test_new_assets_skip_optional_pe_and_share_index_fetch(self):
        source = ValuationSource('direct', '000999.SH')
        assets = (AssetSpec('510001', '测试ETF甲', '000999', source),
                  AssetSpec('510002', '测试ETF乙', '000999', source),
                  AssetSpec('510003', '测试ETF丙', '000998'))
        with tempfile.TemporaryDirectory() as tmp, patch.object(f, 'CACHE', Path(tmp)), \
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

    def test_main_runs_price_strategies_for_asset_without_pe(self):
        dates = pd.bdate_range('2019-01-01', periods=260)
        close = np.linspace(10, 12, len(dates))
        history = pd.DataFrame({'open': close, 'high': close + .1,
                                'low': close - .1, 'close': close}, index=dates)
        asset = AssetSpec('510001', '测试ETF', '000999')
        with tempfile.TemporaryDirectory() as tmp, patch.object(b, 'OUT', Path(tmp)), \
                patch.object(b, 'ASSETS', (asset,)), \
                patch.object(b, 'load_etf', return_value=history), \
                patch.object(b, 'load_pe', side_effect=AssertionError('未配置PE不得读取PE缓存')), \
                patch.object(b, 'sensitivity_510300', side_effect=AssertionError('未配置300不得运行专属附录')), \
                redirect_stdout(io.StringIO()):
            b.main()
            metrics = pd.read_csv(Path(tmp) / 'metrics_510001.csv', index_col=0)
        self.assertEqual(set(metrics.index), {'长拿', '网格', '趋势'})


if __name__ == '__main__':
    unittest.main()
