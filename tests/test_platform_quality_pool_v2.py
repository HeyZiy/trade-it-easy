"""聚宽v5的真实回调链；不把合成成交当成平台回测成绩。"""
import pandas as pd
import pytest

from test_platform_quality_momentum_stop_loss import world


def make(world, experiment='clock'):
    create, cal = world
    stub, mod, codes = create('pool_rotation_v5.py')
    quotes = mod.get_current_data
    def valid_quotes():
        cd = quotes()
        for code in codes:
            if 'low_limit' not in stub.quote_overrides.get(code, {}):
                cd[code].low_limit = .01
        return cd
    mod.get_current_data = valid_quotes
    # 模式在initialize时决定注册回调；重新初始化独立空仓测试场景。
    mod.EXPERIMENT = experiment
    stub.scheduled.clear()
    stub.initialize()
    return stub, mod, codes, cal


@pytest.mark.parametrize('experiment,entry_offset', [('fixed', 1), ('clock', 0)])
def test_clock_comparison_changes_only_signal_execution_delay(world, experiment, entry_offset):
    stub, mod, codes, cal = make(world, experiment)
    stub.run_days(cal[270:272])
    assert mod._held(stub.ctx) == set(codes[:6])
    assert {o[1] for o in stub.orders if o[0] == 'B'} == {str(cal[270 + entry_offset].date())}
    assert stub.g.signal_rows[0]['asof'] == str(cal[269].date())


def test_clock_retries_failed_signal_next_session(world):
    stub, mod, codes, cal = make(world)
    original = mod.build_pool
    mod.build_pool = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('source down'))
    stub.run_days(cal[270:271])
    assert stub.g.signal_retry and not stub.orders
    mod.build_pool = original
    stub.run_days(cal[271:272])
    assert not stub.g.signal_retry and mod._held(stub.ctx) == set(codes[:6])


@pytest.mark.parametrize('experiment,bought', [('clock', False), ('buy_retry', True)])
def test_buy_retry_is_separate_and_expires_same_day(world, experiment, bought):
    stub, mod, codes, cal = make(world, experiment)
    code = codes[0]
    stub.quote_overrides[code] = {'high_limit': 10.}
    stub.today = cal[270]
    stub.ctx.current_dt = cal[270] + pd.Timedelta(hours=8)
    mod.on_signal(stub.ctx)
    stub.ctx.current_dt = cal[270] + pd.Timedelta(hours=9, minutes=31)
    mod.on_open(stub.ctx)
    assert code not in mod._held(stub.ctx)
    stub.quote_overrides.clear()
    stub.ctx.current_dt = cal[270] + pd.Timedelta(hours=9, minutes=32)
    mod.on_buy_retry(stub.ctx)
    assert (code in mod._held(stub.ctx)) == bought
    stub.ctx.current_dt = cal[270] + pd.Timedelta(hours=9, minutes=35)
    mod.on_buy_retry(stub.ctx)
    assert stub.g.active_buy_plan is None


def test_unknown_financial_value_keeps_old_position_without_adjustment(world):
    stub, mod, codes, cal = make(world)
    stub.run_days(cal[270:290])
    code = codes[0]
    quantity = stub.portfolio.positions[code].total_amount
    stub.date_tables['valuation'].loc[0, 'pe_ratio'] = float('nan')
    stub.run_days(cal[290:291])
    assert code in stub.g.no_adjust and code not in stub.g.exit_pending
    assert stub.portfolio.positions[code].total_amount == quantity
    stub.date_tables['valuation'].loc[0, 'pe_ratio'] = 20.
    stub.run_days(cal[291:292])
    assert code not in stub.g.deferred_codes


def test_missing_momentum_does_not_force_quality_holding_out(world):
    stub, mod, codes, cal = make(world)
    stub.run_days(cal[270:290])
    stub.closes[codes[0]].iloc[37] = float('nan')
    stub.run_days(cal[290:291])
    assert codes[0] in mod._held(stub.ctx) and not stub.g.signal_retry


def test_suspension_does_not_generate_quality_exit(world):
    stub, mod, codes, cal = make(world)
    stub.run_days(cal[270:290])
    stub.quote_overrides[codes[0]] = {'paused': True}
    stub.run_days(cal[290:291])
    assert codes[0] in mod._held(stub.ctx) and codes[0] not in stub.g.exit_pending


@pytest.mark.parametrize('field,value', [('high_limit', float('nan')), ('low_limit', None),
                                         ('paused', None)])
@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_unknown_official_execution_status_blocks_both_sides(world, field, value, side):
    stub, mod, codes, cal = make(world)
    code = codes[0]
    if side == 'sell':
        stub.run_days(cal[270:271])
        stub.portfolio.positions[code].closeable_amount = stub.portfolio.positions[code].total_amount
        stub.g.exit_pending[code] = 'out_of_pool'
        day = cal[271]
    else:
        day = cal[270]
        stub.today = day
        stub.ctx.current_dt = day + pd.Timedelta(hours=8)
        mod.on_signal(stub.ctx)
    before = len(stub.orders)
    stub.quote_overrides[code] = {field: value}
    stub.today = day
    stub.ctx.current_dt = day + pd.Timedelta(hours=9, minutes=31)
    mod.on_open(stub.ctx)
    assert not [o for o in stub.orders[before:] if o[2] == code]
    assert any(e['code'] == code and 'status/limits_unknown' in e['detail']
               for e in stub.g.event_rows)


