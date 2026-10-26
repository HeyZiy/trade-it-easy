# -*- coding: utf-8 -*-
"""聚宽日频候选探针：粘贴整个文件，2016-01-04至2026-09-01，资金100万。

PROBE='earnings_response' 检验财报后相对行业表现是否持续，以及后续兑现速度。
PROBE='turnover' 保留原换手率探针。两模式独立输出，不混用历史结果。
财报模式固定设计见README第八节。以下说明对应原turnover模式：

不下单，空账户曲线不是实验结果。每20交易日盘前按v5质量规则采样，信息截至T-1。
因子固定为最近20交易日平台turnover_ratio(%)的算术均值，不扫描窗口或阈值。
低/中/高三组分别在全池、12-2动量最高三分之一、控制变量残差中形成。
控制变量：行业、log总市值、12-2动量、vol60；残差不是纯换手率原值。
组别只用当时数据冻结。同分不按代码拆组，少于每组5只或边界重复则缺测。
T日开盘至T+20/40/60交易日开盘的后复权报价收益；不是实际成交或扣费收益。
停牌/缺报价、数据异常、末尾期限不足均保留状态，不能当作零收益或删除后重组。
输出4份：quality_momentum_v5_turnover_probe_{settings,samples,periods,summary}.csv。
samples保留20个逐日换手率、财务截止日、固定组别、端点报价及缺测原因。
periods同时报告完整组收益与已知样本均值。主对照为低组减高组，按期配对。
summary按全期、2020年起和逐年汇总；HAC不压缩中间缺测的时间间隔。
复用v5构池及单因子工具的分组/HAC函数；本地测试检查源代码一致性。
"""
from jqdata import *
from jqfactor import get_factor_values
import builtins as _b
import builtins as _python_builtins
import math as _math
import time as _time
import numpy as np
import pandas as pd

PROBE = 'turnover'  # 本轮财报反应实验改为 'earnings_response'；其余参数不改。
ROTATE_EVERY = 20
TURNOVER_DAYS = 20
HORIZONS = (20, 40, 60)
GROUPS = 3
MIN_GROUP_SIZE = 5
MOMENTUM_DAYS = 252
SKIP_RECENT_DAYS = 21
ROE_MIN = 12.0
ROE_FACTOR_UNIT = 'ratio'
NP_YOY_MIN = 10.0
PE_MAX = 30.0
PB_MAX = 5.0
VOL_LOOKBACK = 61
VOL60_MAX = 35.0
ROE_TTM_MIN = ROE_MIN
SCORE_CLOSES = MOMENTUM_DAYS + 1
SCORE_SKIP_RECENT = SKIP_RECENT_DAYS
VOL_ANNUAL_DAYS = 250.0
BENCHMARK = '000906.XSHG'
OUTPUT_PREFIX = 'quality_momentum_v5_turnover_probe'
SCOPES = ('pool_raw', 'strong_raw', 'pool_neutral')
GROUP_LABELS = ('low', 'mid', 'high', 'low_minus_high')
NAN = _python_builtins.float('nan')
SOURCE_V5_SHA256 = 'f522e494648e1437d35f535e76f31c1ff2508b4deeb43231fa0a2086473b185f'


def initialize(context):
    global OUTPUT_PREFIX
    if PROBE not in ('turnover', 'earnings_response'):
        raise _python_builtins.ValueError('Unknown probe mode')
    OUTPUT_PREFIX = 'quality_momentum_v5_' + PROBE + '_probe'
    if PROBE == 'earnings_response':
        initialize_response(context)
        return
    params = _python_builtins.getattr(context, 'run_params', None)
    if params is not None and _python_builtins.getattr(params, 'frequency', 'day') not in ('day', 'daily'):
        raise _python_builtins.ValueError('请选择每天频率；这是不下单的信号探针')
    api = ('get_valuation' if _python_builtins.callable(_python_builtins.globals().get('get_valuation'))
           else 'get_fundamentals_continuously')
    if not _python_builtins.callable(_python_builtins.globals().get(api)):
        raise _python_builtins.ValueError('平台缺少历史换手率查询接口')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    log.set_level('order', 'error')
    g.day, g.last_signal_day, g.last_close_day = 0, None, None
    g.pending, g.periods, g.started_files = [], [], _python_builtins.set()
    g.sample_count, g.source_errors, g.industry_errors, g.neutral_errors, g.wall_started = 0, 0, 0, 0, _time.time()
    g.settings = _python_builtins.dict(
        source_v5_sha256=SOURCE_V5_SHA256, frequency='day', no_orders=True,
        probe_revision='2026-10-24-pandas-compat-2',
        pandas_version=pd.__version__, numpy_version=np.__version__,
        start_date=_python_builtins.str(_python_builtins.getattr(params, 'start_date', '')),
        end_date=_python_builtins.str(_python_builtins.getattr(params, 'end_date', '')),
        signal_time='before_open', signal_cutoff='T-1', sample_every=ROTATE_EVERY,
        turnover_field='valuation.turnover_ratio', turnover_unit='percent', turnover_api=api,
        turnover_window=TURNOVER_DAYS, turnover_missing='require all 20 market sessions',
        grouping='terciles, ties kept, minimum 5 members per group',
        strong_definition='highest momentum tercile of quality pool, ties kept',
        neutral_controls='log1p(turnover20) ~ industry + log(market_cap) + momentum12_2 + vol60',
        horizons='20;40;60', returns='post-adjusted open(T+h)/open(T)-1',
        strict_returns='any missing member makes whole-group return unavailable',
        severe_loss='endpoint return <= -15%, not path drawdown',
        hac_lags='horizon/20; no HAC when internal missing periods',
        annualization='none; overlapping forward windows are not an account curve',
        roe_min_pct=ROE_MIN, roe_factor_unit=ROE_FACTOR_UNIT,
        profit_yoy_min_pct=NP_YOY_MIN, pe_max=PE_MAX, pb_max=PB_MAX,
        vol60_max_pct=VOL60_MAX, vol_annual_days=VOL_ANNUAL_DAYS,
        momentum_days=MOMENTUM_DAYS, skip_recent_days=SKIP_RECENT_DAYS)
    _write_settings()
    run_daily(on_probe_signal, time='before_open')
    log.info('[换手探针] 日频、不下单；看分组CSV，不看空账户收益。')


