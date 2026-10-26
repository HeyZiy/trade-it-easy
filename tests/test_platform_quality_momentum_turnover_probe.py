"""验证换手探针的时间边界、固定分组和缺测口径，不产生历史研究结论。"""
import ast
import hashlib
import importlib.util
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_platform_quality_pool_v2 import world, make, BASE  # noqa: E402,F401


SCRIPT = BASE / 'study_v5_turnover_probe_jq.py'


def probe(world):
    stub, _, _, cal = make(world)
    codes = ['600%03d.XSHG' % i for i in range(60)]
    stub.closes = {c: pd.Series(10 * np.exp(np.arange(len(cal)) * (i + 1) / 100000),
                               index=cal) for i, c in enumerate(codes)}
    stub.set_date_table(pd.DataFrame(dict(
        code=codes, inc_net_profit_year_on_year=[20.] * len(codes),
        statDate=['2024-03-31'] * len(codes), pubDate=['2024-04-01'] * len(codes))), table='indicator')
    stub.set_date_table(pd.DataFrame(dict(code=codes, pe_ratio=[20.] * len(codes),
                                         pb_ratio=[3.] * len(codes))), table='valuation')
    stub.turnover_calls, stub.quote_calls, stub.paused_endpoints = [], [], set()

    def valuation(securities, start_date, end_date, fields):
        start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        assert end < stub.today and end == cal[cal < stub.today][-1]
        days = cal[(cal >= start) & (cal <= end)]
        assert len(days) == 20
        stub.turnover_calls.append((start, end, tuple(securities)))
        return pd.DataFrame([dict(code=c, day=d, turnover_ratio=1 + int(c[3:6]) % 17 / 10,
                                   market_cap=50 + int(c[3:6])) for c in securities for d in days])

    def prices(securities, start_date, end_date, fields, frequency, fq, skip_paused, panel):
        start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        assert end <= stub.today and fq == 'post' and not skip_paused and not panel
        assert fields == ['open', 'paused'] and frequency == 'daily'
        stub.quote_calls.append((start, end, tuple(securities)))
        return pd.DataFrame([dict(time=d, code=c, open=stub.closes[c].at[d],
                                   paused=int((d, c) in stub.paused_endpoints))
                             for c in securities for d in cal[(cal >= start) & (cal <= end)]])

    def write_file(name, content, append=False):
        stub.exports[name] = (stub.exports.get(name, '') if append else '') + content

    stub._module.write_file = write_file
    stub._module.get_valuation, stub._module.get_price = valuation, prices
    stub._module.get_industry = lambda securities, date: {
        c: {'sw_l1': {'industry_code': 'industry_%d' % (int(c[3:6]) % 3)}} for c in securities}
    stub.ctx.run_params = SimpleNamespace(frequency='day', start_date=cal[270], end_date=cal[335])
    module = stub.load_script(SCRIPT)
    stub.scheduled.clear()
    stub.initialize()
    return stub, module, codes, cal


def run(stub, module, dates):
    for day in dates:
        stub.today = pd.Timestamp(day).normalize()
        stub.ctx.current_dt = stub.today + pd.Timedelta(hours=9)
        module.on_probe_signal(stub.ctx)
        module.on_probe_signal(stub.ctx)  # 同日重复回调不得重复采样。
        stub.ctx.current_dt = stub.today + pd.Timedelta(hours=15, minutes=30)
        module.after_trading_end(stub.ctx)
        module.after_trading_end(stub.ctx)


def exported(stub, module, kind):
    return pd.read_csv(StringIO(stub.exports[module.OUTPUT_PREFIX + '_' + kind + '.csv']))


