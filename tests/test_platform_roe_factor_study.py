"""单文件聚宽因子诊断：分时点计算，复权端点和停牌缺测，不下单。"""
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_platform_roe_rotation_revisions import AsOfJQStub


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'research/tools/single_factor/roe_factor_study_jq.py'


def world(split=False, entry_paused=False):
    cal = pd.bdate_range('2025-01-02', periods=130)
    codes = ['600%03d.XSHG' % i for i in range(24)]
    closes = {c: pd.Series(10 * (1 + (i + 1) * .0002) ** np.arange(130), index=cal)
              for i, c in enumerate(codes)}
    closes['000300.XSHG'] = pd.Series(100.0, index=cal)
    if split:
        closes[codes[0]].iloc[80:] /= 2
    stub = AsOfJQStub(cal, closes).install()
    stub.set_date_table(pd.DataFrame({'code': codes, 'roe': 4.0,
        'inc_net_profit_year_on_year': 20.0, 'statDate': '2024-09-30',
        'pubDate': '2024-10-31'}), table='indicator')
    stub.set_date_table(pd.DataFrame({'code': codes, 'pe_ratio': 20.0,
        'pb_ratio': 3.0, 'market_cap': np.arange(24) + 10}), table='valuation')
    if entry_paused:
        stub.money[codes[-1]].iloc[66] = 0
    mod = stub.load_script(SCRIPT)
    requests, recorded = [], []

    def get_price(securities, **kwargs):
        entry = pd.Timestamp(kwargs['start_date'])
        # 只允许在收益区间完成后读取已过去的入场开盘，不许查询未来。
        assert entry == pd.Timestamp(kwargs['end_date']) < stub.today
        assert kwargs['fields'] == ['open'] and kwargs['fq'] == 'pre'
        requests.append((stub.today, entry))
        rows = []
        for code in securities:
            value = stub.closes[code].loc[entry]
            if split and code == codes[0] and stub.today >= cal[80] and entry < cal[80]:
                value /= 2  # 模拟以退出日为基准的前复权历史开盘
            rows.append({'code': code, 'time': entry, 'open': value})
        return pd.DataFrame(rows)

    mod.get_price = get_price
    mod.get_industry = lambda securities, date: {
        c: {'sw_l1': {'industry_code': 'A' if i % 2 else 'B'}}
        for i, c in enumerate(securities)}
    mod.record = lambda **values: recorded.append(values)
    mod.log.warn = mod.log.info
    stub.initialize()
    return stub, mod, requests, recorded, codes


def test_single_file_completes_periods_without_future_queries_or_orders():
    stub, mod, requests, recorded, codes = world()
    stub.run_days(stub.calendar[65:108])
    assert stub.orders == []
    assert len(requests) == 2
    assert len(stub.g.factor_signals) == 3
    for mode in stub.g.factor_modes:
        assert len(stub.g.factor_rows[mode]) == 2
    row = stub.g.factor_rows['M1_raw'][0]
    assert row['signal'] == stub.calendar[65]
    assert row['asof'] == stub.calendar[64]
    assert row['entry'] == stub.calendar[66]
    assert row['exit'] == stub.calendar[86]
    assert row['ic'] == pytest.approx(1)
    assert row['high_minus_low'] > 0
    assert row['coverage'] == 1
    assert recorded and all(np.isfinite(list(values.values())).all() for values in recorded)
    mod.on_strategy_end(stub.ctx)
    assert any('报价口径' in line for line in stub.logs)


def test_returns_requery_dynamic_adjusted_entry_instead_of_raw_price_ratio():
    stub, mod, requests, recorded, codes = world(split=True)
    stub.run_days(stub.calendar[65:87])
    row = stub.g.factor_rows['M1_raw'][0]
    # 最低组5只，不应把2拆1误算成第1只亏损50%。
    expected = np.mean([(1 + (i + 1) * .0002) ** 20 - 1 for i in range(5)])
    assert row['G1'] == pytest.approx(expected)


def test_entry_suspension_does_not_remove_stock_or_reassign_groups():
    stub, mod, requests, recorded, codes = world(entry_paused=True)
    stub.run_days(stub.calendar[65:87])
    row = stub.g.factor_rows['M1_raw'][0]
    assert row['pool_n'] == 24
    assert row['paired_n'] == 23
    assert row['coverage'] == pytest.approx(23 / 24)
    assert row['G5_n'] == 5
    assert np.isnan(row['G5'])
    assert np.isnan(row['high_minus_low'])
    assert 'M1原始_本期高减低_pp' not in stub.g.factor_plot


def test_candidate_pool_matches_original_on_same_signal_date():
    stub, mod, requests, recorded, codes = world()
    stub.today = stub.calendar[65]
    stub.ctx.current_dt = stub.today
    actual = mod.build_signal(stub.ctx)
    original = stub.load_script(ROOT / 'research/studies/roe_quality/roe_rotation_v1_1_2.py')
    assert original.build_signal(stub.ctx) == actual


def test_numpy_name_collision_does_not_reject_valid_default_windows():
    stub, mod, requests, recorded, codes = world()
    # 聚宽通配导入可能带入numpy.any；它不按内置any的方式消费生成器。
    mod.any = np.any
    mod.initialize(stub.ctx)
    assert mod.FACTOR_WINDOWS == (1, 5)


@pytest.mark.parametrize('windows', [(0, 5), (1, 61), (True, 5), (1, 5.5), ()])
def test_invalid_windows_are_still_rejected_with_numpy_name_collision(windows):
    stub, mod, requests, recorded, codes = world()
    mod.any = np.any
    mod.FACTOR_WINDOWS = windows
    with pytest.raises(ValueError, match='FACTOR_WINDOWS'):
        mod.initialize(stub.ctx)


def test_full_diagnostic_tolerates_numpy_shadowing_of_builtin_names():
    stub, mod, requests, recorded, codes = world()
    for name, value in {'any': np.any, 'all': np.all, 'max': np.max,
                        'min': np.min, 'sum': np.sum, 'round': np.round,
                        'int': np.int64, 'float': np.float64, 'bool': np.bool_}.items():
        setattr(mod, name, value)
    mod.initialize(stub.ctx)
    stub.run_days(stub.calendar[65:87])
    assert stub.g.factor_rows['M1_raw'][0]['ic'] == pytest.approx(1)
    mod.on_strategy_end(stub.ctx)
    assert any('报价口径' in line for line in stub.logs)


def test_neutralization_and_summary_do_not_require_pandas_to_numpy(monkeypatch):
    stub, mod, requests, recorded, codes = world()

    def unavailable(*args, **kwargs):
        raise AttributeError("'Series' object has no attribute 'to_numpy'")

    # 重现脚本所依赖的公开接口限制；不把现代pandas内部实现当成旧版实现。
    monkeypatch.setattr(pd.Series, 'to_numpy', unavailable)
    monkeypatch.setattr(pd.DataFrame, 'to_numpy', unavailable)
    stub.run_days([stub.calendar[65]])
    assert stub.g.factor_pending['factors']['M1_neutral'].notna().sum() == 24
    summary = mod.summarize_ic(pd.Series([.1, .2, .05, .15]))
    assert summary['mean'] == pytest.approx(.125)
    assert np.isfinite(summary['t_hac'])
    mod.on_strategy_end(stub.ctx)
