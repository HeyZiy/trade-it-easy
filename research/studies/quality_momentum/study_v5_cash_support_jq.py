# -*- coding: utf-8 -*-
# 自动生成：python -m research.studies.quality_momentum.internal.build_cash_support
# 整文件粘贴聚宽，天频率；2016-01-04至2026-09-01；不下单。
BUILD_SOURCES_SHA256 = '{"research/studies/quality_momentum/internal/build_cash_support.py": "c2d073f5c54fcf7bdd7d467d5be47b9b8cd850498d433673a13f4d84475beda5", "research/studies/quality_momentum/internal/build_platform.py": "64b1e9f477c4051249337ac3babd27063c03564fd79594925b9105e897f02e5d", "research/studies/quality_momentum/internal/cash_support.py": "494007de2165eb998264b872b7bc7f45031b761f1a31b4e499f2e345592ca7e8", "research/studies/quality_momentum/internal/cash_support_adapter.py": "6679844a660bc0816067e23d4e4d25025d58fc6b1723eb86e18c51e7c9b208b7", "research/studies/quality_momentum/internal/probe_adapter.py": "63b6b104bfb2783c5b77d69fa6d12f118afa3017039e904e2fac28688e617093", "research/studies/quality_momentum/internal/probe_statistics.py": "62d12d46d674410af979071e612e9c15a0e44ea31995b03a3f7ffb11746b4271", "research/studies/quality_momentum/pool_rotation_v5.py": "cb4967826e2ad82cf5d0963e35bc68b05fce39915029b4f0672868316a7221de", "research/tools/factor_analysis/core.py": "a816fe8bf094b01c3b39a0d7a41dab2d77df26dacee92a22fea6159f558b835f", "research/tools/factor_analysis/platform_codegen.py": "2a4f066e9f051ea79a048e79cb92a35a2d258830d092699492544ea9759e4b88"}'
# -*- coding: utf-8 -*-
"""质量池现金支撑探针：日频，不下单，2016-01-04至2026-09-01。"""
from jqdata import *
from jqfactor import get_factor_values
import builtins as _b
import builtins as _python_builtins
import math as _math
import time as _time
import numpy as np
import pandas as pd

PROBE = 'cash_support'
OUTPUT_PREFIX = 'quality_momentum_v5_cash_support_probe'
ROTATE_EVERY, GROUPS, MIN_GROUP_SIZE = 20, 3, 5
HORIZONS = (20, 40, 60)
SCOPES = ('pool_raw', 'strong_raw', 'pool_neutral')
MOMENTUM_DAYS, SKIP_RECENT_DAYS = 252, 21
SCORE_CLOSES, SCORE_SKIP_RECENT = 253, 21
ROE_MIN, ROE_TTM_MIN, NP_YOY_MIN = 12.0, 12.0, 10.0
ROE_FACTOR_UNIT = 'ratio'
PE_MAX, PB_MAX, VOL60_MAX = 30.0, 5.0, 35.0
VOL_LOOKBACK, VOL_ANNUAL_DAYS = 61, 250.0
BENCHMARK = '000906.XSHG'
NAN = _python_builtins.float('nan')
SOURCE_V5_SHA256 = 'cb4967826e2ad82cf5d0963e35bc68b05fce39915029b4f0672868316a7221de'

SAMPLE_COLUMNS = ['signal_day', 'asof', 'code', 'cash_support', 'cash_ttm', 'profit_ttm',
                  'cash_status', 'cash_report', 'market_cap', 'momentum', 'momentum_rank',
                  'momentum_top6', 'momentum_strong', 'industry_code', 'vol60',
                  'roe_ttm_pct', 'np_yoy_pct', 'pe_ttm', 'pb', 'stat_date', 'pub_date',
                  'group_pool_raw', 'group_strong_raw', 'cash_residual', 'group_pool_neutral',
                  'entry_open', 'entry_paused']
for _h in HORIZONS:
    SAMPLE_COLUMNS += ['r%d' % _h, 'status_r%d' % _h, 'exit_date_%d' % _h,
                       'exit_open_%d' % _h, 'exit_paused_%d' % _h]
SAMPLE_COLUMNS += ['snapshot_error', 'quote_error', 'neutral_error']