def test_real_callback_chain_freezes_before_open_and_measures_exact_horizons(world):
    stub, module, codes, cal = probe(world)
    # 尚未披露的财报排除；质量历史够用但动量历史缺失的候选仍进入全池。
    stub.date_tables['indicator'].loc[0, 'pubDate'] = str(cal[270].date())
    stub.closes[codes[1]].iloc[37] = np.nan
    run(stub, module, cal[270:271])
    frozen = stub.g.pending[0]['frame']
    assert codes[0] not in frozen.index and codes[1] in frozen.index
    assert pd.isna(frozen.at[codes[1], 'momentum'])
    assert not frozen.at[codes[1], 'momentum_strong']
    assert not stub.quote_calls and not stub.orders
    expected_group = frozen.loc[codes[3], 'group_pool_raw']
    run(stub, module, cal[271:336])
    module.on_strategy_end(stub.ctx)
    samples = exported(stub, module, 'samples')
    first = samples[samples.signal_day.eq(str(cal[270].date()))].set_index('code')
    row = first.loc[codes[3]]
    assert row.group_pool_raw == expected_group
    assert row.entry_open == pytest.approx(stub.closes[codes[3]].at[cal[270]])
    for h in (20, 40, 60):
        assert row['r%d' % h] == pytest.approx(
            stub.closes[codes[3]].at[cal[270 + h]] / stub.closes[codes[3]].at[cal[270]] - 1)
        assert row['status_r%d' % h] == 'ok'
    assert stub.g.sample_count == 4 and len(stub.turnover_calls) == 4
    assert not samples.duplicated(['signal_day', 'code']).any()
    assert len(stub.quote_calls) == 4 and not stub.orders
    assert len(stub.exports) == 4
    assert samples[samples.signal_day.eq(str(cal[330].date()))].status_r20.eq('censored').all()
    summary = exported(stub, module, 'summary')
    short = summary[(summary.window == 'full') & (summary.scope == 'pool_raw') &
                    (summary.horizon == 20) & (summary.group == 'low')].iloc[0]
    assert short.periods == 4 and short.censored == 1 and short.valid_periods == 3


def test_suspended_endpoint_is_missing_without_changing_frozen_group(world):
    stub, module, _, cal = probe(world)
    run(stub, module, cal[270:271])
    frozen = stub.g.pending[0]['frame']
    code = frozen[frozen.group_pool_raw.eq(1)].index[0]
    group_size = int(frozen.group_pool_raw.eq(1).sum())
    stub.paused_endpoints.add((cal[290], code))
    run(stub, module, cal[271:331])
    samples = exported(stub, module, 'samples').set_index('code')
    assert samples.loc[code, 'status_r20'] == 'exit_paused'
    assert pd.isna(samples.loc[code, 'r20']) and samples.loc[code, 'group_pool_raw'] == 1
    periods = exported(stub, module, 'periods')
    low = periods[(periods.scope == 'pool_raw') & (periods.horizon == 20) &
                  (periods.group == 'low')].iloc[0]
    assert low.n == group_size and low.observed == group_size - 1
    assert pd.isna(low.return_mean) and pd.notna(low.return_available)
    assert low.coverage == pytest.approx((group_size - 1) / group_size)


def test_turnover_requires_all_twenty_market_sessions_and_rejects_future_or_duplicate(world):
    stub, module, codes, cal = probe(world)
    regular = module.get_valuation
    module.get_valuation = lambda *a, **kw: regular(*a, **kw).iloc[1:]
    snapshot = module.turnover_snapshot(codes[:2], cal[269])
    assert snapshot[codes[0]]['turnover_observed'] == 19
    assert np.isnan(snapshot[codes[0]]['turnover20'])
    assert snapshot[codes[1]]['turnover_status'] == 'ok'
    module.get_valuation = lambda *a, **kw: pd.concat([regular(*a, **kw)] * 2)
    with pytest.raises(module.PoolDataError, match='Duplicate'):
        module.turnover_snapshot(codes[:1], cal[269])
    def future(*args, **kwargs):
        result = regular(*args, **kwargs)
        result.loc[0, 'day'] = cal[270]
        return result
    module.get_valuation = future
    with pytest.raises(module.PoolDataError, match='outside'):
        module.turnover_snapshot(codes[:1], cal[269])


def test_historical_valuation_api_fallback_and_numeric_dates(world):
    stub, module, codes, cal = probe(world)
    regular = module.get_valuation
    calls = []
    def continuous(query, end_date, count, panel):
        assert count == 20 and not panel
        calls.append(end_date)
        frame = regular(codes[:1], cal[250].date(), end_date, ['turnover_ratio', 'market_cap'])
        frame['day'] = pd.to_datetime(frame.day).dt.strftime('%Y%m%d').astype(int)
        return frame
    del module.get_valuation
    module.get_fundamentals_continuously = continuous
    snapshot = module.turnover_snapshot(codes[:1], cal[269])
    assert snapshot[codes[0]]['turnover_status'] == 'ok' and len(calls) == 1