def turnover_snapshot(codes, asof):
    """固定20个市场交易日；停牌造成记录不足时不往更早的日期凑数。"""
    days = pd.DatetimeIndex(get_trade_days(end_date=asof.date(), count=TURNOVER_DAYS)).normalize()
    if _python_builtins.len(days) != TURNOVER_DAYS or days[-1] != asof:
        raise PoolDataError('Turnover calendar does not end at T-1')
    blocks = []
    for start in _python_builtins.range(0, _python_builtins.len(codes), 100):
        chunk = codes[start:start + 100]
        if _python_builtins.callable(_python_builtins.globals().get('get_valuation')):
            frame = get_valuation(chunk, start_date=days[0].date(), end_date=asof.date(),
                                  fields=['turnover_ratio', 'market_cap'])
        else:
            frame = get_fundamentals_continuously(
                query(valuation.code, valuation.turnover_ratio, valuation.market_cap)
                .filter(valuation.code.in_(chunk)), end_date=asof.date(),
                count=TURNOVER_DAYS, panel=False)
        _require_columns(frame, ('code', 'day', 'turnover_ratio', 'market_cap'), 'turnover')
        blocks.append(frame)
    data = pd.concat(blocks, ignore_index=True) if blocks else pd.DataFrame()
    records = {}
    if not data.empty:
        data['day'] = pd.to_datetime(data.day.astype(_python_builtins.str), errors='coerce').dt.normalize()
        if data.day.isna().any() or not data.day.isin(days).all() or not data.code.isin(codes).all():
            raise PoolDataError('Turnover snapshot date/code outside requested window')
        if data.duplicated(['code', 'day']).any():
            raise PoolDataError('Duplicate turnover observations')
    for code in codes:
        own = data[data.code.eq(code)].set_index('day') if not data.empty else pd.DataFrame()
        rates = (clean(own.turnover_ratio).reindex(days) if 'turnover_ratio' in own
                 else pd.Series(NAN, index=days))
        valid = rates.notna() & rates.ge(0)
        item = _python_builtins.dict(turnover20=_python_builtins.float(rates.mean()) if valid.all() else NAN,
                    turnover_observed=_python_builtins.int(valid.sum()),
                    turnover_status='ok' if valid.all() else 'missing_or_invalid',
                    turnover_start=_python_builtins.str(days[0].date()), turnover_end=_python_builtins.str(asof.date()),
                    market_cap=NAN)
        if 'market_cap' in own and asof in own.index:
            cap = pd.to_numeric(own.at[asof, 'market_cap'], errors='coerce')
            item['market_cap'] = _python_builtins.float(cap) if np.isfinite(cap) and cap > 0 else NAN
        item.update({'turnover_d%02d' % (i + 1): _python_builtins.float(rates.iloc[i]) for i in _python_builtins.range(TURNOVER_DAYS)})
        records[code] = item
    return records


def fixed_groups(values):
    groups = assign_groups(values, GROUPS)
    counts = groups.value_counts()
    if _python_builtins.len(counts) != GROUPS or (counts < MIN_GROUP_SIZE).any():
        groups[:] = NAN
    return groups


def neutral_turnover(frame):
    """控制当时的动量、行业、市值、波动；不通过未来收益选择方向。"""
    output = pd.Series(NAN, index=frame.index, dtype=_python_builtins.float)
    columns = ['turnover20', 'market_cap', 'momentum', 'vol60', 'industry_code']
    data = frame[columns].replace([np.inf, -np.inf], np.nan).dropna()
    data = data[data.turnover20.ge(0) & data.market_cap.gt(0) & data.industry_code.ne('')]
    if _python_builtins.len(data) < 20:
        return output
    numeric = pd.DataFrame(_python_builtins.dict(log_cap=np.log(data.market_cap),
                                momentum=data.momentum, vol60=data.vol60))
    for key in numeric:
        sigma = numeric[key].std(ddof=0)
        numeric[key] = ((numeric[key] - numeric[key].mean()) / sigma
                        if sigma > 0 else numeric[key] * 0)
    dummies = pd.get_dummies(data.industry_code, drop_first=True).astype(_python_builtins.float)
    # 聚宽旧版pandas没有to_numpy；values在旧版和当前版均可用。
    x = np.column_stack([np.ones(_python_builtins.len(data)), numeric.values, dummies.values])
    if _python_builtins.len(data) <= np.linalg.matrix_rank(x) + 5:
        return output
    y = np.log1p(data.turnover20).values
    residual = y - x @ np.linalg.lstsq(x, y, rcond=None)[0]
    if np.std(residual) <= 1e-12 * _python_builtins.max(1.0, np.std(y)):
        return output
    output.loc[data.index] = residual
    return output