def initialize(context):
    params = _python_builtins.getattr(context, 'run_params', None)
    if params is not None and _python_builtins.getattr(params, 'frequency', 'day') not in ('day', 'daily'):
        raise _python_builtins.ValueError('请选择天频率；本探针不下单')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    log.set_level('order', 'error')
    g.day, g.last_signal_day, g.last_close_day = 0, None, None
    g.pending, g.periods, g.started_files = [], [], _python_builtins.set()
    g.sample_count, g.source_errors, g.industry_errors, g.neutral_errors = 0, 0, 0, 0
    g.wall_started = _time.time()
    g.settings = _python_builtins.dict(probe=PROBE, source_v5_sha256=SOURCE_V5_SHA256, no_orders=True,
                     start_date=_python_builtins.str(_python_builtins.getattr(params, 'start_date', '')),
                     end_date=_python_builtins.str(_python_builtins.getattr(params, 'end_date', '')), frequency='day',
                     formula='sum(4 single-quarter operating cash flow)/sum(4 single-quarter operating profit)',
                     financial_api='get_history_fundamentals; watch_date=T-1; stat_by_year=False',
                     denominator='positive TTM profit required; no ratio clipping; inspect near-zero denominators',
                     vintage='platform historical visibility; restatement vintage not independently restored',
                     signal_cutoff='T-1', horizons='20;40;60', primary_horizon=60,
                     primary_scope='pool_raw', primary_contrast='high_minus_low',
                     controls='rank(cash_support) ~ industry + log(market_cap) + momentum12_2 + vol60',
                     strong_definition='top momentum tercile; not the actual six holdings',
                     returns='post-adjusted open(T+h)/open(T)-1; paused endpoints unavailable',
                     missing='retain frozen membership; strict group and available means reported',
                     tails='endpoint return <= -15% and >= +15%; not path drawdown',
                     hac='horizon/20; no compression of missing periods',
                     annualization='none', pandas_version=pd.__version__, numpy_version=np.__version__)
    _write_settings()
    run_daily(on_probe_signal, time='before_open')
    log.info('[现金支撑] 日频、不下单；下载原始季度及分组CSV，不看空账户曲线。')


def cash_snapshot(codes, asof, signal_day):
    tables = []
    for table, field_name, label in ((cash_flow, 'net_operate_cash_flow', 'cash_flow'),
                                     (income, 'operating_profit', 'income')):
        field = _python_builtins.getattr(table, field_name)
        blocks = []
        for start in _python_builtins.range(0, _python_builtins.len(codes), 100):
            frame = get_history_fundamentals(
                codes[start:start + 100], [table.statDate, table.pubDate, field],
                watch_date=asof.date(), count=4, interval='1q', stat_by_year=False)
            _require_columns(frame, ('code', 'statDate', 'pubDate', field_name), label)
            if not frame.code.isin(codes[start:start + 100]).all():
                raise PoolDataError('Unexpected financial code')
            blocks.append(frame)
            raw = frame.copy()
            raw['signal_day'], raw['asof'], raw['table'] = _python_builtins.str(signal_day.date()), _python_builtins.str(asof.date()), label
            raw['value'] = raw[field_name]
            _append_csv('quarters', raw[['signal_day', 'asof', 'table', 'code', 'statDate', 'pubDate', 'value']])
        tables.append(pd.concat(blocks, ignore_index=True))
    return cash_support_features(tables[0], tables[1], codes, asof)


def neutral_cash(frame):
    output = pd.Series(NAN, index=frame.index, dtype=_python_builtins.float)
    data = frame[['cash_support', 'market_cap', 'momentum', 'vol60', 'industry_code']].replace(
        [np.inf, -np.inf], np.nan).dropna()
    data = data[data.market_cap.gt(0) & data.industry_code.ne('')]
    if _python_builtins.len(data) < 20:
        return output
    numeric = pd.DataFrame(_python_builtins.dict(log_cap=np.log(data.market_cap), momentum=data.momentum, vol60=data.vol60))
    for key in numeric:
        std = numeric[key].std(ddof=0)
        numeric[key] = (numeric[key] - numeric[key].mean()) / std if std > 0 else numeric[key] * 0
    x = np.column_stack([np.ones(_python_builtins.len(data)), numeric.values,
                         pd.get_dummies(data.industry_code, drop_first=True).astype(_python_builtins.float).values])
    if _python_builtins.len(data) <= np.linalg.matrix_rank(x) + 5:
        return output
    y = data.cash_support.rank(method='average', pct=True).values
    residual = y - x @ np.linalg.lstsq(x, y, rcond=None)[0]
    if np.std(residual) > 1e-12:
        output.loc[data.index] = residual
    return output