def test_ties_do_not_get_arbitrarily_split_and_small_groups_are_unavailable(world):
    _, module, _, _ = probe(world)
    assert module.fixed_groups(pd.Series([1.] * 30)).isna().all()
    assert module.fixed_groups(pd.Series(range(12))).isna().all()
    assert module.fixed_groups(pd.Series(range(30))).value_counts().to_dict() == {1.: 10, 2.: 10, 3.: 10}


def test_neutralization_removes_controls_and_preserves_missing_controls(world):
    _, module, _, _ = probe(world)
    rng = np.random.default_rng(123)
    n = 90
    frame = pd.DataFrame(dict(market_cap=np.exp(rng.normal(4, .4, n)),
                              momentum=rng.normal(0, .3, n), vol60=rng.uniform(10, 30, n),
                              industry_code=['a', 'b', 'c'] * 30))
    noise = rng.normal(0, .03, n)
    frame['turnover20'] = np.expm1(1 + .2 * np.log(frame.market_cap) +
                                  .3 * frame.momentum + .01 * frame.vol60 + noise)
    residual = module.neutral_turnover(frame)
    assert residual.notna().all()
    for control in [np.log(frame.market_cap), frame.momentum, frame.vol60]:
        assert abs(residual.corr(control)) < 1e-10
    frame.loc[0, 'market_cap'] = np.nan
    assert pd.isna(module.neutral_turnover(frame).iloc[0])


def test_legacy_pandas_without_dataframe_to_numpy_keeps_raw_and_neutral_samples(world, monkeypatch):
    stub, module, codes, cal = probe(world)
    def unavailable(*args, **kwargs):
        raise AttributeError("'DataFrame' object has no attribute 'to_numpy'")
    monkeypatch.setattr(pd.DataFrame, 'to_numpy', unavailable)
    run(stub, module, cal[270:271])
    snapshot = stub.g.pending[0]
    assert snapshot['error'] == '', snapshot['error']
    assert len(snapshot['frame']) == len(codes)
    assert snapshot['frame'].group_pool_raw.notna().all()
    assert snapshot['frame'].group_pool_neutral.notna().all()
    assert stub.g.source_errors == 0


def test_auxiliary_neutralization_failure_does_not_destroy_primary_raw_groups(world):
    stub, module, codes, cal = probe(world)
    module.neutral_turnover = lambda *a: (_ for _ in ()).throw(RuntimeError('regression unavailable'))
    run(stub, module, cal[270:271])
    snapshot = stub.g.pending[0]
    assert snapshot['error'] == '' and len(snapshot['frame']) == len(codes)
    assert snapshot['frame'].group_pool_raw.notna().all()
    assert snapshot['frame'].group_strong_raw.notna().any()
    assert snapshot['frame'].group_pool_neutral.isna().all()
    assert snapshot['frame'].neutral_error.eq('regression unavailable').all()
    assert stub.g.source_errors == 0 and stub.g.neutral_errors == 1
    run(stub, module, cal[271:291])
    module.on_strategy_end(stub.ctx)
    first = exported(stub, module, 'periods')
    first = first[first.signal_day.eq(str(cal[270].date())) & first.horizon.eq(20)]
    assert first[first.scope.eq('pool_raw')].return_mean.notna().all()
    assert first[first.scope.eq('pool_neutral')].return_mean.isna().all()


def test_missing_middle_period_does_not_get_compressed_into_hac_series(world):
    _, module, _, _ = probe(world)
    series = pd.Series([.01, .02, np.nan, -.01, .03])
    stats = module.summarize_ic(series, 1)
    assert stats['n'] == 4 and np.isnan(stats['t_hac'])


def test_snapshot_failure_is_recorded_and_sampling_clock_does_not_retry_off_grid(world):
    stub, module, _, cal = probe(world)
    regular = module.build_pool
    module.build_pool = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('source down'))
    run(stub, module, cal[270:271])
    module.build_pool = regular
    run(stub, module, cal[271:296])
    module.on_strategy_end(stub.ctx)
    assert stub.g.sample_count == 2 and len(stub.turnover_calls) == 1
    samples = exported(stub, module, 'samples')
    failed = samples[samples.signal_day.eq(str(cal[270].date()))]
    assert len(failed) == 1 and failed.snapshot_error.iloc[0] == 'source down'
    periods = exported(stub, module, 'periods')
    assert periods[periods.signal_day.eq(str(cal[270].date())) &
                   periods.horizon.eq(20)].window_status.eq('snapshot_error').all()