def freeze_groups(frame):
    frame = frame.copy()
    frame['group_pool_raw'] = fixed_groups(frame.turnover20)
    momentum_group = assign_groups(frame.momentum, GROUPS)
    frame['momentum_strong'] = momentum_group.eq(GROUPS)
    frame['group_strong_raw'] = fixed_groups(frame.turnover20.where(frame.momentum_strong))
    frame['neutral_error'] = ''
    try:
        frame['turnover_residual'] = neutral_turnover(frame)
        frame['group_pool_neutral'] = fixed_groups(frame.turnover_residual)
    except _python_builtins.Exception as exc:
        # 辅助控制回归异常不应销毁已经冻结的原始换手分组。
        frame['turnover_residual'], frame['group_pool_neutral'] = NAN, NAN
        frame['neutral_error'] = _python_builtins.str(exc)
    return frame


def on_probe_signal(context):
    if PROBE == 'earnings_response':
        on_response_signal(context)
        return
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_signal_day == today:
        return
    g.last_signal_day = today
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY:
        return
    asof = _prev_trade_day(context.current_dt)
    snapshot = _python_builtins.dict(signal_day=today, asof=asof, sample_day=g.day,
                    error='', frame=pd.DataFrame())
    try:
        audit = {}
        pool, closes = build_pool(context, asof, audit=audit)
        ranked, scores = rank_pool(pool, closes)
        if pool:
            codes = _python_builtins.sorted(pool)
            turnover = turnover_snapshot(codes, asof)
            try:
                industry = get_industry(codes, date=asof.date()) or {}
            except _python_builtins.Exception as exc:
                industry = {}
                g.industry_errors += 1
                log.info('[换手探针] 行业缺失，仅影响中性化：%s' % exc)
            rows = []
            ranks = {c: i + 1 for i, c in _python_builtins.enumerate(ranked)}
            for code in codes:
                row = _python_builtins.dict(audit[code])
                row.update(turnover[code])
                row.update(code=code, momentum=scores.get(code, NAN), momentum_rank=ranks.get(code, NAN),
                           momentum_top6=code in ranked[:6],
                           industry_code=((industry.get(code) or {}).get('sw_l1') or {}).get('industry_code', ''))
                rows.append(row)
            snapshot['frame'] = freeze_groups(pd.DataFrame(rows).set_index('code', drop=False))
            if snapshot['frame'].neutral_error.ne('').any():
                g.neutral_errors += 1
                log.info('[换手探针] 本期仅中性化失败，原始分组保留：%s' %
                         snapshot['frame'].neutral_error.iloc[0])
    except _python_builtins.Exception as exc:
        snapshot['error'] = _python_builtins.str(exc)
        g.source_errors += 1
        log.info('[换手探针] 本期数据异常，保留缺测，不移动采样时钟：%s' % exc)
    g.pending.append(snapshot)
    g.sample_count += 1
    log.info('[换手探针] 第%d期 %s 池=%d 用时%.1fs' % (
        g.sample_count, today.date(), _python_builtins.len(snapshot['frame']), _time.time() - g.wall_started))


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
            log.info('[换手探针] 前瞻报价缺失，保留原因：%s' % exc)
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


def period_statistics(snapshot, frame):
    rows = []
    for scope in SCOPES:
        for h in HORIZONS:
            groups = {}
            for group, label in _python_builtins.enumerate(GROUP_LABELS[:3], 1):
                own = (frame[frame['group_' + scope].eq(group)]
                       if 'group_' + scope in frame else pd.DataFrame())
                returns = clean(own['r%d' % h]) if _python_builtins.len(own) else pd.Series(dtype=_python_builtins.float)
                known = returns.dropna()
                row = _python_builtins.dict(signal_day=_python_builtins.str(snapshot['signal_day'].date()),
                           asof=_python_builtins.str(snapshot['asof'].date()), scope=scope, horizon=h, group=label,
                           window_status=snapshot['status_%d' % h],
                           n=_python_builtins.len(own), observed=_python_builtins.len(known),
                           coverage=_python_builtins.len(known) / _python_builtins.len(own) if _python_builtins.len(own) else NAN,
                           return_mean=_python_builtins.float(returns.mean()) if _python_builtins.len(own) and returns.notna().all() else NAN,
                           return_available=_python_builtins.float(known.mean()) if _python_builtins.len(known) else NAN,
                           median_available=_python_builtins.float(known.median()) if _python_builtins.len(known) else NAN,
                           win_available=_python_builtins.float(known.gt(0).mean()) if _python_builtins.len(known) else NAN,
                           loss15_available=_python_builtins.float(known.le(-.15).mean()) if _python_builtins.len(known) else NAN,
                           turnover_mean=_python_builtins.float(own.turnover20.mean()) if _python_builtins.len(own) else NAN,
                           momentum_mean=_python_builtins.float(own.momentum.mean()) if _python_builtins.len(own) else NAN,
                           snapshot_error=snapshot['error'])
                groups[label] = row
                rows.append(row)
            low, high = groups['low'], groups['high']
            rows.append(_python_builtins.dict(signal_day=low['signal_day'], asof=low['asof'], scope=scope,
                             horizon=h, group='low_minus_high', window_status=low['window_status'],
                             n=low['n'] + high['n'], observed=low['observed'] + high['observed'],
                             coverage=(_python_builtins.min(low['coverage'], high['coverage'])
                                       if np.isfinite(low['coverage']) and np.isfinite(high['coverage']) else NAN),
                             return_mean=low['return_mean'] - high['return_mean'],
                             return_available=low['return_available'] - high['return_available'],
                             snapshot_error=snapshot['error']))
    return rows


