"""etf_pool 建池模块的离线行为验证;平台接口以桩替,不验证收益。"""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import pandas as pd
import pytest

sys.modules.setdefault('jqdata', ModuleType('jqdata'))
spec = importlib.util.spec_from_file_location(
    'etf_pool_under_test',
    Path(__file__).resolve().parents[1] / 'research/studies/etf/etf_pool.py')


def load():
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def world(m, secs, maps, money):
    """桩替平台接口;代码与公告日过滤下推到 finance.run_query,由桩模拟。"""
    calls = []

    class Field:
        def in_(self, values):
            calls.append(('codes', list(values)))
            self.codes = list(values)
            return self

        def __le__(self, value):
            calls.append(('pub_date', value))
            self.cutoff = value
            return self

    table = NS(code=Field(), pub_date=Field())

    def run_query(q):
        out = maps.copy()
        for field in q:
            if hasattr(field, 'codes'):
                out = out[out['code'].isin(field.codes)]
            if hasattr(field, 'cutoff'):
                out = out[pd.to_datetime(out['pub_date']) <= pd.Timestamp(field.cutoff)]
        return out

    m.finance = NS(FUND_INVEST_TARGET=table, run_query=run_query)
    m.query = lambda t: NS(filter=lambda *filters: filters)
    m.get_all_securities = lambda types, date: (calls.append(('securities', types, date)) or secs)

    def prices(codes, **kwargs):
        calls.append(('price', kwargs))
        return pd.concat([pd.DataFrame({'time': money[c].index, 'code': c,
                                        'money': money[c].values})
                          for c in codes], ignore_index=True)

    m.get_price = prices
    m.log = NS(info=lambda text: calls.append(('log', text)))
    return calls


def pool_inputs(gold='01'):
    """四只ETF:官方代码A、名称解析B、非六位官方代码HSI、完全未映射;再加黄金G。"""
    rows = [('A', '沪深300ETF', '000300.XSHG', '沪深300'),
            ('B', '沪深300增强ETF', None, '沪深300（000300）'),
            ('C', '恒生ETF', 'HSI', '恒生指数'),
            ('U', '未映射ETF', None, None),
            ('G', '黄金ETF', None, None)]
    secs = pd.DataFrame({'type': 'etf', 'display_name': [r[1] for r in rows],
                         'start_date': pd.Timestamp('2020-01-01')},
                        index=[r[0] for r in rows])
    maps = pd.DataFrame([dict(code=r[0], pub_date='2026-01-01',
                              start_date='2026-01-01', end_date=None,
                              traced_index_code=r[2], traced_index_name=r[3])
                         for r in rows])
    sessions = pd.bdate_range(end='2026-11-06', periods=20)
    money = {c: pd.Series(5e7, index=sessions) for c in secs.index}
    money['B'] *= 2  # 同组B均额更高,去重应留B
    return secs, maps, money, sessions


def test_official_code_strips_known_suffix_and_dedupes_by_pool_key():
    m = load()
    secs, maps, money, _ = pool_inputs()
    world(m, secs, maps, money)
    rows = m.build_pool('2026-11-06').to_dict('records')
    got = {r['code']: r for r in rows}
    # 默认政策:未映射全部剔除(含黄金G);A被同组均额更高的B替代。
    assert set(got) == {'B', 'C'}
    assert got['B']['pool_key'] == got['B']['index_key'] == '000300'
    assert got['B']['mapping_source'] == 'parsed_code'
    assert got['C']['index_key'] == 'HSI' and got['C']['mapping_source'] == 'unmapped_code'
    assert [r['code'] for r in rows] == ['B', 'C']  # 按均额降序
    assert got['B']['avg_money'] == pytest.approx(1e8)


def test_manual_groups_merge_unmapped_gold_by_explicit_codes():
    m = load()
    secs, maps, money, _ = pool_inputs()
    world(m, secs, maps, money)
    rows = m.build_pool('2026-11-06',
                        manual_groups={'MANUAL:GOLD': ['G']}).to_dict('records')
    got = {r['code']: r for r in rows}
    assert got['G']['pool_key'] == 'MANUAL:GOLD'
    assert got['G']['mapping_source'] == 'manual'
    assert got['G']['index_key'] == 'MANUAL:GOLD'


def test_keep_unmapped_gives_each_its_own_group():
    m = load()
    secs, maps, money, _ = pool_inputs()
    world(m, secs, maps, money)
    rows = m.build_pool('2026-11-06', keep_unmapped=True).to_dict('records')
    got = {r['code']: r for r in rows}
    assert set(got) == {'B', 'C', 'G', 'U'}
    assert got['U']['pool_key'] == 'U' and got['G']['pool_key'] == 'G'


def test_cash_like_removed_even_when_mapped():
    m = load()
    secs, maps, money, _ = pool_inputs()
    secs.loc['C', 'display_name'] = '货币ETF'
    world(m, secs, maps, money)
    assert [r['code'] for r in m.build_pool('2026-11-06').to_dict('records')] == ['B']


def test_classify_etfs_keeps_everything_for_inventory():
    m = load()
    secs, maps, money, _ = pool_inputs()
    world(m, secs, maps, money)
    etfs = m.classify_etfs('2026-11-06')
    assert len(etfs) == 5
    assert etfs.loc['U', 'mapping_source'] == 'unmapped'
    assert pd.isna(etfs.loc['G', 'pool_key'])
    assert not bool(etfs.loc['B', 'cash_like']) and not bool(etfs.loc['U', 'cash_like'])


@pytest.mark.parametrize('change', ['future_pub', 'future_effective', 'expired', 'young', 'low_money'])
def test_pool_rejects_ineligible(change):
    m = load()
    secs, maps, money, _ = pool_inputs()
    secs = secs.loc[['A']]
    maps = maps[maps.code == 'A'].copy()
    if change == 'future_pub':
        maps['pub_date'] = '2026-11-07'
    elif change == 'future_effective':
        maps['start_date'] = '2026-11-07'
    elif change == 'expired':
        maps['end_date'] = '2026-11-06'
    elif change == 'young':
        secs.loc['A', 'start_date'] = pd.Timestamp('2026-01-01')
    else:
        money['A'] *= .1
    world(m, secs, maps, money)
    assert m.build_pool('2026-11-06').empty


def test_latest_publication_must_also_be_effective():
    m = load()
    secs, maps, money, _ = pool_inputs()
    secs = secs.loc[['A']]
    maps = maps[maps.code == 'A'].copy()
    later = maps.copy()
    later['pub_date'], later['start_date'], later['traced_index_code'] = \
        '2026-10-01', '2026-12-01', '000905'
    maps = pd.concat([maps, later])
    world(m, secs, maps, money)
    rows = m.build_pool('2026-11-06').to_dict('records')
    assert [r['code'] for r in rows] == ['A'] and rows[0]['index_key'] == '000300'


def test_batches_kept_small():
    m = load()
    secs, maps, money, _ = pool_inputs()
    calls = world(m, secs, maps, money)
    m.build_pool('2026-11-06')
    code_calls = [v for v in calls if v[0] == 'codes']
    assert code_calls and max(len(v[1]) for v in code_calls) <= m.BATCH_SIZE
    price_call = next(v[1] for v in calls if v[0] == 'price')
    assert price_call['count'] == 20 and price_call['end_date'] == '2026-11-06'
    assert ('securities', ['fund'], '2026-11-06') in calls
    assert ('pub_date', '2026-11-06') in calls
