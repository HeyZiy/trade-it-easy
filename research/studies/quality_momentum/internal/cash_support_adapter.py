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
NAN = float('nan')
SOURCE_V5_SHA256 = ''

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
    params = getattr(context, 'run_params', None)
    if params is not None and getattr(params, 'frequency', 'day') not in ('day', 'daily'):
        raise ValueError('请选择天频率；本探针不下单')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    log.set_level('order', 'error')
    g.day, g.last_signal_day, g.last_close_day = 0, None, None
    g.pending, g.periods, g.started_files = [], [], set()
    g.sample_count, g.source_errors, g.industry_errors, g.neutral_errors = 0, 0, 0, 0
    g.wall_started = _time.time()
    g.settings = dict(probe=PROBE, source_v5_sha256=SOURCE_V5_SHA256, no_orders=True,
                     start_date=str(getattr(params, 'start_date', '')),
                     end_date=str(getattr(params, 'end_date', '')), frequency='day',
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
        field = getattr(table, field_name)
        blocks = []
        for start in range(0, len(codes), 100):
            frame = get_history_fundamentals(
                codes[start:start + 100], [table.statDate, table.pubDate, field],
                watch_date=asof.date(), count=4, interval='1q', stat_by_year=False)
            _require_columns(frame, ('code', 'statDate', 'pubDate', field_name), label)
            if not frame.code.isin(codes[start:start + 100]).all():
                raise PoolDataError('Unexpected financial code')
            blocks.append(frame)
            raw = frame.copy()
            raw['signal_day'], raw['asof'], raw['table'] = str(signal_day.date()), str(asof.date()), label
            raw['value'] = raw[field_name]
            _append_csv('quarters', raw[['signal_day', 'asof', 'table', 'code', 'statDate', 'pubDate', 'value']])
        tables.append(pd.concat(blocks, ignore_index=True))
    return cash_support_features(tables[0], tables[1], codes, asof)


def neutral_cash(frame):
    output = pd.Series(NAN, index=frame.index, dtype=float)
    data = frame[['cash_support', 'market_cap', 'momentum', 'vol60', 'industry_code']].replace(
        [np.inf, -np.inf], np.nan).dropna()
    data = data[data.market_cap.gt(0) & data.industry_code.ne('')]
    if len(data) < 20:
        return output
    numeric = pd.DataFrame(dict(log_cap=np.log(data.market_cap), momentum=data.momentum, vol60=data.vol60))
    for key in numeric:
        std = numeric[key].std(ddof=0)
        numeric[key] = (numeric[key] - numeric[key].mean()) / std if std > 0 else numeric[key] * 0
    x = np.column_stack([np.ones(len(data)), numeric.values,
                         pd.get_dummies(data.industry_code, drop_first=True).astype(float).values])
    if len(data) <= np.linalg.matrix_rank(x) + 5:
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
    except Exception as exc:
        frame['cash_residual'], frame['group_pool_neutral'] = NAN, NAN
        frame['neutral_error'] = str(exc)
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
    snapshot = dict(signal_day=today, asof=asof, sample_day=g.day, error='', frame=pd.DataFrame())
    try:
        audit = {}
        pool, closes = build_pool(context, asof, audit=audit)
        ranked, scores = rank_pool(pool, closes)
        if pool:
            codes = sorted(pool)
            features = cash_snapshot(codes, asof, today)
            cap = get_fundamentals(query(valuation.code, valuation.market_cap).filter(
                valuation.code.in_(codes)), date=asof)
            _require_columns(cap, ('code', 'market_cap'), 'market cap')
            if cap.code.duplicated().any():
                raise PoolDataError('Duplicate market cap')
            features['market_cap'] = pd.to_numeric(cap.set_index('code').market_cap, errors='coerce')
            try:
                industry = get_industry(codes, date=asof.date()) or {}
            except Exception:
                industry = {}
                g.industry_errors += 1
            rows = []
            ranks = {c: i + 1 for i, c in enumerate(ranked)}
            for code in codes:
                row = dict(audit[code])
                row.update(features.loc[code].to_dict())
                row.update(momentum=scores.get(code, NAN), momentum_rank=ranks.get(code, NAN),
                           momentum_top6=code in ranked[:6],
                           industry_code=((industry.get(code) or {}).get('sw_l1') or {}).get('industry_code', ''))
                rows.append(row)
            snapshot['frame'] = freeze_cash(pd.DataFrame(rows).set_index('code', drop=False))
            if snapshot['frame'].neutral_error.ne('').any():
                g.neutral_errors += 1
    except Exception as exc:
        snapshot['error'] = str(exc)
        g.source_errors += 1
        log.info('[现金支撑] 本期缺测：%s' % exc)
    g.pending.append(snapshot)
    g.sample_count += 1
    log.info('[现金支撑] 第%d期 %s 池=%d' % (g.sample_count, today.date(), len(snapshot['frame'])))


def period_statistics(snapshot, frame):
    return cash_period_statistics(snapshot, frame)


def on_strategy_end(context):
    today = pd.Timestamp(context.current_dt).normalize()
    for snapshot in list(g.pending):
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