def test_return_summary_includes_first_execution_cost(world):
    stub, mod, codes, cal = make(world)
    stub.run_days(cal[270:271])
    messages = []
    mod.log.info = messages.append
    mod.on_strategy_end(stub.ctx)
    loss_pct = (stub.portfolio.total_value / 1_000_000 - 1) * 100
    assert loss_pct < 0
    assert any(('收益=%+.2f%%' % loss_pct) in message for message in messages)


def test_same_length_history_with_wrong_trading_date_is_rejected(world):
    stub, mod, codes, cal = make(world)
    stub.today = cal[270]
    history = mod.history
    def wrong_calendar(*args, **kwargs):
        frame = history(*args, **kwargs)
        dates = list(frame.index)
        dates[0] -= pd.Timedelta(days=1)
        frame.index = dates
        return frame
    mod.history = wrong_calendar
    with pytest.raises(mod.PoolDataError, match='cutoff'):
        mod._history_closes(codes, cal[269])


@pytest.mark.parametrize('unit,raw,expected', [('ratio', .001, .1), ('percent', .1, .1)])
def test_roe_unit_does_not_change_when_subset_has_low_returns(world, unit, raw, expected):
    stub, mod, codes, cal = make(world)
    stub.today = cal[270]
    mod.ROE_FACTOR_UNIT = unit
    mod.get_factor_values = lambda **kwargs: {'roe_ttm': pd.DataFrame(
        {codes[0]: [raw]}, index=[cal[269]])}
    assert mod._fetch_roe_ttm([codes[0]], cal[269]).iloc[0] == pytest.approx(expected)


@pytest.mark.parametrize('case', ['normal', 'financial_unknown', 'failed',
                                 'momentum_missing', 'industry_unknown', 'suspended'])
def test_simulator_and_final_backtest_select_same_positions(world, case):
    from src.quality_pool import config, screener
    stub, mod, codes, cal = make(world)
    for local, platform in [('TOP_N', 'TOP_N'), ('INDUSTRY_CAP', 'INDUSTRY_CAP'),
                            ('ROTATE_EVERY', 'ROTATE_EVERY'), ('ROE_TTM_MIN', 'ROE_MIN'),
                            ('NP_YOY_MIN', 'NP_YOY_MIN'), ('PE_MAX', 'PE_MAX'), ('PB_MAX', 'PB_MAX'),
                            ('VOL_LOOKBACK', 'VOL_LOOKBACK'), ('VOL60_MAX', 'VOL60_MAX'),
                            ('SCORE_CLOSES', 'SCORE_CLOSES'), ('SCORE_SKIP_RECENT', 'SCORE_SKIP_RECENT'),
                            ('VOL_ANNUAL_DAYS', 'VOL_ANNUAL_DAYS'), ('LIMIT_PAD', 'LIMIT_PAD'),
                            ('FEE_COMMISSION', 'FEE_COMMISSION'), ('MIN_COMMISSION', 'MIN_COMMISSION'),
                            ('SLIPPAGE_SPREAD', 'SLIPPAGE_SPREAD')]:
        assert getattr(config, local) == getattr(mod, platform)
    assert config.SIGNAL_CLOCK == '08:00' and config.EXEC_CLOCK == '09:31'
    held = {codes[-1]}
    fin = pd.DataFrame({'roe_ttm_pct': 16., 'np_yoy_pct': 20.,
                        'pe_ttm': 20., 'pb': 3.}, index=codes)
    status = pd.DataFrame({'is_st': False, 'is_suspended': False}, index=codes)
    closes = mod.history(253, '1d', 'close', security_list=codes).copy()
    industries = {code: 'I%d' % (i // 3) for i, code in enumerate(codes)}
    if case == 'financial_unknown':
        fin.loc[codes[-1], 'pe_ttm'] = float('nan')
    elif case == 'failed':
        fin.loc[codes[-1], 'roe_ttm_pct'] = 12.
    elif case == 'momentum_missing':
        closes.loc[closes.index[0], codes[-1]] = float('nan')
    elif case == 'industry_unknown':
        industries.pop(codes[-1])
    elif case == 'suspended':
        status.loc[codes[-1], 'is_suspended'] = True
    local_audit, platform_audit = ({c: {} for c in held}, {c: {} for c in held})
    local_pool = screener.build_pool(set(codes), fin, status, closes, audit=local_audit)
    platform_pool = mod._evaluate_pool(set(codes), fin, status, closes, audit=platform_audit)
    assert local_pool == platform_pool
    pd.testing.assert_frame_equal(pd.DataFrame.from_dict(local_audit, orient='index'),
                                  pd.DataFrame.from_dict(platform_audit, orient='index'))
    local_rank, local_scores = screener.rank_pool(local_pool, closes)
    platform_rank, platform_scores = mod.rank_pool(platform_pool, closes)
    assert local_rank == platform_rank and local_scores == pytest.approx(platform_scores)
    deferred = {c for c in held if local_audit[c]['pool_state'] == 'unknown'}
    selected, kept = screener.select_targets(local_rank, held, set(local_pool), industries, deferred)
    assert (selected, kept) == mod.select_targets(
        platform_rank, held, set(platform_pool), industries, deferred)
    assert len(selected) <= 6 and set(selected) <= held | set(local_rank[:6])
    assert (codes[-1] in kept) == (case != 'failed')