def freeze_cash(frame):
    frame = frame.copy()
    frame['group_pool_raw'] = fixed_groups(frame.cash_support)
    frame['momentum_strong'] = assign_groups(frame.momentum, GROUPS).eq(GROUPS)
    frame['group_strong_raw'] = fixed_groups(frame.cash_support.where(frame.momentum_strong))
    frame['neutral_error'] = ''
    try:
        frame['cash_residual'] = neutral_cash(frame)
        frame['group_pool_neutral'] = fixed_groups(frame.cash_residual)
    except _python_builtins.Exception as exc:
        frame['cash_residual'], frame['group_pool_neutral'] = NAN, NAN
        frame['neutral_error'] = _python_builtins.str(exc)
    return frame


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
            features = cash_snapshot(codes, asof, today)
            cap = get_fundamentals(query(valuation.code, valuation.market_cap).filter(
                valuation.code.in_(codes)), date=asof)
            _require_columns(cap, ('code', 'market_cap'), 'market cap')
            if cap.code.duplicated().any():
                raise PoolDataError('Duplicate market cap')
            features['market_cap'] = pd.to_numeric(cap.set_index('code').market_cap, errors='coerce')
            try:
                industry = get_industry(codes, date=asof.date()) or {}
            except _python_builtins.Exception:
                industry = {}
                g.industry_errors += 1
            rows = []
            ranks = {c: i + 1 for i, c in _python_builtins.enumerate(ranked)}
            for code in codes:
                row = _python_builtins.dict(audit[code])
                row.update(features.loc[code].to_dict())
                row.update(momentum=scores.get(code, NAN), momentum_rank=ranks.get(code, NAN),
                           momentum_top6=code in ranked[:6],
                           industry_code=((industry.get(code) or {}).get('sw_l1') or {}).get('industry_code', ''))
                rows.append(row)
            snapshot['frame'] = freeze_cash(pd.DataFrame(rows).set_index('code', drop=False))
            if snapshot['frame'].neutral_error.ne('').any():
                g.neutral_errors += 1
    except _python_builtins.Exception as exc:
        snapshot['error'] = _python_builtins.str(exc)
        g.source_errors += 1
        log.info('[现金支撑] 本期缺测：%s' % exc)
    g.pending.append(snapshot)
    g.sample_count += 1
    log.info('[现金支撑] 第%d期 %s 池=%d' % (g.sample_count, today.date(), _python_builtins.len(snapshot['frame'])))


def period_statistics(snapshot, frame):
    return cash_period_statistics(snapshot, frame)


def on_strategy_end(context):
    today = pd.Timestamp(context.current_dt).normalize()
    for snapshot in _python_builtins.list(g.pending):
        resolve_snapshot(snapshot, today)
    g.pending.clear()
    if g.periods:
        summary = summarize_periods(pd.DataFrame(g.periods).reindex(columns=PERIOD_COLUMNS), ROTATE_EVERY)
        write_file(OUTPUT_PREFIX + '_summary.csv', summary.to_csv(index=False), append=False)
    g.settings.update(sample_count=g.sample_count, source_errors=g.source_errors,
                      industry_errors=g.industry_errors, neutral_errors=g.neutral_errors,
                      elapsed_seconds=_time.time() - g.wall_started)
    _write_settings()
    log.info('[现金支撑结束] 下载settings、quarters、samples、periods、summary五份CSV。')


def fixed_groups(values):
    groups = assign_groups(values, GROUPS)
    counts = groups.value_counts()
    if _python_builtins.len(counts) != GROUPS or (counts < MIN_GROUP_SIZE).any():
        groups[:] = NAN
    return groups

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
            log.info('[现金支撑] 前瞻报价缺失，保留原因：%s' % exc)
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

PERIOD_COLUMNS = ['signal_day', 'asof', 'scope', 'horizon', 'group', 'window_status',
                  'n', 'observed', 'coverage', 'return_mean', 'return_available',
                  'median_available', 'win_available', 'loss15_available', 'gain15_available',
                  'cash_support_mean', 'snapshot_error']

