"""排序对照的账户隔离与原v5行为一致性；合成撮合不代表聚宽回测成绩。"""
import ast
import hashlib
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_platform_quality_pool_v2 import world, make, BASE  # noqa: E402,F401
from test_platform_pool_rotation import PoolPortfolio  # noqa: E402


SCRIPT = BASE / 'study_v5_rank_control_jq.py'


def control(world, seeds=(1, 2), mode='compare'):
    stub, original, codes, cal = make(world)
    stub._module.get_current_data = original.get_current_data
    stub._module.SubPortfolioConfig = lambda **kwargs: SimpleNamespace(**kwargs)

    def set_portfolios(configs):
        stub.ctx.subportfolios = [PoolPortfolio(stub, cfg.cash) for cfg in configs]
        for portfolio in stub.ctx.subportfolios:
            portfolio.long_positions = portfolio.positions

    stub._module.set_subportfolios = set_portfolios
    def write_file(name, content, append=False):
        stub.exports[name] = (stub.exports.get(name, '') if append else '') + content
    stub._module.write_file = write_file
    original_order = stub._order_target
    stub.routed_orders = []

    def order_target(code, amount, pindex=0):
        portfolio = stub.portfolio
        stub.portfolio = stub.ctx.subportfolios[pindex]
        cash_before, before = stub.portfolio.cash, len(stub.orders)
        try:
            result = original_order(code, amount)
            if result is not None:
                result.order_id = str(stub.oid)
                gross = result.filled * result.price
                result.commission = (cash_before - stub.portfolio.cash - gross if result.is_buy
                                     else cash_before + gross - stub.portfolio.cash)
            for row in stub.orders[before:]:
                stub.routed_orders.append((pindex,) + row)
            return result
        finally:
            stub.portfolio = portfolio

    stub._module.order_target = order_target
    stub.portfolio.starting_cash = 1_000_000 * (1 + len(seeds) if mode == 'compare' else 1)
    stub.ctx.run_params = SimpleNamespace(frequency='day', start_date=cal[270], end_date=cal[-1])
    module = stub.load_script(SCRIPT)
    module.RANDOM_SEEDS, module.RUN_MODE = seeds, mode
    module.OUTPUT_PREFIX = 'test_rank_control_' + mode
    stub.scheduled.clear()
    stub.initialize()
    return stub, module, codes, cal


def run_control(stub, dates):
    for day in dates:
        stub.today = pd.Timestamp(day).normalize()
        stub.daily_orders = {}
        for portfolio in stub.ctx.subportfolios:
            for pos in portfolio.positions.values():
                pos.closeable_amount = pos.total_amount
        for time in sorted(stub.scheduled):
            hour, minute = {'before_open': (9, 0), 'open': (9, 30)}[time]
            stub.ctx.current_dt = stub.today + pd.Timedelta(hours=hour, minutes=minute)
            stub.scheduled[time](stub.ctx)
        for portfolio in stub.ctx.subportfolios:
            for code, pos in portfolio.positions.items():
                pos.price = stub.price(code)
        stub.ctx.current_dt = stub.today + pd.Timedelta(hours=15, minutes=30)
        stub._script.after_trading_end(stub.ctx)


@pytest.mark.parametrize('mode', ['compare', 'momentum_only'])
def test_momentum_matches_original_through_partial_fills_rebalance_and_deferred_exit(world, mode):
    base, baseline, codes, cal = make(world)
    multi, module, _, _ = control(world, mode=mode)
    for stub in (base, multi):
        for i, code in enumerate(codes):
            stub.closes[code].iloc[270:] = [10 + j * (i + 1) / 500 for j in range(90)]
        stub.buy_caps[codes[0]] = 2000
    base.run_days(cal[270:290])
    run_control(multi, cal[270:290])
    for stub in (base, multi):
        stub.date_tables['valuation'].loc[0, 'pe_ratio'] = 40.
        stub.date_tables['valuation'].loc[1, 'pe_ratio'] = float('nan')
        stub.sell_caps[codes[0]] = 300
        stub.quote_overrides[codes[2]] = {'paused': True}
    base.run_days(cal[290:293])
    run_control(multi, cal[290:293])
    for stub in (base, multi):
        stub.date_tables['valuation'].loc[1, 'pe_ratio'] = 20.
        stub.quote_overrides.clear()
    base.run_days(cal[293:332])
    run_control(multi, cal[293:332])
    state = multi.g.accounts[0]
    assert base.orders == [row[1:] for row in multi.routed_orders if row[0] == 0]
    for field in ('equity', 'cash', 'holdings', 'pool_size', 'ranked_size', 'target_size',
                  'pending_exits', 'data_paused', 'turnover'):
        assert [r[field] for r in state.nav_rows] == pytest.approx([r[field] for r in base.g.nav_rows])
    for got, want in zip(state.signal_rows, base.g.signal_rows):
        assert {key: got[key] for key in want} == want
    assert state.holding_rows == base.g.holding_rows
    assert state.exit_pending == base.g.exit_pending
    assert state.deferred_codes == base.g.deferred_codes
    assert not any(r['kind'] == 'ORDER_ERROR' for r in state.event_rows)
    assert set(module._held(state, multi.ctx)) == baseline._held(base.ctx)


