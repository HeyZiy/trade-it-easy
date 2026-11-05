# -*- coding: utf-8 -*-
# 自动生成：python -m research.studies.quality_momentum.internal.build_growth_basis
# 整文件粘贴聚宽，天频率；2016-01-04至2026-09-01；不下单。
BUILD_SOURCES_SHA256 = '{"research/studies/quality_momentum/internal/build_growth_basis.py": "6e2dbfa64609f6dec966fecd25749d5b01e476a6e134ac4a2ac3e14a4d7fd1f8", "research/studies/quality_momentum/internal/build_platform.py": "64b1e9f477c4051249337ac3babd27063c03564fd79594925b9105e897f02e5d", "research/studies/quality_momentum/internal/growth_basis.py": "64ac410422f822d18a870b592bcfc9785fef299f9cd35b858c6dbfe1d3b23164", "research/studies/quality_momentum/internal/growth_basis_adapter.py": "a04114b687a00c31f8a271c7205aa973e46a0b9ee166be3c97dcdf92f6d3063f", "research/studies/quality_momentum/internal/probe_adapter.py": "63b6b104bfb2783c5b77d69fa6d12f118afa3017039e904e2fac28688e617093", "research/studies/quality_momentum/internal/probe_statistics.py": "62d12d46d674410af979071e612e9c15a0e44ea31995b03a3f7ffb11746b4271", "research/studies/quality_momentum/pool_rotation_v5.py": "cb4967826e2ad82cf5d0963e35bc68b05fce39915029b4f0672868316a7221de", "research/tools/factor_analysis/core.py": "a816fe8bf094b01c3b39a0d7a41dab2d77df26dacee92a22fea6159f558b835f", "research/tools/factor_analysis/platform_codegen.py": "2a4f066e9f051ea79a048e79cb92a35a2d258830d092699492544ea9759e4b88"}'
# -*- coding: utf-8 -*-
"""增长口径对照：天频率，2016-01-04至2026-09-01，不下单。"""
from jqdata import *
from jqfactor import get_factor_values
import builtins as _b
import builtins as _python_builtins
import math as _math
import time as _time
import numpy as np
import pandas as pd

PROBE = 'growth_basis'
OUTPUT_PREFIX = 'quality_momentum_v5_growth_basis_probe'
ROTATE_EVERY, HORIZONS = 20, (20, 40, 60)
MOMENTUM_DAYS, SKIP_RECENT_DAYS = 252, 21
SCORE_CLOSES, SCORE_SKIP_RECENT = 253, 21
ROE_MIN, ROE_TTM_MIN = 12.0, 12.0
# 仅在本独立探针中移除增长门槛，其余原构池函数保持不变；原同比仍须已知。
NP_YOY_MIN = -_python_builtins.float('inf')
ROE_FACTOR_UNIT = 'ratio'
PE_MAX, PB_MAX, VOL60_MAX = 30.0, 5.0, 35.0
VOL_LOOKBACK, VOL_ANNUAL_DAYS = 61, 250.0
BENCHMARK, NAN = '000906.XSHG', _python_builtins.float('nan')
SOURCE_V5_SHA256 = 'cb4967826e2ad82cf5d0963e35bc68b05fce39915029b4f0672868316a7221de'

SAMPLE_COLUMNS = ['signal_day', 'asof', 'code', 'roe_ttm_pct', 'np_yoy_pct', 'pe_ttm', 'pb',
                  'stat_date', 'pub_date', 'vol60', 'momentum', 'momentum_rank',
                  'industry_code', 'market_cap', 'baseline', 'common', 'pass_baseline']
for _arm in ('ttm_total', 'ttm_parent', 'ttm_adjusted'):
    SAMPLE_COLUMNS += [_arm, _arm + '_current', _arm + '_prior', _arm + '_report', _arm + '_status', 'pass_' + _arm]
SAMPLE_COLUMNS += ['entry_open', 'entry_paused']
for _h in HORIZONS:
    SAMPLE_COLUMNS += ['r%d' % _h, 'status_r%d' % _h, 'exit_date_%d' % _h,
                       'exit_open_%d' % _h, 'exit_paused_%d' % _h]
SAMPLE_COLUMNS += ['snapshot_error', 'quote_error']


