"""聚宽副图按实际成交和持仓统计，不改变交易或年度收益口径。"""
import ast
import importlib.util
import math
import sys
import types
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest


FOLDER = Path(__file__).resolve().parents[1] / 'research/studies/etf_rotation'
SCRIPTS = ['unified_coverage.py', '../industry_momentum/unified_industry22.py', 'unified_energy17.py',
           'unified_hold_qualified.py', 'study_etf_fit_jq.py',
           'same_pool_baseline.py']


@pytest.fixture(params=SCRIPTS)
def world(request, monkeypatch):
    api = types.ModuleType('jqdata')
    api.g = types.SimpleNamespace()
    api.sum, api.min, api.max = np.sum, np.min, np.max
    monkeypatch.setitem(sys.modules, 'jqdata', api)
    spec = importlib.util.spec_from_file_location('chart_test', FOLDER / request.param)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    errors, records = [], []
    module.log = types.SimpleNamespace(info=lambda *args: None, error=errors.append)
    for name in ('set_benchmark', 'set_option', 'set_order_cost', 'set_slippage',
                 'run_daily'):
        setattr(module, name, lambda *args, **kwargs: None)
    module.OrderCost = lambda **kwargs: kwargs
    module.FixedSlippage = module.PriceRelatedSlippage = lambda value: value
    module.record = lambda **kwargs: records.append(kwargs)
    module.get_trades = lambda: {}
    portfolio = types.SimpleNamespace(starting_cash=100000., total_value=100000.,
                                      positions={})
    context = types.SimpleNamespace(current_dt=datetime(2024, 1, 2, 15, 1),
                                    portfolio=portfolio)
    module.initialize(context)
    return types.SimpleNamespace(module=module, context=context,
                                 records=records, errors=errors)


@pytest.mark.parametrize('amounts,expected', [([], 0), ([100000], 50),
                                           ([100000, 100000], 100),
                                           ([3000, 7000, 10000], 10)])
def test_turnover_uses_actual_fills_and_previous_equity(world, amounts, expected):
    w = world
    w.module.get_trades = lambda: {
        i: types.SimpleNamespace(price=1., amount=amount)
        for i, amount in enumerate(amounts)
    }
    # 当天净值与前收不同，分母仍须用前收；实际仓位也不能由目标仓位推算。
    w.context.portfolio.total_value = 120000.
    w.context.portfolio.positions = {
        'A': types.SimpleNamespace(total_amount=100, value=30000.),
        'B': types.SimpleNamespace(total_amount=0, value=99999.),
    }
    w.module.after_market_close(w.context)
    assert w.records[-1] == {'单边换手%': expected, '实际仓位%': 25.}
    if hasattr(w.module.g, 'yearly_performance'):
        performance = w.module.g.yearly_performance[2024]
        assert performance['start_value'] == 100000.
        assert performance['end_value'] == 120000.
    else:
        assert w.module.g.year_stats[2024]['start'] == 100000.
        assert w.module.g.year_stats[2024]['end'] == 120000.
    assert not w.errors


def test_failed_trade_read_is_unknown_and_does_not_stop_annual_stats(world):
    def unavailable():
        raise RuntimeError('trades unavailable')

    w = world
    w.module.get_trades = unavailable
    w.module.after_market_close(w.context)
    assert math.isnan(w.records[-1]['单边换手%'])
    assert w.records[-1]['实际仓位%'] == 0.
    assert 'trades unavailable' in w.errors[-1]
    previous = getattr(w.module.g, 'stats_last_total_value',
                       getattr(w.module.g, 'previous_value', None))
    assert previous == 100000.


def test_missing_held_value_does_not_fabricate_cash_exposure(world):
    w = world
    w.context.portfolio.positions = {
        'A': types.SimpleNamespace(total_amount=100, value=float('nan')),
    }
    w.module.record_execution_metrics(w.context, 100000.)
    assert w.records[-1]['单边换手%'] == 0.
    assert math.isnan(w.records[-1]['实际仓位%'])
    assert w.errors


def test_all_standalone_scripts_share_the_same_chart_definition():
    definitions = []
    for name in SCRIPTS:
        tree = ast.parse((FOLDER / name).read_text(encoding='utf-8'))
        helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == 'record_execution_metrics')
        definitions.append(ast.dump(helper))
    assert len(set(definitions)) == 1