def test_standalone_pool_and_statistics_match_authoritative_sources(world):
    _, module, _, _ = probe(world)
    generator = BASE.parents[1] / 'tools/single_factor/platform_codegen.py'
    spec = importlib.util.spec_from_file_location('factor_codegen', generator)
    codegen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codegen)
    probe_nodes = {node.name: node for node in ast.parse(SCRIPT.read_text(encoding='utf-8')).body
                   if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    sources = [(BASE / 'pool_rotation_v5.py', ['PoolDataError', '_evaluate_pool', '_annualized_vol',
                'rank_pool', 'build_pool', '_prev_trade_day', '_require_columns', '_report_date',
                '_fetch_roe_ttm', '_history_closes', '_held']),
               (BASE.parents[1] / 'tools/single_factor/single_factor_test.py',
                ['clean', 'assign_groups', 'summarize_ic'])]
    for path, names in sources:
        authoritative = ast.parse(codegen.protect_builtins(path.read_text(encoding='utf-8')))
        for node in authoritative.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
                assert ast.dump(probe_nodes[node.name]) == ast.dump(node), node.name
    assert module.SOURCE_V5_SHA256 == hashlib.sha256((BASE / 'pool_rotation_v5.py').read_bytes()).hexdigest()


def response_probe(world):
    stub, module, codes, cal = probe(world)
    module.PROBE = 'earnings_response'
    stub.date_tables['indicator']['statDate'] = '2024-09-30'
    stub.date_tables['indicator']['pubDate'] = str(cal[220].date())
    stub.reference_calls, stub.response_history_calls = [], []
    original_prices = module.get_price

    def prices(securities, start_date, end_date, fields, frequency, fq, skip_paused, panel):
        if fields == ['open', 'paused']:
            return original_prices(securities, start_date, end_date, fields, frequency, fq, skip_paused, panel)
        assert fields == ['close', 'paused'] and fq == 'post' and not skip_paused and not panel
        start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
        assert end < stub.today and end == cal[cal < stub.today][-1]
        stub.response_history_calls.append((start, end, tuple(securities)))
        return pd.DataFrame([dict(time=d, code=c, close=stub.closes[c].at[d],
                                   paused=int((d, c) in stub.paused_endpoints))
                             for c in securities for d in cal[(cal >= start) & (cal <= end)]])

    def industry_stocks(industry, date):
        assert pd.Timestamp(date) < stub.today
        stub.reference_calls.append((industry, date))
        return [c for c in codes if 'industry_%d' % (int(c[3:6]) % 3) == industry]

    module.get_price, module.get_industry_stocks = prices, industry_stocks
    stub.scheduled.clear()
    stub.exports.clear()  # 此辅助桩此前初始化过换手模式；两次运行的输出独立。
    stub.initialize()
    return stub, module, codes, cal


def test_response_real_callbacks_freeze_t1_groups_and_export_auditable_paths(world):
    stub, module, codes, cal = response_probe(world)
    run(stub, module, cal[270:271])
    snapshot = stub.g.pending[0]
    assert snapshot['error'] == '', snapshot['error']
    frozen = snapshot['frame'].copy()
    assert frozen.response_status.eq('ok').all()
    assert frozen.response_anchor.eq(str(cal[221].date())).all()
    assert frozen.response_age.eq(48).all()
    assert frozen.group_pool_response.notna().all()
    assert frozen.group_pool_momentum.notna().all()
    assert not stub.turnover_calls and not stub.quote_calls and not stub.orders
    # 冻结后变动未来报价，不得重新用未来路径分组。
    code = codes[-1]
    stub.closes[code].loc[cal[275]:] *= 1.08
    run(stub, module, cal[271:336])
    module.on_strategy_end(stub.ctx)
    samples = exported(stub, module, 'samples')
    first = samples[samples.signal_day.eq(str(cal[270].date()))].set_index('code')
    assert not samples.duplicated(['signal_day', 'code']).any()
    assert first.loc[code, 'group_pool_response'] == frozen.loc[code, 'group_pool_response']
    assert first.loc[code, 'hit5_20'] == 1 and first.loc[code, 'wait5_20'] == 5
    assert len(first.loc[code, 'forward_opens'].split(';')) == 61
    assert first.loc[code, 'r60'] == pytest.approx(stub.closes[code].at[cal[330]] / stub.closes[code].at[cal[270]] - 1)
    assert samples[samples.signal_day.eq(str(cal[330].date()))].path_status_20.eq('censored').all()
    assert len(stub.exports) == 4 and not stub.orders
    periods = exported(stub, module, 'periods')
    summary = exported(stub, module, 'summary')
    paired = periods[(periods.signal_day.eq(str(cal[270].date()))) & (periods.horizon == 20) &
                     (periods.metric == 'hit5')].set_index(['scope', 'group'])
    assert paired.loc[('response_vs_momentum', 'high_minus_high'), 'value'] == pytest.approx(
        paired.loc[('pool_response', 'high'), 'value'] - paired.loc[('pool_momentum', 'high'), 'value'])
    assert set(summary.metric) == set(module.RESPONSE_METRICS)
    settings = exported(stub, module, 'settings').set_index('setting').value
    assert settings['probe'] == 'earnings_response' and int(settings['source_errors']) == 0


def test_response_score_excludes_self_and_detects_fading_after_report(world):
    stub, module, codes, cal = response_probe(world)
    run(stub, module, cal[270:271])
    frame = stub.g.pending[0]['frame'].copy()
    for code in codes:
        stub.closes[code][:] = 10.
    code = codes[0]
    stub.closes[code].loc[cal[249]:] = 12.
    stub.closes[code].loc[cal[269]:] = 11.
    frame = module.response_features(frame, cal[269])
    row = frame.loc[code]
    assert code not in row.peer_members.split(';') and row.peer_count == 19
    assert row.industry_gross_post == 1 and row.industry_gross_recent == 1
    assert row.excess_log_post == pytest.approx(np.log(1.1))
    assert row.excess_log_recent == pytest.approx(np.log(11 / 12))
    assert row.response_state == 'faded'
    assert row.response_score == pytest.approx(np.log(11 / 12))
    assert all(pd.Timestamp(date) == cal[269] for _, date in stub.reference_calls)


def test_response_report_visibility_age_and_missing_industry_preserve_original_candidates(world):
    stub, module, codes, cal = response_probe(world)
    run(stub, module, cal[270:271])
    original = stub.g.pending[0]['frame'].copy()
    original.loc[codes[0], 'pub_date'] = str(cal[270].date())
    original.loc[codes[1], 'pub_date'] = str(cal[260].date())
    original.loc[codes[2], 'pub_date'] = str(cal[190].date())
    original.loc[codes[3], 'industry_code'] = ''
    original.loc[codes[4], 'momentum'] = np.nan
    frozen = module.response_features(original, cal[269])
    assert len(frozen) == len(original)
    assert frozen.loc[codes[0], 'response_status'] == 'report_not_visible'
    assert frozen.loc[codes[1], 'response_status'] == 'report_too_recent'
    assert frozen.loc[codes[2], 'response_status'] == 'report_too_old'
    assert frozen.loc[codes[3], 'response_status'] == 'industry_unknown'
    eligible_new = frozen.group_pool_response.notna()
    eligible_old = frozen.group_pool_momentum.notna()
    pd.testing.assert_series_equal(eligible_new, eligible_old, check_names=False)
    assert not eligible_new.loc[codes[:5]].any()


def test_response_missing_peer_history_and_industry_errors_are_not_filled(world):
    stub, module, codes, cal = response_probe(world)
    run(stub, module, cal[270:271])
    original = stub.g.pending[0]['frame'].copy()
    code = codes[0]
    stub.closes[code].loc[cal[245]] = np.nan
    frame = module.response_features(original, cal[269])
    assert frame.loc[code, 'response_status'] == 'stock_history_missing_or_paused'
    assert pd.isna(frame.loc[code, 'response_score'])
    # 同业参考只使用T-1已经可见的完整历史，缺报价成员不伪造为平盘。
    assert code not in frame.loc[codes[3], 'peer_members'].split(';')
    module.get_industry_stocks = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('industry down'))
    frame = module.response_features(original, cal[269])
    assert len(frame) == len(original) and frame.response_score.isna().all()
    assert stub.g.industry_errors == 3


