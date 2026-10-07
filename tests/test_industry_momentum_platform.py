"""Exercise the real standalone JoinQuant scripts with controlled order fills."""
import ast
import builtins
from datetime import datetime
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1] / 'research/studies/industry_momentum'
LEDGER_SCRIPTS = sorted(p.name for p in ROOT.glob('lM_v3*.py')
                        if 'g.trades' in p.read_text(encoding='utf-8'))


def legacy_numpy_sum(values):
    # Older NumPy accepts generators but treats dict_values as a scalar object.
    if hasattr(values, '__next__'):
        return builtins.sum(values)
    return np.sum(values)


def load_script(name):
    tree = ast.parse((ROOT / name).read_text(encoding='utf-8'))
    tree.body = [n for n in tree.body if not isinstance(n, ast.ImportFrom)]
    ns = {}
    exec(compile(tree, name, 'exec'), ns)
    return ns


def scenario(name, fills):
    ns = load_script(name)
    code = '159001.XSHE'
    pos = SimpleNamespace(total_amount=1000, avg_cost=10.0, value=9000.0)
    context = SimpleNamespace(current_dt=datetime(2026, 7, 2, 14, 55),
        portfolio=SimpleNamespace(positions={code: pos}, available_cash=0.0,
                                  total_value=9000.0, starting_cash=10000.0))
    g = SimpleNamespace(pool=[code], day=1, trades=[], neg_run={code: 5},
                        be={code: True}, peak={code: 20.0}, peaks={code: 20.0}, ban={})
    messages = []
    orders = {}
    ns.update(g=g, log=SimpleNamespace(info=messages.append),
              get_security_name=lambda c: c,
              get_current_data=lambda: {code: SimpleNamespace(last_price=9.0, paused=False)},
              history=lambda count, unit, field, security_list: pd.DataFrame(
                  {code: [10.0] * count}),
              attribute_history=lambda security, count, *a, **kw: pd.DataFrame({'close': [10.0] * count}),
              momentum_score=lambda *a: -0.01,
              get_orders=lambda: orders)
    # Ranking variants need this held ETF outside their exit band.
    ns['EXIT_PCT'] = 0.0
    ns['TOPN'] = 1
    def sell(security, target):
        spec = fills.pop(0)
        if spec is None:
            return None
        amount, price, fee = spec
        oid = str(len(orders) + 1)
        result = SimpleNamespace(order_id=oid, filled=amount, price=price,
                                 avg_cost=pos.avg_cost, commission=fee)
        pos.total_amount -= amount
        if pos.total_amount == 0:
            pos.avg_cost = 0.0  # Platform position objects can mutate after the order.
        orders[oid] = result
        return result
    ns['order_target'] = sell
    ns['order'] = lambda *a: pytest.fail('No eligible buy in this scenario')
    return ns, context, code, orders, messages


@pytest.mark.parametrize('name', LEDGER_SCRIPTS)
@pytest.mark.parametrize('fill', [None, (0, 0.0, 0.0)])
def test_rejected_sell_does_not_record_trade_or_clear_exit_state(name, fill):
    ns, context, code, _, _ = scenario(name, [fill])
    ns['run_rotation'](context)
    assert ns['g'].trades == []
    assert ns['g'].be[code] is True
    assert ns['g'].neg_run[code] >= 5
    assert ns['g'].peak[code] == 20.0
    assert ns['g'].peaks[code] == 20.0
    assert code not in ns['g'].ban


@pytest.mark.parametrize('name', LEDGER_SCRIPTS)
def test_partial_sell_uses_fills_and_combines_one_closed_trade(name):
    ns, context, code, _, _ = scenario(name, [(400, 8.8, 0.352), (600, 8.7, 0.522)])
    ns['run_rotation'](context)
    assert ns['g'].trades == []
    assert ns['g'].sell_partial[code] == pytest.approx(-480.352)
    assert ns['g'].be[code] is True
    assert ns['g'].peak[code] == 20.0
    assert ns['g'].peaks[code] == 20.0
    context.current_dt = datetime(2026, 7, 3, 14, 55)
    ns['run_rotation'](context)
    assert len(ns['g'].trades) == 1
    assert ns['g'].trades[0]['pnl'] == pytest.approx(-1260.874)
    assert code not in ns['g'].sell_partial
    assert sum(f['pnl'] for f in ns['g'].sell_fills) == pytest.approx(-1260.874)
    for state in ('be', 'neg_run', 'peak', 'peaks'):
        assert code not in getattr(ns['g'], state)