def summarize_periods(periods):
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
            stats = summarize_ic(mature.return_mean, _python_builtins.int(h // ROTATE_EVERY))
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


def after_trading_end(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_close_day == today:
        return
    g.last_close_day = today
    ready = [s for s in g.pending if g.day - s['sample_day'] >= _python_builtins.max(HORIZONS)]
    for snapshot in ready:
        resolve_snapshot(snapshot, today)
        g.pending.remove(snapshot)


def on_strategy_end(context):
    if PROBE == 'earnings_response':
        end_response(context)
        return
    today = pd.Timestamp(context.current_dt).normalize()
    for snapshot in _python_builtins.list(g.pending):
        resolve_snapshot(snapshot, today)
    g.pending.clear()
    if g.periods:
        summary = summarize_periods(pd.DataFrame(g.periods).reindex(columns=PERIOD_COLUMNS))
        write_file(OUTPUT_PREFIX + '_summary.csv', summary.to_csv(index=False), append=False)
        main = summary[(summary.window == 'full') & (summary.group == 'low_minus_high')]
        for row in main.itertuples():
            log.info('[换手探针汇总] %s %d日 低-高=%+.3fpp 完整配对=%d NWt=%s' % (
                row.scope, row.horizon, row.mean_return * 100, row.valid_periods, row.t_hac))
    g.settings.update(sample_count=g.sample_count, source_errors=g.source_errors, industry_errors=g.industry_errors,
                      neutral_errors=g.neutral_errors,
                      elapsed_seconds=_time.time() - g.wall_started)
    _write_settings()
    if g.source_errors or g.neutral_errors:
        log.info('[换手探针异常汇总] 数据异常%d次，中性化异常%d期；先核对有效期数与明细，缺测不能解释为因子无效。' %
                 (g.source_errors, g.neutral_errors))
    log.info('[换手探针结束] 下载summary、periods、samples、settings；尚未验证账户增量。')


def _append_csv(kind, frame):
    write_file(OUTPUT_PREFIX + '_' + kind + '.csv',
               frame.to_csv(index=False, header=kind not in g.started_files),
               append=kind in g.started_files)
    g.started_files.add(kind)


def _write_settings():
    frame = pd.DataFrame([_python_builtins.dict(setting=k, value=v) for k, v in g.settings.items()])
    write_file(OUTPUT_PREFIX + '_settings.csv', frame.to_csv(index=False), append=False)


PERIOD_COLUMNS = ['signal_day', 'asof', 'scope', 'horizon', 'group', 'window_status',
                  'n', 'observed', 'coverage', 'return_mean', 'return_available',
                  'median_available', 'win_available', 'loss15_available',
                  'turnover_mean', 'momentum_mean', 'snapshot_error']
SAMPLE_COLUMNS = ['signal_day', 'asof', 'code', 'turnover20', 'turnover_observed',
                  'turnover_status', 'turnover_start', 'turnover_end', 'market_cap',
                  'momentum', 'momentum_rank', 'momentum_top6', 'momentum_strong',
                  'industry_code', 'vol60', 'roe_ttm_pct', 'np_yoy_pct', 'pe_ttm', 'pb',
                  'stat_date', 'pub_date', 'group_pool_raw', 'group_strong_raw',
                  'turnover_residual', 'group_pool_neutral', 'entry_open', 'entry_paused']
SAMPLE_COLUMNS += ['turnover_d%02d' % (i + 1) for i in _python_builtins.range(TURNOVER_DAYS)]
for _h in HORIZONS:
    SAMPLE_COLUMNS += ['r%d' % _h, 'status_r%d' % _h, 'exit_date_%d' % _h,
                       'exit_open_%d' % _h, 'exit_paused_%d' % _h]
SAMPLE_COLUMNS += ['snapshot_error', 'quote_error', 'neutral_error']


# 财报反应模式与原换手率模式共用构池、分组、时钟、前瞻报价和HAC工具。
RESPONSE_MIN_AGE = 20
RESPONSE_MAX_AGE = 60
RESPONSE_RECENT = 20
RESPONSE_TARGET = .05
RESPONSE_MIN_PEERS = 5
RESPONSE_SCOPES = ('pool_response', 'pool_momentum', 'strong_response')
RESPONSE_METRICS = ('return', 'hit5', 'wait5', 'mdd', 'mae')
RESPONSE_FIELDS = ['response_score', 'response_status', 'response_state', 'response_anchor',
                   'response_age', 'stock_anchor_close', 'stock_asof_close', 'stock_recent_close',
                   'industry_gross_post', 'industry_gross_recent', 'excess_log_post',
                   'excess_log_recent', 'peer_count', 'peer_members', 'reference_start',
                   'reference_member_count', 'reference_valid_count',
                   'group_pool_response', 'group_pool_momentum', 'group_strong_response',
                   'forward_dates', 'forward_opens', 'forward_paused', 'forward_statuses']


def initialize_response(context):
    params = _python_builtins.getattr(context, 'run_params', None)
    if params is not None and _python_builtins.getattr(params, 'frequency', 'day') not in ('day', 'daily'):
        raise _python_builtins.ValueError('请选择每天频率')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    log.set_level('order', 'error')
    g.day, g.last_signal_day, g.last_close_day = 0, None, None
    g.pending, g.periods, g.started_files = [], [], _python_builtins.set()
    g.sample_count, g.source_errors, g.industry_errors = 0, 0, 0
    g.wall_started = _time.time()
    g.settings = _python_builtins.dict(
        probe=PROBE, probe_revision='2026-10-26-response-1', source_v5_sha256=SOURCE_V5_SHA256,
        frequency='day', no_orders=True, pandas_version=pd.__version__, numpy_version=np.__version__,
        start_date=_python_builtins.str(_python_builtins.getattr(params, 'start_date', '')),
        end_date=_python_builtins.str(_python_builtins.getattr(params, 'end_date', '')),
        signal_cutoff='T-1 before_open; every 20 market sessions; no retry off grid',
        event='latest visible indicator.pubDate, not earnings surprise or announcement timestamp',
        anchor='close of first market session strictly after pubDate; excludes initial reaction',
        event_age_min=RESPONSE_MIN_AGE, event_age_max=RESPONSE_MAX_AGE,
        recent_days=RESPONSE_RECENT, target_return=RESPONSE_TARGET,
        score='20*min(log(stock_gross/industry_gross)/event_age, recent20_excess_log/20)',
        industry='sw_l1 members at T-1; fixed equal-weight buy-and-hold reference, excludes self',
        reference_eligibility='complete positive close and unpaused on common historical window',
        minimum_other_peers=RESPONSE_MIN_PEERS,
        grouping='terciles, ties kept, min 5/group; pool response vs momentum on SAME eligible set',
        strong_definition='highest momentum tercile of original quality pool, then response eligible',
        primary='20-day hit +5% and restricted wait; high response vs high momentum, same eligible pool',
        wait_definition='min(first +5% open observation, horizon); unhit=horizon; read with hit rate',
        path='post-adjusted daily opens T..T+h; no intraday hit/drawdown or actual fill claim',
        horizons='20;40;60', costs='not modeled; signals only, not full-account returns',
        missing='keep frozen rows; unknown path is missing, never counted as non-hit',
        paired='strict complete groups in same sample period, equal period weight',
        hac='horizon/20; internal missing periods not compressed',
        roe_min_pct=ROE_MIN, roe_factor_unit=ROE_FACTOR_UNIT, profit_yoy_min_pct=NP_YOY_MIN,
        pe_max=PE_MAX, pb_max=PB_MAX, vol60_max_pct=VOL60_MAX,
        momentum_days=MOMENTUM_DAYS, skip_recent_days=SKIP_RECENT_DAYS)
    _write_settings()
    run_daily(on_probe_signal, time='before_open')
    log.info('[财报反应探针] 日频、不下单；先看行业参考覆盖率和完整分组期数。')


def response_closes(codes, days):
    blocks = []
    for offset in _python_builtins.range(0, _python_builtins.len(codes), 100):
        chunk = codes[offset:offset + 100]
        frame = get_price(chunk, start_date=days[0].date(), end_date=days[-1].date(),
                          fields=['close', 'paused'], frequency='daily', fq='post',
                          skip_paused=False, panel=False)
        _require_columns(frame, ('time', 'code', 'close', 'paused'), 'response history')
        blocks.append(frame)
    data = pd.concat(blocks, ignore_index=True)
    data['time'] = pd.to_datetime(data.time).dt.normalize()
    if (not data.time.isin(days).all() or not data.code.isin(codes).all()
            or data.duplicated(['time', 'code']).any()):
        raise PoolDataError('Response history outside requested calendar/code or duplicated')
    matrices = []
    for key in ('close', 'paused'):
        data[key] = pd.to_numeric(data[key], errors='coerce')
        matrices.append(data.pivot(index='time', columns='code', values=key).reindex(index=days, columns=codes))
    return matrices


def response_features(frame, asof):
    """只消费T-1已知财报、行业成员和价格；未来收益完全不参与分组。"""
    frame = frame.copy()
    for key in RESPONSE_FIELDS:
        frame[key] = '' if key in ('response_status', 'response_state', 'response_anchor',
                                  'peer_members', 'reference_start', 'forward_dates',
                                  'forward_opens', 'forward_paused', 'forward_statuses') else NAN
    frame['response_status'] = 'report_age_unavailable'
    days = pd.DatetimeIndex(get_trade_days(end_date=asof.date(), count=RESPONSE_MAX_AGE + 2)).normalize()
    if _python_builtins.len(days) != RESPONSE_MAX_AGE + 2 or days[-1] != asof:
        raise PoolDataError('Incomplete response calendar ending at T-1')
    anchors = {}
    for code, row in frame.iterrows():
        pub = pd.to_datetime(row.pub_date, errors='coerce')
        stat = _report_date(row.stat_date)
        if pd.isna(pub) or pd.isna(stat) or pub > asof or stat > asof:
            frame.at[code, 'response_status'] = 'report_not_visible'
            continue
        pub = pub.normalize()
        if pub < days[0]:
            frame.at[code, 'response_status'] = 'report_too_old'
            continue
        after = days[days > pub]
        age = _python_builtins.len(after) - 1
        frame.at[code, 'response_age'] = age
        if age < RESPONSE_MIN_AGE:
            frame.at[code, 'response_status'] = 'report_too_recent'
            continue
        if age > RESPONSE_MAX_AGE:
            frame.at[code, 'response_status'] = 'report_too_old'
            continue
        frame.at[code, 'response_anchor'] = _python_builtins.str(after[0].date())
        if not row.industry_code:
            frame.at[code, 'response_status'] = 'industry_unknown'
            continue
        anchors[code] = after[0]
        frame.at[code, 'response_status'] = 'reference_unavailable'
    memberships = {}
    for industry in _python_builtins.sorted(_python_builtins.set(frame.loc[_python_builtins.list(anchors), 'industry_code'])):
        try:
            members = get_industry_stocks(industry, date=asof.date())
            memberships[industry] = _python_builtins.sorted(_python_builtins.set(members or []))
        except _python_builtins.Exception as exc:
            g.industry_errors += 1
            log.info('[财报反应探针] 行业参考缺失 %s: %s' % (industry, exc))
    all_codes = _python_builtins.set(anchors)
    for members in memberships.values():
        all_codes.update(members)
    if not all_codes or not anchors:
        return freeze_response(frame)
    try:
        closes, paused = response_closes(_python_builtins.sorted(all_codes), days)
    except _python_builtins.Exception as exc:
        g.source_errors += 1
        frame.loc[_python_builtins.list(anchors), 'response_status'] = 'history_error: ' + _python_builtins.str(exc)
        return freeze_response(frame)
    for industry, members in memberships.items():
        own_codes = [c for c in anchors if frame.at[c, 'industry_code'] == industry]
        start = _python_builtins.min([anchors[c] for c in own_codes] + [days[-RESPONSE_RECENT - 1]])
        historical = closes.loc[start:, members]
        valid = (historical.notna() & np.isfinite(historical) & historical.gt(0)
                 & paused.loc[start:, members].eq(0)).all()
        eligible_peers = _python_builtins.list(valid.index[valid])
        for code in own_codes:
            anchor = anchors[code]
            stock_start = _python_builtins.min(anchor, days[-RESPONSE_RECENT - 1])
            own = closes.loc[stock_start:, code]
            frame.at[code, 'reference_start'] = _python_builtins.str(start.date())
            frame.at[code, 'reference_member_count'] = _python_builtins.len(members)
            frame.at[code, 'reference_valid_count'] = _python_builtins.len(eligible_peers)
            if not (own.notna() & np.isfinite(own) & own.gt(0) & paused.loc[stock_start:, code].eq(0)).all():
                frame.at[code, 'response_status'] = 'stock_history_missing_or_paused'
                continue
            peers = [p for p in eligible_peers if p != code]
            frame.at[code, 'peer_count'] = _python_builtins.len(peers)
            frame.at[code, 'peer_members'] = ';'.join(peers)
            if _python_builtins.len(peers) < RESPONSE_MIN_PEERS:
                frame.at[code, 'response_status'] = 'too_few_peers'
                continue
            industry_post = (closes.loc[asof, peers] / closes.loc[anchor, peers]).mean()
            industry_recent = (closes.loc[asof, peers] / closes.loc[days[-RESPONSE_RECENT - 1], peers]).mean()
            stock_end = closes.at[asof, code]
            stock_anchor = closes.at[anchor, code]
            stock_recent = closes.at[days[-RESPONSE_RECENT - 1], code]
            post = _math.log(stock_end / stock_anchor / industry_post)
            recent = _math.log(stock_end / stock_recent / industry_recent)
            age = frame.at[code, 'response_age']
            score = RESPONSE_RECENT * _python_builtins.min(post / age, recent / RESPONSE_RECENT)
            state = ('continuing' if post > 0 and recent > 0 else 'faded' if post > 0 else
                     'recent_only' if recent > 0 else 'weak')
            frame.loc[code, ['response_score', 'response_status', 'response_state',
                             'stock_anchor_close', 'stock_asof_close', 'stock_recent_close',
                             'industry_gross_post', 'industry_gross_recent',
                             'excess_log_post', 'excess_log_recent']] = [
                score, 'ok', state, stock_anchor, stock_end, stock_recent,
                industry_post, industry_recent, post, recent]
    return freeze_response(frame)


def freeze_response(frame):
    # 两种排序共用完全相同的可比较候选；原池中不可比较的股票仍保留。
    eligible = clean(frame.response_score).notna() & clean(frame.momentum).notna()
    frame['group_pool_response'] = fixed_groups(frame.response_score.where(eligible))
    frame['group_pool_momentum'] = fixed_groups(frame.momentum.where(eligible))
    frame['momentum_strong'] = assign_groups(frame.momentum, GROUPS).eq(GROUPS)
    frame['group_strong_response'] = fixed_groups(frame.response_score.where(eligible & frame.momentum_strong))
    return frame


def on_response_signal(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_signal_day == today:
        return
    g.last_signal_day = today
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY:
        return
    asof = _prev_trade_day(context.current_dt)
    snapshot = _python_builtins.dict(signal_day=today, asof=asof, sample_day=g.day,
                                    error='', frame=pd.DataFrame())
    try:
        audit = {}
        pool, closes = build_pool(context, asof, audit=audit)
        ranked, scores = rank_pool(pool, closes)
        if pool:
            codes = _python_builtins.sorted(pool)
            try:
                industry = get_industry(codes, date=asof.date()) or {}
            except _python_builtins.Exception:
                industry = {}
                g.industry_errors += 1
            ranks = {c: i + 1 for i, c in _python_builtins.enumerate(ranked)}
            rows = []
            for code in codes:
                row = _python_builtins.dict(audit[code])
                row.update(code=code, momentum=scores.get(code, NAN), momentum_rank=ranks.get(code, NAN),
                           momentum_top6=code in ranked[:6],
                           industry_code=((industry.get(code) or {}).get('sw_l1') or {}).get('industry_code', ''))
                rows.append(row)
            snapshot['frame'] = response_features(pd.DataFrame(rows).set_index('code', drop=False), asof)
    except _python_builtins.Exception as exc:
        snapshot['error'] = _python_builtins.str(exc)
        g.source_errors += 1
        log.info('[财报反应探针] 保留本期异常、不移动采样时钟: %s' % exc)
    g.pending.append(snapshot)
    g.sample_count += 1
    log.info('[财报反应探针] 第%d期 %s 池=%d 用时%.1fs' % (
        g.sample_count, today.date(), _python_builtins.len(snapshot['frame']), _time.time() - g.wall_started))


def response_path_metrics(prices, statuses, horizon):
    """固定期限完整日开盘路径；未达标与未知严格区分。"""
    result = {key: NAN for key in ('hit5', 'wait5', 'mdd', 'mae')}
    if _python_builtins.len(prices) <= horizon:
        return result, 'censored'
    if _python_builtins.any(s != 'ok' for s in statuses[:horizon + 1]):
        return result, 'path_unknown'
    values = np.asarray(prices[:horizon + 1], dtype=_python_builtins.float)
    if not np.isfinite(values).all() or (values <= 0).any():
        return result, 'path_unknown'
    gross = values / values[0]
    hits = np.flatnonzero(gross[1:] >= 1 + RESPONSE_TARGET)
    wait = _python_builtins.int(hits[0] + 1) if _python_builtins.len(hits) else horizon
    result.update(hit5=_python_builtins.float(_python_builtins.len(hits) > 0), wait5=wait,
                  mdd=_python_builtins.float((gross / np.maximum.accumulate(gross) - 1).min()),
                  mae=_python_builtins.float((gross - 1).min()))
    return result, 'ok'


def resolve_response(snapshot, today):
    frame = snapshot['frame'].copy()
    days = pd.DatetimeIndex(get_trade_days(start_date=snapshot['signal_day'].date(),
                                          end_date=today.date())).normalize()[:_python_builtins.max(HORIZONS) + 1]
    quotes, quote_error = pd.DataFrame(), ''
    if not frame.empty:
        try:
            quotes = forward_quotes(_python_builtins.list(frame.code), days[0], days[-1])
        except _python_builtins.Exception as exc:
            quote_error = _python_builtins.str(exc)
            g.source_errors += 1
    for h in HORIZONS:
        mature = _python_builtins.len(days) > h
        snapshot['status_%d' % h] = ('censored' if not mature else 'snapshot_error' if snapshot['error'] else
                                    'quote_error' if quote_error else 'mature')
        for key in ('r', 'hit5_', 'wait5_', 'mdd_', 'mae_', 'exit_open_'):
            frame[key + _python_builtins.str(h)] = NAN
        frame['status_r%d' % h] = 'censored' if not mature else 'quote_error'
        frame['path_status_%d' % h] = 'censored' if not mature else 'quote_error'
        frame['exit_date_%d' % h] = _python_builtins.str(days[h].date()) if mature else ''
    frame['entry_open'] = NAN
    if not frame.empty and not quote_error:
        for code in frame.index:
            observations = [quote_at(quotes, day, code) for day in days]
            prices, paused, states = _python_builtins.zip(*observations)
            frame.at[code, 'entry_open'] = prices[0]
            frame.at[code, 'forward_dates'] = ';'.join(_python_builtins.str(d.date()) for d in days)
            for field, values in [('forward_opens', prices), ('forward_paused', paused), ('forward_statuses', states)]:
                frame.at[code, field] = ';'.join(_python_builtins.str(v) for v in values)
            for h in HORIZONS:
                if _python_builtins.len(days) <= h:
                    continue
                frame.at[code, 'exit_open_%d' % h] = prices[h]
                state = ('entry_' + states[0] if states[0] != 'ok' else
                         'exit_' + states[h] if states[h] != 'ok' else 'ok')
                frame.at[code, 'status_r%d' % h] = state
                if state == 'ok':
                    frame.at[code, 'r%d' % h] = prices[h] / prices[0] - 1
                metrics, status = response_path_metrics(prices, states, h)
                frame.at[code, 'path_status_%d' % h] = status
                for key, value in metrics.items():
                    frame.at[code, key + '_%d' % h] = value
    if frame.empty:
        frame = pd.DataFrame([_python_builtins.dict(code='')])
    frame['signal_day'], frame['asof'] = _python_builtins.str(snapshot['signal_day'].date()), _python_builtins.str(snapshot['asof'].date())
    frame['snapshot_error'], frame['quote_error'] = snapshot['error'], quote_error
    fields = ['signal_day', 'asof', 'code', 'stat_date', 'pub_date', 'industry_code',
              'roe_ttm_pct', 'np_yoy_pct', 'pe_ttm', 'pb', 'vol60', 'momentum',
              'momentum_rank', 'momentum_top6', 'momentum_strong', 'entry_open'] + RESPONSE_FIELDS
    for h in HORIZONS:
        fields += ['r%d' % h, 'status_r%d' % h, 'path_status_%d' % h,
                   'exit_date_%d' % h, 'exit_open_%d' % h]
        fields += [key + '_%d' % h for key in RESPONSE_METRICS if key != 'return']
    _append_csv('samples', frame.reindex(columns=fields + ['snapshot_error', 'quote_error']))
    rows = response_periods(snapshot, frame)
    g.periods.extend(rows)
    _append_csv('periods', pd.DataFrame(rows))


def response_periods(snapshot, frame):
    rows = []
    for h in HORIZONS:
        for metric in RESPONSE_METRICS:
            column = 'r%d' % h if metric == 'return' else metric + '_%d' % h
            comparisons = {}
            for scope in RESPONSE_SCOPES:
                for group, label in _python_builtins.enumerate(('low', 'mid', 'high'), 1):
                    own = frame[frame['group_' + scope].eq(group)] if 'group_' + scope in frame else pd.DataFrame()
                    values = clean(own[column]) if not own.empty else pd.Series(dtype=_python_builtins.float)
                    known = values.dropna()
                    row = _python_builtins.dict(
                        signal_day=_python_builtins.str(snapshot['signal_day'].date()),
                        asof=_python_builtins.str(snapshot['asof'].date()), horizon=h, metric=metric,
                        scope=scope, group=label, window_status=snapshot['status_%d' % h],
                        n=_python_builtins.len(own), observed=_python_builtins.len(known),
                        coverage=_python_builtins.len(known) / _python_builtins.len(own) if not own.empty else NAN,
                        value=_python_builtins.float(values.mean()) if not own.empty and values.notna().all() else NAN,
                        available=_python_builtins.float(known.mean()) if not known.empty else NAN,
                        snapshot_error=snapshot['error'])
                    comparisons[(scope, label)] = row
                    rows.append(row)
                rows.append(response_difference(comparisons[(scope, 'high')], comparisons[(scope, 'low')],
                                                scope, 'high_minus_low'))
            rows.append(response_difference(comparisons[('pool_response', 'high')],
                                            comparisons[('pool_momentum', 'high')],
                                            'response_vs_momentum', 'high_minus_high'))
    return rows


def response_difference(left, right, scope, label):
    row = _python_builtins.dict(left)
    row.update(scope=scope, group=label, n=left['n'] + right['n'],
               observed=left['observed'] + right['observed'],
               coverage=_python_builtins.min(left['coverage'], right['coverage'])
               if np.isfinite(left['coverage']) and np.isfinite(right['coverage']) else NAN,
               value=left['value'] - right['value'], available=left['available'] - right['available'])
    return row


def response_summary(periods):
    periods = periods.copy()
    periods['signal_day'] = pd.to_datetime(periods.signal_day)
    windows = [('full', periods), ('2020_onward', periods[periods.signal_day.dt.year.ge(2020)])]
    windows += [(_python_builtins.str(year), block) for year, block in periods.groupby(periods.signal_day.dt.year)]
    rows = []
    for window, block in windows:
        for (scope, h, group, metric), subset in block.groupby(['scope', 'horizon', 'group', 'metric']):
            mature = subset.sort_values('signal_day')
            mature = mature[mature.window_status.ne('censored')]
            stats = summarize_ic(mature.value, _python_builtins.int(h // ROTATE_EVERY))
            rows.append(_python_builtins.dict(
                window=window, scope=scope, horizon=h, group=group, metric=metric,
                periods=_python_builtins.len(subset), censored=_python_builtins.int(subset.window_status.eq('censored').sum()),
                valid_periods=stats['n'], mean_value=stats['mean'], positive_period_share=stats['positive_fraction'],
                t_iid=stats['t_iid'], t_hac=stats['t_hac'], available_mean=mature.available.mean(),
                coverage_mean=mature.coverage.mean(), members_mean=mature.n.mean()))
    return pd.DataFrame(rows)


def end_response(context):
    today = pd.Timestamp(context.current_dt).normalize()
    for snapshot in _python_builtins.list(g.pending):
        resolve_snapshot(snapshot, today)
    g.pending.clear()
    if g.periods:
        summary = response_summary(pd.DataFrame(g.periods))
        write_file(OUTPUT_PREFIX + '_summary.csv', summary.to_csv(index=False), append=False)
        main = summary[(summary.window == 'full') & (summary.scope == 'response_vs_momentum')]
        for row in main.itertuples():
            log.info('[财报反应汇总] %d日 %s 新排序-旧排序=%s 完整配对=%d' % (
                row.horizon, row.metric, row.mean_value, row.valid_periods))
    g.settings.update(sample_count=g.sample_count, source_errors=g.source_errors,
                      industry_errors=g.industry_errors, elapsed_seconds=_time.time() - g.wall_started)
    _write_settings()
    log.info('[财报反应结束] 下载4份CSV；检查覆盖率，再判断速度、收益和风险；尚无账户结论。')


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