def initialize(context):
    params = _python_builtins.getattr(context, 'run_params', None)
    if params is not None and _python_builtins.getattr(params, 'frequency', 'day') not in ('day', 'daily'):
        raise _python_builtins.ValueError('请选择天频率；本实验不下单')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    g.day, g.last_signal_day, g.last_close_day = 0, None, None
    g.pending, g.periods, g.started_files = [], [], _python_builtins.set()
    g.sample_count, g.source_errors, g.wall_started = 0, 0, _time.time()
    g.settings = _python_builtins.dict(probe=PROBE, source_v5_sha256=SOURCE_V5_SHA256,
        start_date=_python_builtins.str(_python_builtins.getattr(params, 'start_date', '')), end_date=_python_builtins.str(_python_builtins.getattr(params, 'end_date', '')),
        frequency='day', no_orders=True, signal_cutoff='T-1', horizons='20;40;60', primary_horizon=60,
        primary_scope='common_inputs', primary_contrast='ttm_adjusted_minus_baseline',
        parent_pool='v5 with profit-growth threshold removed; other rules unchanged; original profit growth must be known',
        threshold_pct=10, ttm='sum of 4 consecutive single quarters, compared to preceding 4; prior must be positive',
        formula='(current TTM/prior TTM-1)*100; no fallback for adjusted profit',
        arms='baseline single-quarter total-profit yoy; TTM total; TTM parent; TTM adjusted',
        adjusted_scope='indicator.adjusted_profit; platform field definition, do not assume equivalence to income.net_profit',
        common_inputs='all four growth values known; latest reports aligned with baseline indicator',
        returns='post-adjusted open(T+h)/open(T)-1; paused endpoints unavailable',
        interpretation='equal-weight pool diagnostics; overlapping arms, not independent portfolios',
        vintage='platform watch_date visibility; historical revisions not independently restored',
        pandas_version=pd.__version__, numpy_version=np.__version__)
    _write_settings()
    run_daily(on_probe_signal, time='before_open')


def growth_snapshot(codes, asof, signal):
    blocks = []
    specs = ((income, ('net_profit', 'np_parent_company_owners'), 'income'),
             (indicator, ('adjusted_profit',), 'indicator'))
    for table, names, label in specs:
        for start in _python_builtins.range(0, _python_builtins.len(codes), 100):
            chunk = codes[start:start+100]
            frame = get_history_fundamentals(chunk,
                [table.statDate, table.pubDate] + [_python_builtins.getattr(table, name) for name in names],
                watch_date=asof.date(), count=8, interval='1q', stat_by_year=False)
            _require_columns(frame, ('code', 'statDate', 'pubDate') + names, label)
            if not frame.code.isin(chunk).all():
                raise PoolDataError('Unexpected growth financial code')
            for name in names:
                raw = frame[['code', 'statDate', 'pubDate']].copy()
                raw['metric'], raw['value'] = name, frame[name]
                raw['signal_day'], raw['asof'], raw['table'] = _python_builtins.str(signal.date()), _python_builtins.str(asof.date()), label
                _append_csv('quarters', raw[['signal_day', 'asof', 'table', 'metric', 'code', 'statDate', 'pubDate', 'value']])
                blocks.append(raw)
    return growth_features(pd.concat(blocks, ignore_index=True), codes, asof)


