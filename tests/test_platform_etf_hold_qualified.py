"""独立验证取消排名退出的行为，不混入信号、费用或仓位制度变化。"""
import ast
import copy
import importlib.util
import sys
import types
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest


FOLDER = Path(__file__).resolve().parents[1] / 'research/studies/etf_rotation'
SCRIPT = FOLDER / 'unified_hold_qualified.py'


@pytest.fixture
def world(monkeypatch):
    api = types.ModuleType('jqdata')
    api.g = types.SimpleNamespace(last_position=None)
    monkeypatch.setitem(sys.modules, 'jqdata', api)
    spec = importlib.util.spec_from_file_location('hold_qualified_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.log = types.SimpleNamespace(info=lambda message: None)
    portfolio = types.SimpleNamespace(positions={}, available_cash=100000.0)
    context = types.SimpleNamespace(current_dt=datetime(2016, 1, 4, 10, 40),
                                    portfolio=portfolio)
    strategy = module.EtfRotationStrategy(context)
    scores = dict.fromkeys(strategy.etf_pool, np.nan)
    strategy.calculate_momentum_score = scores.__getitem__
    strategy._cur = lambda: {}
    strategy.get_stock_name = lambda code: code
    orders = []

    def order(code, target_value):
        orders.append((code, target_value))
        if target_value == 0:
            portfolio.positions.pop(code, None)
            portfolio.available_cash = 100000.0
        else:
            portfolio.positions[code] = types.SimpleNamespace(total_amount=100)
            portfolio.available_cash = 0.0

    strategy.order_target_value_safe = order
    return types.SimpleNamespace(module=module, strategy=strategy, scores=scores,
                                 portfolio=portfolio, orders=orders,
                                 first=strategy.etf_pool[0], second=strategy.etf_pool[1])


def test_only_rule_change_is_qualified_holding_early_return():
    reference = ast.parse((FOLDER / 'study_etf_fit_jq.py').read_text(encoding='utf-8'))
    changed = ast.parse(SCRIPT.read_text(encoding='utf-8'))
    original = ast.Module(body=reference.body[:len(changed.body)], type_ignores=[])

    def pool_assignment(tree):
        return next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == 'etf_pool'
                            for t in node.targets))

    original_pool, hold_pool = pool_assignment(original), pool_assignment(changed)
    # 历史11只对照仍保留；15只排名版仅额外加入这四只。
    additions = {'512100.XSHG', '588000.XSHG', '159980.XSHE', '159985.XSHE'}
    assert len(ast.literal_eval(original_pool.value)) == 15
    assert len(ast.literal_eval(hold_pool.value)) == 11
    assert ast.literal_eval(hold_pool.value) == [
        code for code in ast.literal_eval(original_pool.value) if code not in additions]
    original_pool.value = copy.deepcopy(hold_pool.value)
    normalized = copy.deepcopy(changed)
    method = next(node for node in ast.walk(normalized)
                  if isinstance(node, ast.FunctionDef) and node.name == 'execute_trade')
    addition = ast.parse('''if current_holding and current_holding in candidates:
    g.last_position = current_holding
    return
''').body[0]
    matching = [node for node in method.body if ast.dump(node) == ast.dump(addition)]
    assert len(matching) == 1
    method.body.remove(matching[0])
    assert ast.dump(original) == ast.dump(normalized)


def test_empty_account_buys_first_qualified_asset(world):
    w = world
    w.scores[w.first], w.scores[w.second] = .2, .4
    w.strategy.execute_trade()
    assert w.orders == [(w.second, 100000.0)]
    assert w.module.g.last_position == w.second


def test_rank_drop_retains_position_then_disqualification_switches(world):
    w = world
    w.scores[w.first], w.scores[w.second] = .4, .2
    w.strategy.execute_trade()
    assert w.orders == [(w.first, 100000.0)]
    w.scores[w.second] = .8
    w.strategy.execute_trade()
    assert w.orders == [(w.first, 100000.0)]
    assert w.module.g.last_position == w.first
    w.scores[w.first] = 0.0
    w.strategy.execute_trade()
    assert w.orders[-2:] == [(w.first, 0), (w.second, 100000.0)]
    assert w.module.g.last_position == w.second
    w.scores[w.second] = -.1
    w.strategy.execute_trade()
    assert w.orders[-1] == (w.second, 0)
    assert not w.portfolio.positions
    assert w.module.g.last_position is None


@pytest.mark.parametrize('disqualified_score', [0.0, -.1, 5.0, 5.1, np.nan])
def test_original_bounds_and_missing_data_still_force_exit(world, disqualified_score):
    w = world
    w.portfolio.positions[w.first] = types.SimpleNamespace(total_amount=100)
    w.module.g.last_position = w.first
    w.scores[w.first] = disqualified_score
    w.strategy.execute_trade()
    assert w.orders == [(w.first, 0)]
    assert w.module.g.last_position is None


def test_empty_candidate_set_and_empty_account_do_not_trade(world):
    world.strategy.execute_trade()
    assert world.orders == []
    assert world.module.g.last_position is None


def test_existing_winner_does_not_top_up_with_spare_cash(world):
    w = world
    w.portfolio.positions[w.first] = types.SimpleNamespace(total_amount=100)
    w.scores[w.first] = .3
    w.strategy.execute_trade()
    assert w.orders == []
    assert w.module.g.last_position == w.first
