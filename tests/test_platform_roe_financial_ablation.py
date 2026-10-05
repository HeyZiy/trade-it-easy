"""财务筛选移除实验：其余候选池/交易规则保持原基线。"""
import ast
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_platform_roe_rotation_revisions import load_world, fundamentals


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = 'roe_rotation_v1_1_5.py'


def test_disabled_filters_do_not_query_or_require_financial_data():
    codes = ['600%03d.XSHG' % i for i in range(4)]
    rows = fundamentals(codes)
    rows['roe'] = [4.0, 1.0, np.nan, 4.0]
    rows['inc_net_profit_year_on_year'] = [20.0, -20.0, np.nan, 20.0]
    rows.loc[3, 'pubDate'] = '2099-01-01'
    stub, mod = load_world(SCRIPT, rows)
    stub.date_tables['valuation'].loc[1:, ['pe_ratio', 'pb_ratio']] = [100.0, 9.0]
    original = mod.get_fundamentals
    mod.get_fundamentals = lambda *a, **k: pytest.fail('No financial queries when disabled')
    assert mod.FINANCIAL_FILTERS is False
    assert mod.build_signal(stub.ctx) == codes
    mod.get_fundamentals = original
    mod.FINANCIAL_FILTERS = True
    assert mod.build_signal(stub.ctx) == [codes[0]]


def test_st_paused_board_unlock_history_and_volatility_filters_are_preserved():
    codes = ['600%03d.XSHG' % i for i in range(6)] + ['300001.XSHE', '688001.XSHG']
    cal = pd.bdate_range('2025-01-02', periods=100)
    closes = {c: pd.Series(10.0, index=cal) for c in codes}
    closes[codes[3]] = pd.Series(10 + np.tile([1, -1], 50), index=cal)
    closes[codes[4]].iloc[:50] = np.nan
    stub, mod = load_world(SCRIPT, fundamentals(codes), closes)
    stub.set_extras('is_st', {codes[1]: True})
    stub.money[codes[2]].iloc[-1] = 0
    stub.set_locked_shares(pd.DataFrame({'code': [codes[5]],
        'day': [stub.today + pd.Timedelta(days=10)]}))
    assert mod.build_signal(stub.ctx) == [codes[0]]


def test_restoring_financial_filters_has_identical_orders_to_v1_1_2():
    codes = ['600%03d.XSHG' % i for i in range(12)]
    rows = fundamentals(codes)
    rows['pubDate'] = '2025-04-01'
    baseline, baseline_mod = load_world('roe_rotation_v1_1_2.py', rows)
    baseline.run_days(baseline.calendar[70:93])
    stub, mod = load_world(SCRIPT, rows)
    mod.FINANCIAL_FILTERS = True
    stub.run_days(stub.calendar[70:93])
    assert stub.orders == baseline.orders
    assert stub.g.hold_since == baseline.g.hold_since
    assert stub.portfolio.total_value == pytest.approx(baseline.portfolio.total_value)


def test_execution_and_attribution_functions_are_unchanged_except_builtin_qualification():
    class RestoreBuiltinNames(ast.NodeTransformer):
        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == '_python_builtins':
                return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
            return self.generic_visit(node)

    def functions(name):
        tree = ast.parse((ROOT / 'research/studies/roe_quality' / name).read_text(encoding='utf-8'))
        tree = RestoreBuiltinNames().visit(tree)
        return {f.name: ast.dump(f) for f in tree.body if isinstance(f, ast.FunctionDef)}

    base, modified = functions('roe_rotation_v1_1_2.py'), functions(SCRIPT)
    for name in ('on_open', 'on_close', 'mark_nav', 'after_trading_end',
                 '_collect_rank_orders', '_log_rank_summary', '_rank_candidates'):
        assert base[name] == modified[name]