def test_response_rejects_future_history_but_retains_pool_rows(world):
    stub, module, codes, cal = response_probe(world)
    run(stub, module, cal[270:271])
    original = stub.g.pending[0]['frame'].copy()
    regular = module.get_price
    def future(*a, **kw):
        result = regular(*a, **kw)
        result.loc[0, 'time'] = cal[270]
        return result
    module.get_price = future
    frame = module.response_features(original, cal[269])
    assert len(frame) == len(codes) and frame.response_score.isna().all()
    assert frame.response_status.str.startswith('history_error').all()
    assert stub.g.source_errors == 1


def test_response_wait_does_not_discard_non_hits_or_call_unknown_a_failure(world):
    _, module, _, _ = response_probe(world)
    metrics, status = module.response_path_metrics([100., 110., 100., 105.], ['ok'] * 4, 3)
    assert status == 'ok' and metrics['hit5'] == 1 and metrics['wait5'] == 1
    assert metrics['mdd'] == pytest.approx(100 / 110 - 1) and metrics['mae'] == 0
    metrics, status = module.response_path_metrics([100., 101., 99., 102.], ['ok'] * 4, 3)
    assert metrics['hit5'] == 0 and metrics['wait5'] == 3
    metrics, status = module.response_path_metrics([100., 101., 99., 105.], ['ok'] * 4, 3)
    assert metrics['hit5'] == 1 and metrics['wait5'] == 3
    metrics, status = module.response_path_metrics([100., 110., 100., 105.], ['ok', 'ok', 'paused', 'ok'], 3)
    assert status == 'path_unknown' and all(pd.isna(v) for v in metrics.values())
    metrics, status = module.response_path_metrics([100., 110.], ['ok'] * 2, 3)
    assert status == 'censored' and all(pd.isna(v) for v in metrics.values())