@pytest.mark.parametrize('name', LEDGER_SCRIPTS)
def test_later_fill_is_reconciled_once_at_close(name):
    ns, context, code, orders, _ = scenario(name, [(400, 8.8, 0.352)])
    ns['run_rotation'](context)
    order = orders['1']
    order.filled, order.price, order.commission = 1000, 8.74, 0.874
    context.portfolio.positions[code].total_amount = 0
    ns['after_trading_end'](context)
    ns['after_trading_end'](context)
    assert len(ns['g'].trades) == 1
    assert ns['g'].trades[0]['pnl'] == pytest.approx(-1260.874)
    assert len(ns['g'].sell_fills) == 2


@pytest.mark.parametrize('name', LEDGER_SCRIPTS)
def test_fee_update_for_earlier_partial_order_updates_closed_trade(name):
    ns, context, code, orders, _ = scenario(name, [(400, 8.8, 0.0), (600, 8.7, 0.522)])
    ns['run_rotation'](context)
    context.current_dt = datetime(2026, 7, 3, 14, 55)
    ns['run_rotation'](context)
    orders['1'].commission = 0.352
    ns['after_trading_end'](context)
    ns['after_trading_end'](context)
    assert len(ns['g'].trades) == 1
    assert ns['g'].trades[0]['pnl'] == pytest.approx(-1260.874)
    assert code not in ns['g'].sell_partial


@pytest.mark.parametrize('name', ['lM_v3_2.py', 'lM_v3_2_1.py'])
def test_late_trail_fill_starts_cold_period(name):
    ns, context, code, orders, _ = scenario(name, [(0, 0.0, 0.0)])
    ns['run_rotation'](context)
    assert code not in ns['g'].ban
    orders['1'].filled, orders['1'].price, orders['1'].commission = 400, 8.8, 0.352
    context.portfolio.positions[code].total_amount = 600
    ns['after_trading_end'](context)
    assert ns['g'].ban[code] == ns['g'].day + ns['REENTRY_BAN']


@pytest.mark.parametrize('name', LEDGER_SCRIPTS)
def test_shared_ledger_cannot_drift_between_standalone_versions(name):
    baseline = (ROOT / 'lM_v3_3_2.py').read_text(encoding='utf-8')
    canonical = ast.parse(baseline.split('# BEGIN SHARED PLATFORM LEDGER\n', 1)[1]
                         .split('# END SHARED PLATFORM LEDGER', 1)[0])
    expected = {n.name: ast.dump(n) for n in canonical.body if isinstance(n, ast.FunctionDef)}
    actual_tree = ast.parse((ROOT / name).read_text(encoding='utf-8'))
    actual = {n.name: ast.dump(n) for n in actual_tree.body if isinstance(n, ast.FunctionDef)}
    assert all(actual[k] == value for k, value in expected.items())


@pytest.mark.parametrize('account_adjustment', [0.0, 300.0])
@pytest.mark.parametrize('shadow_sum', [None, np.sum, legacy_numpy_sum])
def test_report_includes_partial_realized_pnl_and_reconciles_equity(account_adjustment, shadow_sum):
    ns, context, code, _, messages = scenario('lM_v3_3_2.py', [(400, 8.8, 0.352)])
    ns['run_rotation'](context)
    if shadow_sum is not None:
        ns['sum'] = shadow_sum  # JoinQuant imports may shadow Python builtins.
    context.portfolio.positions[code].value = 5400.0
    context.portfolio.total_value = 8919.648 + account_adjustment
    ns['on_strategy_end'](context)
    assert any('完成平仓 0 笔' in line for line in messages)
    assert any('未清仓已实现 -480.35 | 全部成交已实现 -480.35' in line for line in messages)
    assert any(f'待核对账户差额 {account_adjustment:+.2f}' in line for line in messages)