def cash_support_features(cash, profit, codes, asof):
    """两表各自最新四季必须连续、同报告期；不退回旧的完整窗口。"""
    asof = pd.Timestamp(asof).normalize()
    output = []
    for code in codes:
        row = _python_builtins.dict(code=code, cash_support=np.nan, cash_ttm=np.nan,
                   profit_ttm=np.nan, cash_status='missing_quarters', cash_report='')
        series = []
        for frame, field in ((cash, 'net_operate_cash_flow'), (profit, 'operating_profit')):
            own = frame[frame.code.eq(code)].copy()
            own['report'] = pd.to_datetime(own.statDate, errors='coerce').dt.normalize()
            own['published'] = pd.to_datetime(own.pubDate, errors='coerce').dt.normalize()
            values = pd.to_numeric(own[field], errors='coerce')
            if (own.report.isna().any() or own.published.isna().any()
                    or own.report.gt(asof).any() or own.published.gt(asof).any()
                    or own.published.lt(own.report).any()):
                row['cash_status'] = 'invalid_visibility'
                break
            if own.report.duplicated().any():
                row['cash_status'] = 'duplicate_report'
                break
            own[field] = values
            own = own.sort_values('report').tail(4)
            quarters = own.report.dt.to_period('Q')
            if (_python_builtins.len(own) != 4 or not own.report.dt.is_quarter_end.all()
                    or _python_builtins.list(quarters) != _python_builtins.list(pd.period_range(quarters.iloc[-1] - 3, periods=4, freq='Q'))):
                break
            if (asof - own.report.iloc[-1]).days > 240:
                row['cash_status'] = 'stale_report'
                break
            if not np.isfinite(own[field].values).all():
                row['cash_status'] = 'invalid_value'
                break
            series.append(own.set_index('report')[field])
        if _python_builtins.len(series) == 2:
            if not series[0].index.equals(series[1].index):
                row['cash_status'] = 'report_mismatch'
            else:
                cf, op = _python_builtins.float(series[0].sum()), _python_builtins.float(series[1].sum())
                row.update(cash_ttm=cf, profit_ttm=op,
                           cash_report=_python_builtins.str(series[0].index[-1].date()))
                if not np.isfinite([cf, op]).all():
                    row['cash_status'] = 'invalid_value'
                elif op <= 0:
                    row['cash_status'] = 'nonpositive_profit'
                else:
                    value = cf / op
                    row.update(cash_support=value if np.isfinite(value) else np.nan,
                               cash_status='ok' if np.isfinite(value) else 'invalid_ratio')
        output.append(row)
    return pd.DataFrame(output).set_index('code', drop=False)

def cash_period_statistics(snapshot, frame, scopes=('pool_raw', 'strong_raw', 'pool_neutral'), horizons=(20, 40, 60)):
    rows = []
    for scope in scopes:
        for h in horizons:
            groups = {}
            for group, label in _python_builtins.enumerate(('low', 'mid', 'high'), 1):
                own = (frame[frame['group_' + scope].eq(group)]
                       if 'group_' + scope in frame else pd.DataFrame())
                returns = clean(own['r%d' % h]) if _python_builtins.len(own) else pd.Series(dtype=_python_builtins.float)
                known = returns.dropna()
                row = _python_builtins.dict(signal_day=_python_builtins.str(snapshot['signal_day'].date()),
                           asof=_python_builtins.str(snapshot['asof'].date()), scope=scope, horizon=h, group=label,
                           window_status=snapshot['status_%d' % h], n=_python_builtins.len(own), observed=_python_builtins.len(known),
                           coverage=_python_builtins.len(known) / _python_builtins.len(own) if _python_builtins.len(own) else np.nan,
                           return_mean=_python_builtins.float(returns.mean()) if _python_builtins.len(own) and returns.notna().all() else np.nan,
                           return_available=_python_builtins.float(known.mean()) if _python_builtins.len(known) else np.nan,
                           median_available=_python_builtins.float(known.median()) if _python_builtins.len(known) else np.nan,
                           win_available=_python_builtins.float(known.gt(0).mean()) if _python_builtins.len(known) else np.nan,
                           loss15_available=_python_builtins.float(known.le(-.15).mean()) if _python_builtins.len(known) else np.nan,
                           gain15_available=_python_builtins.float(known.ge(.15).mean()) if _python_builtins.len(known) else np.nan,
                           cash_support_mean=_python_builtins.float(own.cash_support.mean()) if _python_builtins.len(own) else np.nan,
                           snapshot_error=snapshot['error'])
                groups[label] = row
                rows.append(row)
            low, high = groups['low'], groups['high']
            difference = _python_builtins.dict(high)
            difference.update(group='high_minus_low', n=low['n'] + high['n'],
                              observed=low['observed'] + high['observed'],
                              coverage=_python_builtins.min(low['coverage'], high['coverage'])
                              if np.isfinite([low['coverage'], high['coverage']]).all() else np.nan)
            for key in ('return_mean', 'return_available', 'median_available', 'win_available',
                        'loss15_available', 'gain15_available', 'cash_support_mean'):
                difference[key] = high[key] - low[key]
            rows.append(difference)
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

def assign_groups(factor, n_groups):
    """分位数分组；保留同分，边界重复导致缺组时整轮标记缺测。"""
    values = clean(factor).dropna()
    empty = pd.Series(np.nan, index=factor.index, dtype=_python_builtins.float)
    if _python_builtins.len(values) < n_groups or values.nunique() < n_groups:
        return empty
    labels = pd.qcut(values, q=n_groups, labels=False, duplicates='drop')
    if labels.nunique() != n_groups:
        return empty
    empty.loc[values.index] = labels + 1
    return empty

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