def on_probe_signal(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_signal_day == today:
        return
    g.last_signal_day = today
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY:
        return
    asof = _prev_trade_day(context.current_dt)
    snapshot = _python_builtins.dict(signal_day=today, asof=asof, sample_day=g.day, error='', frame=pd.DataFrame())
    try:
        audit = {}
        pool, closes = build_pool(context, asof, audit=audit)
        ranked, scores = rank_pool(pool, closes)
        if pool:
            codes = _python_builtins.sorted(pool)
            features = growth_snapshot(codes, asof, today)
            cap = get_fundamentals(query(valuation.code, valuation.market_cap).filter(
                valuation.code.in_(codes)), date=asof)
            _require_columns(cap, ('code', 'market_cap'), 'market cap')
            if cap.code.duplicated().any():
                raise PoolDataError('Duplicate market cap')
            features['market_cap'] = pd.to_numeric(cap.set_index('code').market_cap, errors='coerce')
            try:
                industries = get_industry(codes, date=asof.date()) or {}
            except _python_builtins.Exception:
                industries = {}
            ranks = {c: i+1 for i, c in _python_builtins.enumerate(ranked)}
            rows = []
            for code in codes:
                row = _python_builtins.dict(audit[code])
                row.update(features.loc[code].to_dict())
                row.update(momentum=scores.get(code, NAN), momentum_rank=ranks.get(code, NAN),
                           industry_code=((industries.get(code) or {}).get('sw_l1') or {}).get('industry_code', ''))
                rows.append(row)
            snapshot['frame'] = freeze_growth(pd.DataFrame(rows).set_index('code', drop=False))
    except _python_builtins.Exception as exc:
        snapshot['error'] = _python_builtins.str(exc)
        g.source_errors += 1
        log.info('[增长口径] 本期缺测：%s' % exc)
    g.pending.append(snapshot)
    g.sample_count += 1
    log.info('[增长口径] 第%d期 %s 母池=%d' % (g.sample_count, today.date(), _python_builtins.len(snapshot['frame'])))


def period_statistics(snapshot, frame):
    return growth_period_statistics(snapshot, frame)


def on_strategy_end(context):
    today = pd.Timestamp(context.current_dt).normalize()
    for snapshot in _python_builtins.list(g.pending):
        resolve_snapshot(snapshot, today)
    g.pending.clear()
    if g.periods:
        summary = summarize_periods(pd.DataFrame(g.periods).reindex(columns=PERIOD_COLUMNS), ROTATE_EVERY)
        write_file(OUTPUT_PREFIX + '_summary.csv', summary.to_csv(index=False), append=False)
    g.settings.update(sample_count=g.sample_count, source_errors=g.source_errors,
                      elapsed_seconds=_time.time() - g.wall_started)
    _write_settings()
    log.info('[增长口径结束] 下载settings、quarters、samples、periods、summary五份CSV；空账户不是结果。')


def forward_quotes(codes, start, end):
    """在已经到期后取统一后复权开盘价，不能把停牌补价视作成交。"""
    blocks = []
    for index in _python_builtins.range(0, _python_builtins.len(codes), 100):
        frame = get_price(codes[index:index + 100], start_date=start.date(), end_date=end.date(),
                          fields=['open', 'paused'], frequency='daily', fq='post',
                          skip_paused=False, panel=False)
        _require_columns(frame, ('time', 'code', 'open', 'paused'), 'forward quote')
        blocks.append(frame)
    data = pd.concat(blocks, ignore_index=True)
    data['time'] = pd.to_datetime(data.time).dt.normalize()
    if (data.time.isna().any() or not data.time.between(start, end).all()
            or not data.code.isin(codes).all() or data.duplicated(['time', 'code']).any()):
        raise PoolDataError('Invalid forward quote date/code/duplicates')
    return data.set_index(['time', 'code'])

def quote_at(quotes, day, code):
    if (day, code) not in quotes.index:
        return NAN, NAN, 'missing'
    row = quotes.loc[(day, code)]
    price = pd.to_numeric(row['open'], errors='coerce')
    paused = pd.to_numeric(row['paused'], errors='coerce')
    if not np.isfinite(paused) or paused not in (0, 1):
        return _python_builtins.float(price), NAN, 'status_unknown'
    if paused:
        return _python_builtins.float(price), _python_builtins.float(paused), 'paused'
    if not np.isfinite(price) or price <= 0:
        return NAN, _python_builtins.float(paused), 'missing'
    return _python_builtins.float(price), _python_builtins.float(paused), 'ok'

def resolve_snapshot(snapshot, today):
    if PROBE == 'earnings_response':
        resolve_response(snapshot, today)
        return
    frame = snapshot['frame'].copy()
    days = pd.DatetimeIndex(get_trade_days(start_date=snapshot['signal_day'].date(),
                                          end_date=today.date())).normalize()
    quote_error = ''
    quotes = pd.DataFrame()
    if not frame.empty:
        try:
            quotes = forward_quotes(_python_builtins.list(frame.code), snapshot['signal_day'], today)
        except _python_builtins.Exception as exc:
            quote_error = _python_builtins.str(exc)
            g.source_errors += 1
            log.info('[增长口径] 前瞻报价缺失，保留原因：%s' % exc)
    for h in HORIZONS:
        mature = _python_builtins.len(days) > h
        snapshot['status_%d' % h] = ('censored' if not mature else
                                    'snapshot_error' if snapshot['error'] else
                                    'quote_error' if quote_error else 'mature')
        frame['r%d' % h] = NAN
        frame['status_r%d' % h] = 'censored' if not mature else 'quote_error'
        frame['exit_date_%d' % h] = _python_builtins.str(days[h].date()) if mature else ''
        frame['exit_open_%d' % h] = NAN
        frame['exit_paused_%d' % h] = NAN
    frame['entry_open'], frame['entry_paused'] = NAN, NAN
    if not frame.empty and not quote_error:
        for code in frame.index:
            opening, paused, entry_status = quote_at(quotes, snapshot['signal_day'], code)
            frame.at[code, 'entry_open'], frame.at[code, 'entry_paused'] = opening, paused
            for h in HORIZONS:
                if _python_builtins.len(days) <= h:
                    continue
                ending, exit_paused, exit_status = quote_at(quotes, days[h], code)
                frame.at[code, 'exit_open_%d' % h] = ending
                frame.at[code, 'exit_paused_%d' % h] = exit_paused
                state = ('entry_' + entry_status if entry_status != 'ok' else
                         'exit_' + exit_status if exit_status != 'ok' else 'ok')
                frame.at[code, 'status_r%d' % h] = state
                if state == 'ok':
                    frame.at[code, 'r%d' % h] = ending / opening - 1
    frame['signal_day'], frame['asof'] = _python_builtins.str(snapshot['signal_day'].date()), _python_builtins.str(snapshot['asof'].date())
    frame['snapshot_error'], frame['quote_error'] = snapshot['error'], quote_error
    if frame.empty:
        frame = pd.DataFrame([_python_builtins.dict(signal_day=_python_builtins.str(snapshot['signal_day'].date()),
                                   asof=_python_builtins.str(snapshot['asof'].date()), code='',
                                   snapshot_error=snapshot['error'] or 'empty_pool',
                                   quote_error=quote_error)])
    _append_csv('samples', frame.reindex(columns=SAMPLE_COLUMNS))
    period_rows = period_statistics(snapshot, frame)
    g.periods.extend(period_rows)
    _append_csv('periods', pd.DataFrame(period_rows, columns=PERIOD_COLUMNS))

def after_trading_end(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_close_day == today:
        return
    g.last_close_day = today
    ready = [s for s in g.pending if g.day - s['sample_day'] >= _python_builtins.max(HORIZONS)]
    for snapshot in ready:
        resolve_snapshot(snapshot, today)
        g.pending.remove(snapshot)

def _append_csv(kind, frame):
    write_file(OUTPUT_PREFIX + '_' + kind + '.csv',
               frame.to_csv(index=False, header=kind not in g.started_files),
               append=kind in g.started_files)
    g.started_files.add(kind)

def _write_settings():
    g.settings["build_sources_sha256"] = BUILD_SOURCES_SHA256
    g.settings['sample_every'] = ROTATE_EVERY
    frame = pd.DataFrame([_python_builtins.dict(setting=k, value=v) for k, v in g.settings.items()])
    write_file(OUTPUT_PREFIX + '_settings.csv', frame.to_csv(index=False), append=False)

GROWTH_ARMS = ('baseline', 'ttm_total', 'ttm_parent', 'ttm_adjusted')

GROWTH_METRICS = ('net_profit', 'np_parent_company_owners', 'adjusted_profit')

PERIOD_COLUMNS = ['signal_day', 'asof', 'scope', 'horizon', 'group', 'window_status',
                  'n', 'observed', 'coverage', 'return_mean', 'return_available',
                  'median_available', 'win_available', 'loss15_available', 'gain15_available', 'snapshot_error']

def growth_features(raw, codes, asof):
    asof = pd.Timestamp(asof).normalize()
    raw = raw.copy()
    raw['report'] = pd.to_datetime(raw.statDate, errors='coerce').dt.normalize()
    raw['published'] = pd.to_datetime(raw.pubDate, errors='coerce').dt.normalize()
    raw['value'] = pd.to_numeric(raw.value, errors='coerce')
    rows = []
    for code in codes:
        row = _python_builtins.dict(code=code)
        for metric, arm in _python_builtins.zip(GROWTH_METRICS, GROWTH_ARMS[1:]):
            own = raw[raw.code.eq(code) & raw.metric.eq(metric)].sort_values('report')
            row.update({arm: np.nan, arm + '_current': np.nan, arm + '_prior': np.nan,
                        arm + '_report': '', arm + '_status': 'missing_quarters'})
            if (own.report.isna().any() or own.published.isna().any()
                    or own.report.gt(asof).any() or own.published.gt(asof).any()
                    or own.published.lt(own.report).any()):
                row[arm + '_status'] = 'invalid_visibility'
                continue
            if own.report.duplicated().any():
                row[arm + '_status'] = 'duplicate_report'
                continue
            own = own.tail(8)
            if _python_builtins.len(own) != 8:
                continue
            periods = own.report.dt.to_period('Q')
            if (not own.report.dt.is_quarter_end.all()
                    or _python_builtins.list(periods) != _python_builtins.list(pd.period_range(periods.iloc[-1] - 7, periods=8, freq='Q'))):
                continue
            row[arm + '_report'] = _python_builtins.str(own.report.iloc[-1].date())
            if (asof - own.report.iloc[-1]).days > 240:
                row[arm + '_status'] = 'stale_report'
                continue
            if not np.isfinite(own.value.values).all():
                row[arm + '_status'] = 'invalid_value'
                continue
            prior, current = _python_builtins.float(own.value.iloc[:4].sum()), _python_builtins.float(own.value.iloc[4:].sum())
            row.update({arm + '_current': current, arm + '_prior': prior})
            if not np.isfinite([prior, current]).all():
                row[arm + '_status'] = 'invalid_value'
            elif prior <= 0:
                row[arm + '_status'] = 'nonpositive_prior'
            else:
                value = (current / prior - 1) * 100
                row[arm + '_status'] = 'ok' if np.isfinite(value) else 'invalid_value'
                row[arm] = value if np.isfinite(value) else np.nan
        rows.append(row)
    return pd.DataFrame(rows).set_index('code', drop=False)

def freeze_growth(frame):
    frame = frame.copy()
    frame['baseline'] = frame.np_yoy_pct
    frame['common'] = frame[_python_builtins.list(GROWTH_ARMS)].notna().all(axis=1)
    baseline_report = pd.to_datetime(frame.stat_date, errors='coerce')
    for arm in GROWTH_ARMS[1:]:
        frame['pass_' + arm] = frame[arm].gt(10)
        frame['common'] &= pd.to_datetime(frame[arm + '_report'], errors='coerce').eq(baseline_report)
    frame['pass_baseline'] = frame.baseline.gt(10)
    return frame

def growth_period_statistics(snapshot, frame):
    rows = []
    for scope in ('available_inputs', 'common_inputs'):
        for h in (20, 40, 60):
            groups = {}
            for arm in GROWTH_ARMS:
                if 'pass_' + arm not in frame:
                    own = pd.DataFrame()
                else:
                    mask = frame['pass_' + arm].eq(True)
                    if scope == 'common_inputs':
                        mask &= frame.common.eq(True)
                    own = frame[mask]
                r = clean(own['r%d' % h]) if _python_builtins.len(own) else pd.Series(dtype=_python_builtins.float)
                known = r.dropna()
                row = _python_builtins.dict(signal_day=_python_builtins.str(snapshot['signal_day'].date()), asof=_python_builtins.str(snapshot['asof'].date()),
                           scope=scope, horizon=h, group=arm, window_status=snapshot['status_%d' % h],
                           n=_python_builtins.len(own), observed=_python_builtins.len(known), coverage=_python_builtins.len(known)/_python_builtins.len(own) if _python_builtins.len(own) else np.nan,
                           return_mean=_python_builtins.float(r.mean()) if _python_builtins.len(r) and r.notna().all() else np.nan,
                           return_available=_python_builtins.float(known.mean()) if _python_builtins.len(known) else np.nan,
                           median_available=_python_builtins.float(known.median()) if _python_builtins.len(known) else np.nan,
                           win_available=_python_builtins.float(known.gt(0).mean()) if _python_builtins.len(known) else np.nan,
                           loss15_available=_python_builtins.float(known.le(-.15).mean()) if _python_builtins.len(known) else np.nan,
                           gain15_available=_python_builtins.float(known.ge(.15).mean()) if _python_builtins.len(known) else np.nan,
                           snapshot_error=snapshot['error'])
                groups[arm] = row
                rows.append(row)
            for left, right in (('ttm_total', 'baseline'), ('ttm_parent', 'ttm_total'),
                                ('ttm_adjusted', 'ttm_parent'), ('ttm_adjusted', 'baseline')):
                a, b = groups[left], groups[right]
                diff = _python_builtins.dict(a)
                diff.update(group=left + '_minus_' + right, n=a['n'] + b['n'],
                            observed=a['observed'] + b['observed'],
                            coverage=_python_builtins.min(a['coverage'], b['coverage'])
                            if np.isfinite([a['coverage'], b['coverage']]).all() else np.nan)
                for key in ('return_mean', 'return_available', 'median_available', 'win_available',
                            'loss15_available', 'gain15_available'):
                    diff[key] = a[key] - b[key]
                rows.append(diff)
    return rows

def summarize_periods(periods, sample_every=20):
    rows = []
    periods = periods.copy()
    periods['signal_day'] = pd.to_datetime(periods.signal_day)
    windows = [('full', periods), ('2020_onward', periods[periods.signal_day.dt.year.ge(2020)])]
    windows += [(_python_builtins.str(year), block) for year, block in periods.groupby(periods.signal_day.dt.year)]
    for window, block in windows:
        for (scope, h, group), subset in block.groupby(['scope', 'horizon', 'group']):
            subset = subset.sort_values('signal_day')
            # 末尾尚未到期不是已实现收益缺失；保留数量，但不把它当统计样本。
            mature = subset[subset.window_status.ne('censored')]
            stats = summarize_ic(mature.return_mean, _python_builtins.int(h // sample_every))
            rows.append(_python_builtins.dict(window=window, scope=scope, horizon=h, group=group,
                             periods=_python_builtins.len(subset), censored=_python_builtins.int(subset.window_status.eq('censored').sum()),
                             valid_periods=stats['n'], mean_return=stats['mean'],
                             positive_period_share=stats['positive_fraction'],
                             t_iid=stats['t_iid'], t_hac=stats['t_hac'],
                             available_mean=_python_builtins.float(mature.return_available.mean()),
                             coverage_mean=_python_builtins.float(mature.coverage.mean()),
                             members_mean=_python_builtins.float(mature.n.mean()),
                             win_available=_python_builtins.float(mature.win_available.mean()),
                             loss15_available=_python_builtins.float(mature.loss15_available.mean())))
    return pd.DataFrame(rows)

class PoolDataError(_python_builtins.Exception):
    pass

def _evaluate_pool(universe, fundamentals, status, closes, unlock_codes=None, audit=None):
    """停牌只限制成交，不定义质量出池；解禁不参与现行规则。

    audit.pool_state 为 passed / failed / unknown。确认失败优先于未知，
    因此已知ROE不达标时，即使估值缺失仍可正确退出。
    """
    pool = []
    tracked = _b.set(universe) | _b.set(audit or {})
    for code in _b.sorted(tracked):
        failed, missing = ([], [])
        values = {}
        if code not in universe:
            failed.append('not_in_mainboard_universe')
        for field, low, high, reason in (('roe_ttm_pct', ROE_TTM_MIN, None, 'roe_low'), ('np_yoy_pct', NP_YOY_MIN, None, 'profit_growth_low'), ('pe_ttm', 0, PE_MAX, 'pe_high'), ('pb', 0, PB_MAX, 'pb_high')):
            value = fundamentals.at[code, field] if code in fundamentals.index and field in fundamentals.columns else None
            try:
                value = _b.float(value)
            except (_python_builtins.TypeError, _python_builtins.ValueError):
                value = _b.float('nan')
            values[field] = value
            if not _math.isfinite(value):
                missing.append('financial_missing' if high is None else 'valuation_invalid')
            elif value <= low:
                failed.append(reason if high is None else field.split('_')[0] + '_nonpos')
            elif high is not None and value >= high:
                failed.append(reason)
        if code not in status.index or 'is_st' not in status.columns or pd.isna(status.at[code, 'is_st']):
            missing.append('status_missing')
        elif _b.bool(status.at[code, 'is_st']):
            failed.append('st')
        vol = _annualized_vol(closes[code]) if code in closes.columns else None
        if vol is None:
            missing.append('vol_history_missing')
        elif vol >= VOL60_MAX:
            failed.append('vol_high')
        state = 'failed' if failed else 'unknown' if missing else 'passed'
        if audit is not None:
            audit.setdefault(code, {}).update(values, pool_state=state, pool_reason='|'.join(_python_builtins.dict.fromkeys(failed or missing)) or 'passed', vol60=vol)
        if state == 'passed':
            pool.append(code)
    return pool

def _annualized_vol(series):
    window = pd.to_numeric(series.iloc[-VOL_LOOKBACK:], errors='coerce')
    if _b.len(window) < VOL_LOOKBACK or window.isna().any() or (not window.map(_math.isfinite).all()) or (window <= 0).any():
        return None
    return _b.float(window.pct_change().iloc[1:].std(ddof=1) * _math.sqrt(VOL_ANNUAL_DAYS) * 100.0)

def rank_pool(pool, closes):
    """253根收盘剔除最近21根；同分按代码排序，缺计分历史仅影响新仓。"""
    scores = {}
    for code in pool:
        if code not in closes.columns:
            continue
        series = closes[code].iloc[-SCORE_CLOSES:-SCORE_SKIP_RECENT]
        if _b.len(series) == SCORE_CLOSES - SCORE_SKIP_RECENT and series.notna().all() and series.map(_math.isfinite).all() and (series > 0).all():
            scores[code] = _b.float(series.iloc[-1] / series.iloc[0] - 1.0)
    return (_b.sorted(scores, key=lambda c: (-scores[c], c)), scores)

def build_pool(context, asof, audit=None, subset=None):
    secs = get_all_securities(['stock'], date=context.current_dt)
    if not _b.isinstance(secs, pd.DataFrame) or secs.empty:
        raise PoolDataError('Missing historical stock universe')
    universe = {c for c in secs.index if c[:2] in ('60', '00')}
    requested = universe if subset is None else _python_builtins.set(subset)
    ind = get_fundamentals(query(indicator.code, indicator.inc_net_profit_year_on_year,
                                indicator.statDate, indicator.pubDate), date=asof)
    _require_columns(ind, ('code', 'inc_net_profit_year_on_year', 'statDate', 'pubDate'), 'financial')
    if ind.empty or ind['code'].duplicated().any():
        raise PoolDataError('Empty or duplicate financial snapshot')
    ind = ind.set_index('code')
    visible = ((ind['statDate'].apply(_report_date) <= asof)
               & (pd.to_datetime(ind['pubDate'], errors='coerce') <= asof))
    roe = _fetch_roe_ttm(requested & universe, asof) if requested & universe else pd.Series(dtype=_python_builtins.float)
    fin = pd.DataFrame(index=_python_builtins.sorted(requested))
    fin['roe_ttm_pct'] = roe
    fin['np_yoy_pct'] = pd.to_numeric(ind['inc_net_profit_year_on_year'], errors='coerce').where(visible)
    # 未知/未披露财报不能使用因子值冒充质量已知。
    fin.loc[~fin.index.isin(ind.index[visible]), 'roe_ttm_pct'] = _b.float('nan')
    val = get_fundamentals(query(valuation.code, valuation.pe_ratio, valuation.pb_ratio)
                           .filter(valuation.code.in_(_b.list(requested))), date=asof)
    _require_columns(val, ('code', 'pe_ratio', 'pb_ratio'), 'valuation')
    if val['code'].duplicated().any():
        raise PoolDataError('Duplicate valuation snapshot')
    val = val.set_index('code')
    fin['pe_ttm'] = pd.to_numeric(val['pe_ratio'], errors='coerce')
    fin['pb'] = pd.to_numeric(val['pb_ratio'], errors='coerce')
    if not fin.notna().any().any():
        raise PoolDataError('No usable financial values')
    cd = get_current_data()
    status = pd.DataFrame({'is_st': {c: _b.getattr(cd[c], 'is_st', None) for c in requested}})
    held = _held(context) & requested
    candidates = {c for c in fin.index if fin.at[c, 'roe_ttm_pct'] > ROE_MIN
                  and fin.at[c, 'np_yoy_pct'] > NP_YOY_MIN
                  and 0 < fin.at[c, 'pe_ttm'] < PE_MAX and 0 < fin.at[c, 'pb'] < PB_MAX}
    history_codes = _python_builtins.sorted((candidates | held) & universe)
    closes = _history_closes(history_codes, asof) if history_codes else pd.DataFrame()
    pool = _evaluate_pool(universe & requested, fin, status, closes, audit=audit)
    if audit is not None:
        for c, row in audit.items():
            row.update(roe_ann=row.get('roe_ttm_pct'), profit_yoy=row.get('np_yoy_pct'),
                       pe=row.get('pe_ttm'), pb=row.get('pb'))
            if c in ind.index:
                row.update(stat_date=_b.str(ind.at[c, 'statDate']), pub_date=_b.str(ind.at[c, 'pubDate']))
    return pool, closes

def _prev_trade_day(dt):
    days = get_trade_days(end_date=pd.Timestamp(dt).date(), count=2)
    if _b.len(days) != 2:
        raise PoolDataError('Missing previous trading day')
    return pd.Timestamp(days[0]).normalize()

def _require_columns(rows, columns, source):
    if not _b.isinstance(rows, pd.DataFrame) or not _python_builtins.set(columns) <= _python_builtins.set(rows.columns):
        raise PoolDataError('Incomplete %s schema' % source)

def _report_date(value):
    if pd.isna(value):
        return pd.NaT
    text = _b.str(value).strip().lower()
    try:
        if (_b.len(text) == 6 and text[:4].isdigit()
                and text[4] == 'q' and text[5] in '1234'):
            return pd.Period(text, freq='Q').end_time.normalize()
        date = pd.Timestamp(value).normalize()
        return date if date.is_quarter_end else pd.NaT
    except (_python_builtins.TypeError, _python_builtins.ValueError, _python_builtins.OverflowError):
        return pd.NaT

def _fetch_roe_ttm(codes, asof):
    """因子库roe_ttm（净利润TTM/期末股东权益）；按code返回百分比Series。"""
    data = get_factor_values(securities=_b.list(codes), factors=['roe_ttm'],
                             end_date=pd.Timestamp(asof).date(), count=1)
    if not _b.isinstance(data, _python_builtins.dict) or 'roe_ttm' not in data:
        raise PoolDataError('Missing roe_ttm factor snapshot')
    frame = data['roe_ttm']
    if not _b.isinstance(frame, pd.DataFrame) or frame.empty:
        raise PoolDataError('Empty roe_ttm factor snapshot')
    if pd.Timestamp(frame.index[-1]).normalize() != pd.Timestamp(asof).normalize():
        raise PoolDataError('ROE cutoff mismatch')
    values = frame.iloc[-1].apply(pd.to_numeric, errors='coerce').replace(
        [_b.float('inf'), -_b.float('inf')], _b.float('nan')).dropna()
    if values.empty:
        raise PoolDataError('No valid roe_ttm values')
    # 固定的数据源口径，不能随当日分布或补判股票子集改变单位。
    return values * (100.0 if ROE_FACTOR_UNIT == 'ratio' else 1.0)

def _history_closes(codes, asof):
    count = _b.max(VOL_LOOKBACK, MOMENTUM_DAYS + 1)
    closes = history(count, '1d', 'close', security_list=codes,
                     skip_paused=False, df=True)
    if (not _b.isinstance(closes, pd.DataFrame)
            or not _python_builtins.set(codes) <= _python_builtins.set(closes.columns) or _b.len(closes) != count):
        raise PoolDataError('Incomplete daily close matrix')
    dates = pd.to_datetime(closes.index).normalize()
    expected = pd.DatetimeIndex(get_trade_days(end_date=pd.Timestamp(asof).date(), count=count)).normalize()
    if (dates.max() != pd.Timestamp(asof).normalize()
            or dates.duplicated().any() or not dates.is_monotonic_increasing
            or not dates.equals(expected)):
        raise PoolDataError('Daily close matrix cutoff does not match T-1')
    return closes[codes].apply(pd.to_numeric, errors='coerce').replace(
        [_b.float('inf'), -_b.float('inf')], _b.float('nan'))

def _held(context):
    # 卖单冻结的数量仍然占用一个持仓名额。
    return {c for c, p in context.portfolio.positions.items()
            if p.total_amount + _b.getattr(p, 'locked_amount', 0) > 0}

def clean(series):
    return pd.to_numeric(series, errors='coerce').replace([np.inf, -np.inf], np.nan)

def summarize_ic(ic, lags=3):
    """ICIR保留符号；普通t及Bartlett/Newey-West均值t均报告，不硬设合格线。"""
    full = clean(ic)
    values = full.dropna().values.astype(_python_builtins.float)
    n = _python_builtins.len(values)
    result = {'n': n, 'mean': np.nan, 'icir': np.nan, 't_iid': np.nan,
              't_hac': np.nan, 'positive_fraction': np.nan}
    if n == 0:
        return result
    mean = values.mean()
    std = values.std(ddof=1) if n > 1 else np.nan
    result.update(mean=mean, positive_fraction=_python_builtins.float((values > 0).mean()))
    if n > 1 and std > 0:
        result.update(icir=mean / std, t_iid=mean / std * np.sqrt(n))
    # 缺测间隔不能压缩成相邻期；有缺测时不输出规则等间距HAC。
    if n > 1 and full.notna().all():
        residual = values - mean
        lag_count = _python_builtins.min(lags, n - 1)
        variance = residual @ residual / n
        for lag in _python_builtins.range(1, lag_count + 1):
            gamma = residual[lag:] @ residual[:-lag] / n
            variance += 2 * (1 - lag / (lag_count + 1)) * gamma
        variance *= n / (n - 1)
        if variance > 0:
            result['t_hac'] = mean / np.sqrt(variance / n)
    return result