@pytest.mark.parametrize('name', LEDGER_SCRIPTS)
@pytest.mark.parametrize('has_closed_trades', [False, True])
def test_report_with_numpy_sum_preserves_closed_partial_and_top3_totals(name, has_closed_trades):
    ns, context, code, _, messages = scenario(name, [(400, 8.8, 0.352)])
    ns['run_rotation'](context)
    if has_closed_trades:
        ns['g'].trades = [dict(date='2026-01-02', code=code, name=code, pnl=pnl)
                          for pnl in (200.0, -50.0, 100.0, 25.0)]
    closed = 275.0 if has_closed_trades else 0.0
    context.portfolio.positions[code].value = 5400.0
    context.portfolio.total_value = 8919.648 + closed
    ns['sum'] = np.sum
    ns['max'] = np.max
    ns['on_strategy_end'](context)
    assert any(f'合并盈亏 {closed:+.2f}' in line for line in messages)
    assert any(f'全部成交已实现 {closed-480.352:+.2f}' in line for line in messages)
    expected_top = 'TOP3=+325.00' if has_closed_trades else 'TOP3=+0.00'
    assert any(expected_top in line for line in messages)
    assert any('待核对账户差额 +0.00' in line for line in messages)


def test_original_reference_weighted_regressions_use_consistent_r2():
    path = ROOT.parent / 'etf_rotation_reference/new.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    blocks = []
    def find_blocks(node):
        for _, value in ast.iter_fields(node):
            if isinstance(value, list):
                for i, child in enumerate(value):
                    if isinstance(child, ast.Assign) and any(isinstance(t, ast.Name)
                            and t.id == 'ss_res' for t in child.targets):
                        end = next(j for j in range(i, len(value))
                                   if isinstance(value[j], ast.Assign) and any(
                                   isinstance(t, ast.Name) and t.id == 'r2' for t in value[j].targets))
                        blocks.append(value[i-2:end+1])
                    if isinstance(child, ast.AST):
                        find_blocks(child)
            elif isinstance(value, ast.AST):
                find_blocks(value)
    find_blocks(tree)
    assert len(blocks) == 2
    prices = np.array([1.355, 1.352, 1.339, 1.321, 1.313, 1.323, 1.290,
        1.161, 1.102, 1.133, 1.172, 1.198, 1.212, 1.207, 1.185, 1.180,
        1.177, 1.219, 1.208, 1.263, 1.246, 1.246, 1.221, 1.239, 1.284, 1.318])
    y = np.log(prices)
    x = np.arange(len(y))
    w = np.linspace(1.0, 2.0, len(y))
    slope, intercept = np.polyfit(x, y, 1, w=w)
    expected = 1 - np.sum(w**2 * (y-slope*x-intercept)**2) / np.sum(
        w**2 * (y-np.average(y, weights=w**2))**2)
    for block in blocks:
        ns = dict(np=np, w=w, weights=w, y=y, log_prices=y, x=x, x_values=x,
                  slope=slope, intercept=intercept, resid=y-slope*x-intercept)
        exec(compile(ast.Module(body=block, type_ignores=[]), 'reference_r2', 'exec'), ns)
        assert ns['r2'] == pytest.approx(expected)
        assert ns['r2'] > 0


@pytest.mark.parametrize('name', sorted(p.name for p in ROOT.glob('lM_v3*.py')))
def test_all_weighted_scores_match_correct_reference(name):
    ns = load_script(name)
    prices = np.array([1.355, 1.352, 1.339, 1.321, 1.313, 1.323, 1.290,
        1.161, 1.102, 1.133, 1.172, 1.198, 1.212, 1.207, 1.185, 1.180,
        1.177, 1.219, 1.208, 1.263, 1.246, 1.246, 1.221, 1.239, 1.284, 1.318])
    for sample in (prices, 1.0 / prices):
        y = np.log(sample)
        w = np.linspace(1.0, 2.0, len(sample))
        x = np.arange(len(sample))
        slope, intercept = np.polyfit(x, y, 1, w=w)
        ew = w ** 2
        center = np.average(y, weights=ew)
        r2 = 1 - np.sum(ew * (y - (slope*x + intercept))**2) / np.sum(ew*(y-center)**2)
        expected = round(float((math.exp(slope*250)-1)*r2), 4)
        assert ns['momentum_score'](sample[:-1], sample[-1]) == expected
