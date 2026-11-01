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
NP_YOY_MIN = -float('inf')
ROE_FACTOR_UNIT = 'ratio'
PE_MAX, PB_MAX, VOL60_MAX = 30.0, 5.0, 35.0
VOL_LOOKBACK, VOL_ANNUAL_DAYS = 61, 250.0
BENCHMARK, NAN = '000906.XSHG', float('nan')
SOURCE_V5_SHA256 = ''

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
    params = getattr(context, 'run_params', None)
    if params is not None and getattr(params, 'frequency', 'day') not in ('day', 'daily'):
        raise ValueError('请选择天频率；本实验不下单')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    g.day, g.last_signal_day, g.last_close_day = 0, None, None
    g.pending, g.periods, g.started_files = [], [], set()
    g.sample_count, g.source_errors, g.wall_started = 0, 0, _time.time()
    g.settings = dict(probe=PROBE, source_v5_sha256=SOURCE_V5_SHA256,
        start_date=str(getattr(params, 'start_date', '')), end_date=str(getattr(params, 'end_date', '')),
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
        for start in range(0, len(codes), 100):
            chunk = codes[start:start+100]
            frame = get_history_fundamentals(chunk,
                [table.statDate, table.pubDate] + [getattr(table, name) for name in names],
                watch_date=asof.date(), count=8, interval='1q', stat_by_year=False)
            _require_columns(frame, ('code', 'statDate', 'pubDate') + names, label)
            if not frame.code.isin(chunk).all():
                raise PoolDataError('Unexpected growth financial code')
            for name in names:
                raw = frame[['code', 'statDate', 'pubDate']].copy()
                raw['metric'], raw['value'] = name, frame[name]
                raw['signal_day'], raw['asof'], raw['table'] = str(signal.date()), str(asof.date()), label
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
    snapshot = dict(signal_day=today, asof=asof, sample_day=g.day, error='', frame=pd.DataFrame())
    try:
        audit = {}
        pool, closes = build_pool(context, asof, audit=audit)
        ranked, scores = rank_pool(pool, closes)
        if pool:
            codes = sorted(pool)
            features = growth_snapshot(codes, asof, today)
            cap = get_fundamentals(query(valuation.code, valuation.market_cap).filter(
                valuation.code.in_(codes)), date=asof)
            _require_columns(cap, ('code', 'market_cap'), 'market cap')
            if cap.code.duplicated().any():
                raise PoolDataError('Duplicate market cap')
            features['market_cap'] = pd.to_numeric(cap.set_index('code').market_cap, errors='coerce')
            try:
                industries = get_industry(codes, date=asof.date()) or {}
            except Exception:
                industries = {}
            ranks = {c: i+1 for i, c in enumerate(ranked)}
            rows = []
            for code in codes:
                row = dict(audit[code])
                row.update(features.loc[code].to_dict())
                row.update(momentum=scores.get(code, NAN), momentum_rank=ranks.get(code, NAN),
                           industry_code=((industries.get(code) or {}).get('sw_l1') or {}).get('industry_code', ''))
                rows.append(row)
            snapshot['frame'] = freeze_growth(pd.DataFrame(rows).set_index('code', drop=False))
    except Exception as exc:
        snapshot['error'] = str(exc)
        g.source_errors += 1
        log.info('[增长口径] 本期缺测：%s' % exc)
    g.pending.append(snapshot)
    g.sample_count += 1
    log.info('[增长口径] 第%d期 %s 母池=%d' % (g.sample_count, today.date(), len(snapshot['frame'])))


def period_statistics(snapshot, frame):
    return growth_period_statistics(snapshot, frame)


def on_strategy_end(context):
    today = pd.Timestamp(context.current_dt).normalize()
    for snapshot in list(g.pending):
        resolve_snapshot(snapshot, today)
    g.pending.clear()
    if g.periods:
        summary = summarize_periods(pd.DataFrame(g.periods).reindex(columns=PERIOD_COLUMNS), ROTATE_EVERY)
        write_file(OUTPUT_PREFIX + '_summary.csv', summary.to_csv(index=False), append=False)
    g.settings.update(sample_count=g.sample_count, source_errors=g.source_errors,
                      elapsed_seconds=_time.time() - g.wall_started)
    _write_settings()
    log.info('[增长口径结束] 下载settings、quarters、samples、periods、summary五份CSV；空账户不是结果。')