def test_response_missing_interior_price_keeps_endpoint_return_and_paired_path_missing(world):
    stub, module, _, cal = response_probe(world)
    run(stub, module, cal[270:271])
    frozen = stub.g.pending[0]['frame']
    code = frozen[frozen.group_pool_response.eq(3)].index[0]
    stub.paused_endpoints.add((cal[280], code))
    run(stub, module, cal[271:291])
    module.on_strategy_end(stub.ctx)
    first_day = str(cal[270].date())
    samples = exported(stub, module, 'samples')
    row = samples[samples.signal_day.eq(first_day)].set_index('code').loc[code]
    assert row.status_r20 == 'ok' and pd.notna(row.r20)
    assert row.path_status_20 == 'path_unknown' and pd.isna(row.hit5_20)
    periods = exported(stub, module, 'periods')
    paired = periods[(periods.signal_day.eq(first_day)) & (periods.horizon == 20) &
                     (periods.scope == 'response_vs_momentum')]
    assert pd.notna(paired[paired.metric.eq('return')].value.iloc[0])
    assert pd.isna(paired[paired.metric.eq('hit5')].value.iloc[0])


def test_response_works_without_modern_pandas_conversion_or_turnover_api(world, monkeypatch):
    stub, module, codes, cal = response_probe(world)
    monkeypatch.setattr(pd.DataFrame, 'to_numpy', lambda *a, **kw: (_ for _ in ()).throw(AttributeError('legacy pandas')))
    del module.get_valuation
    run(stub, module, cal[270:271])
    assert stub.g.pending[0]['error'] == ''
    assert len(stub.g.pending[0]['frame']) == len(codes)
    assert stub.g.pending[0]['frame'].response_status.eq('ok').all()


def test_response_summary_does_not_compress_missing_pair_or_include_censored_tail(world):
    _, module, _, _ = response_probe(world)
    periods = pd.DataFrame([
        dict(signal_day=day, scope='response_vs_momentum', horizon=20, group='high_minus_high',
             metric='hit5', window_status='censored' if i == 5 else 'mature',
             value=value, available=.2, coverage=1., n=10)
        for i, (day, value) in enumerate(zip(pd.bdate_range('2024-01-01', periods=6), [.1, .2, np.nan, -.1, .3, .9]))])
    row = module.response_summary(periods).query("window == 'full'").iloc[0]
    assert row.periods == 6 and row.censored == 1 and row.valid_periods == 4
    assert row.mean_value == pytest.approx(.125) and pd.isna(row.t_hac)