def test_shared_snapshot_preserves_visibility_and_history_eligibility(world):
    stub, module, codes, cal = control(world, seeds=tuple(range(1, 11)))
    stub.date_tables['indicator'].loc[0, 'pubDate'] = str(cal[270].date())
    stub.closes[codes[1]].iloc[30] = float('nan')  # 质量波动窗口足够，计分历史不足。
    calls = []
    factor = module.get_factor_values
    module.get_factor_values = lambda **kw: (calls.append(kw), factor(**kw))[1]
    run_control(stub, cal[270:271])
    signals = [s.signal_rows[0] for s in stub.g.accounts]
    assert len(calls) == 1 and calls[0]['end_date'] == cal[269].date()
    assert len({r['candidate_hash'] for r in signals}) == 1
    assert {r['ranked_size'] for r in signals} == {6}
    for row in signals:
        assert codes[0] not in row['top6'] and codes[1] not in row['top6']
    assert len(stub.g.accounts) == 11
    assert len(stub.g.member_rows) == 7  # 共享成员表只写一次，未披露的股票不在质量池。
    assert sum(p.starting_cash for p in stub.ctx.subportfolios) == 11_000_000


def test_seed_is_reproducible_and_account_iteration_does_not_change_fills(world):
    first, m1, codes, cal = control(world)
    second, m2, _, _ = control(world)
    second.g.accounts.reverse()
    run_control(first, cal[270:272])
    run_control(second, cal[270:272])
    for seed in (1, 2):
        ranked = m1.random_rank(codes, seed, cal[269])
        assert ranked == m1.random_rank(list(reversed(codes)), seed, cal[269])
        assert set(ranked) == set(codes)
    for state in first.g.accounts:
        peer = next(s for s in second.g.accounts if s.label == state.label)
        assert state.signal_rows == peer.signal_rows
        assert state.nav_rows == peer.nav_rows


def test_random_new_entries_do_not_reorder_retained_positions_or_fill_beyond_top6(world):
    stub, module, codes, cal = control(world)
    state = stub.g.accounts[1]
    for code in (codes[-1], codes[0]):
        stub.ctx.subportfolios[state.index].positions[code] = SimpleNamespace(total_amount=100, locked_amount=0)
    asof = cal[269]
    shuffled = module.random_rank(codes, state.seed, asof)
    industries = {c: 'crowded' for c in shuffled[:6]}
    industries.update({c: 'outside' for c in shuffled[6:]})
    audit = {c: {'pool_state': 'passed'} for c in codes}
    module._plan_account(state, stub.ctx, asof, codes, codes, dict.fromkeys(codes, 0), industries, audit)
    assert state.pending['selected'][:2] == [codes[0], codes[-1]]
    assert set(state.pending['selected']) <= {codes[0], codes[-1]} | set(shuffled[:6])
    assert len(state.pending['selected']) < 6


def test_other_accounts_pending_order_does_not_block_same_stock(world):
    stub, module, codes, cal = control(world)
    regular = module.order_target
    # 所有账户暂用相同排序，以直接验证同一股票的订单不会串账户。
    for state in stub.g.accounts:
        state.seed = None

    def pending_first(code, quantity, pindex):
        if pindex == 0 and code == codes[0]:
            stub.oid += 1
            order = SimpleNamespace(order_id=str(stub.oid), security=code, is_buy=True,
                                    filled=0, price=10., status='open', commission=0.)
            stub.daily_orders[stub.oid] = stub.live_orders[stub.oid] = order
            return order
        return regular(code, quantity, pindex)

    module.order_target = pending_first
    run_control(stub, cal[270:271])
    assert codes[0] not in module._held(stub.g.accounts[0], stub.ctx)
    assert codes[0] in module._held(stub.g.accounts[1], stub.ctx)
    assert stub.g.accounts[0].order_rows[0]['filled'] == 0
    assert stub.g.accounts[1].order_rows[0]['filled'] > 0


def test_shared_failure_retries_all_accounts_without_losing_confirmed_exits(world):
    stub, module, codes, cal = control(world)
    run_control(stub, cal[270:290])
    state = stub.g.accounts[0]
    state.exit_pending[codes[0]] = 'out_of_pool'
    state.exit_details[codes[0]] = {'pool_reason': 'pe_high'}
    build = module.build_pool
    module.build_pool = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('missing source'))
    run_control(stub, cal[290:291])
    assert stub.g.signal_retry
    assert all(s.nav_rows[-1]['data_paused'] for s in stub.g.accounts)
    assert codes[0] not in module._held(state, stub.ctx)  # 确认退出仍执行。
    module.build_pool = build
    run_control(stub, cal[291:292])
    assert not stub.g.signal_retry
    assert len({s.signal_rows[-1]['candidate_hash'] for s in stub.g.accounts}) == 1


def test_exports_include_initial_cost_year_boundary_and_reconciled_stock_residual(world):
    stub, module, codes, cal = control(world)
    run_control(stub, cal[270:273])
    module.on_strategy_end(stub.ctx)
    assert len(stub.exports) == 13
    read = lambda name: pd.read_csv(StringIO(stub.exports[module.OUTPUT_PREFIX + '_' + name + '.csv']))
    summary, attribution = read('summary'), read('attribution')
    assert (summary.return_pct < 0).all()
    assert (summary.max_drawdown_pct > 0).all()
    for _, row in summary.iterrows():
        per_stock = attribution[attribution.account == row.account]
        assert per_stock.price_pnl.sum() + per_stock.residual.sum() == pytest.approx(row.pnl)
    assert set(read('orders').account) == {'momentum', 'random_01', 'random_02'}
    assert set(read('settings').columns) >= {'start_date', 'end_date', 'random_seeds', 'source_v5_sha256'}
    state = module.new_account(0, None)
    def nav(date, value):
        return dict(date=date, equity=value, cash_pct=0., holdings=6,
                    turnover=0., traded_value=0., data_paused=False)
    state.nav_rows = [nav('2024-12-30', 1_100_000), nav('2024-12-31', 1_200_000),
                      nav('2025-01-02', 1_080_000)]
    annual = module.yearly_metrics(state)
    assert annual[0]['return_pct'] == pytest.approx(20.)
    assert annual[1]['return_pct'] == pytest.approx(-10.)
    assert annual[1]['max_drawdown_pct'] == pytest.approx(10.)
    assert annual[1]['initial_equity'] == 1_200_000


def test_batched_details_keep_all_days_once_without_retaining_large_buffers(world):
    stub, module, codes, cal = control(world)
    run_control(stub, cal[270:311])
    assert len(stub.g.position_rows) <= 6 * len(stub.g.accounts)
    module.on_strategy_end(stub.ctx)
    positions = pd.read_csv(StringIO(stub.exports[module.OUTPUT_PREFIX + '_positions.csv']))
    members = pd.read_csv(StringIO(stub.exports[module.OUTPUT_PREFIX + '_members.csv']))
    assert positions.date.nunique() == 41
    assert not positions.duplicated(['date', 'account', 'code']).any()
    assert members.signal_day.nunique() == 3
    assert not members.duplicated(['signal_day', 'code']).any()
    assert not stub.g.position_rows and not stub.g.member_rows


def test_mode_and_frequency_guard_and_unattributed_order_fail_loudly(world):
    stub, module, codes, cal = control(world)
    stub.ctx.run_params.frequency = 'minute'
    with pytest.raises(ValueError, match='每天'):
        module.initialize(stub.ctx)
    stub.ctx.run_params.frequency = 'day'
    stub.portfolio.starting_cash = 100_000
    with pytest.raises(ValueError, match='初始资金'):
        module.initialize(stub.ctx)
    stub.daily_orders[999] = SimpleNamespace(security=codes[0])
    with pytest.raises(RuntimeError, match='Unattributed'):
        module.after_trading_end(stub.ctx)


def test_reused_v5_data_and_selection_rules_have_not_drifted():
    original = (BASE / 'pool_rotation_v5.py').read_text(encoding='utf-8-sig')
    study = SCRIPT.read_text(encoding='utf-8')
    assert hashlib.sha256(original.encode('utf-8')).hexdigest() in study
    defs = lambda source: {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(source).body
                           if isinstance(n, ast.FunctionDef)}
    base, experiment = defs(original), defs(study)
    for name in ('_evaluate_pool', 'rank_pool', 'select_targets', '_annualized_vol', '_history_closes',
                 '_fetch_roe_ttm', '_sw_industries', '_quote_block', '_affordable_shares'):
        assert base[name] == experiment[name], name
