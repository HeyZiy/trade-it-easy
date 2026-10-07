# -*- coding: utf-8 -*-
# 低预期＋盈利维持：独立聚宽回测诊断，直接粘贴整文件，无需上传旧数据。
# 聚宽「策略回测」：频率“天”，2016-01-04起；结束日选数据已覆盖的日期。
# 资金任意，不下单；研究结果看日志、record与导出CSV，空账户收益不是结果。
# 主60日、辅助20/120日；每20交易日冻结一次，只使用T-1已披露信息。
# 源：internal/中的财务统计、平台适配实现及共用工具。
import builtins as _python_builtins
import numpy as np
import pandas as pd

VERSION = 'low_expectations_v2_2026-10-06'
PROFIT_FIELD = 'np_parent_company_owners'
PE_LIMIT = 10.0
GROUP_NAMES = ('A', 'B', 'C', 'D', 'LOW')
HISTORY_COLUMNS = ('code', 'statDate', 'pubDate', 'np_parent_company_owners')
METRICS = ('A_minus_LOW_observed', 'A_minus_B_observed', 'interaction_observed',
           'A_observed_minus_benchmark', 'LOW_observed_minus_benchmark',
           'A_minus_LOW_strict', 'A_minus_B_strict', 'regression_stable_within_low',
           'regression_stable', 'regression_interaction')

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

def report_date(value):
    if pd.isna(value):
        return pd.NaT
    try:
        text = _python_builtins.str(value).strip().lower()
        if _python_builtins.len(text) == 6 and text[:4].isdigit() and text[4] == 'q' and text[5] in '1234':
            return pd.Period(text, freq='Q').end_time.normalize()
        date = pd.Timestamp(value).normalize()
        return date if date.is_quarter_end else pd.NaT
    except (_python_builtins.ValueError, _python_builtins.TypeError, _python_builtins.OverflowError):
        return pd.NaT

def centered_rank(values):
    """同分同秩，截面中心化；产品项不等价于只买低估值且改善强。"""
    ranks = clean(values).rank(method='average', pct=True)
    return ranks - ranks.mean()

def prepare_profit_features(rows, snapshot, max_report_age=240):
    """只接受最新连续八个可见单季度；返回每只原股票的资格及排除原因。"""
    if snapshot.code.duplicated().any() or snapshot['asof'].nunique() != 1:
        raise _python_builtins.ValueError('Snapshot must contain unique codes at one asof')
    if _python_builtins.set(HISTORY_COLUMNS) - _python_builtins.set(rows.columns):
        raise _python_builtins.ValueError('Missing profit history columns')
    requested = _python_builtins.set(snapshot.code)
    if _python_builtins.set(rows.code) - requested:
        raise _python_builtins.ValueError('Profit history contains unrequested codes')
    cutoff = pd.Timestamp(snapshot['asof'].iloc[0]).normalize()
    data = rows[_python_builtins.list(HISTORY_COLUMNS)].copy()
    data['statDate'] = data.statDate.apply(report_date)
    data['pubDate'] = pd.to_datetime(data.pubDate, errors='coerce').dt.normalize()
    data[PROFIT_FIELD] = clean(data[PROFIT_FIELD])
    blocks = {code: block.sort_values('statDate') for code, block in data.groupby('code')}
    records = []
    for original in snapshot.itertuples(index=False):
        block = blocks.get(original.code, pd.DataFrame(columns=HISTORY_COLUMNS))
        row = _python_builtins.dict(code=original.code, signal=original.signal, asof=original.asof,
                   profit_status='ok', quarter_n=_python_builtins.len(block), profit_report=pd.NaT,
                   profit_last_published=pd.NaT, current_ttm=np.nan, prior_ttm=np.nan,
                   profit_yoy=np.nan, latest_quarter_change=np.nan, implied_ttm=np.nan,
                   ttm_relative_mismatch=np.nan)
        reason = None
        if block.empty:
            reason = 'no_history'
        elif block.statDate.isna().any() or block.pubDate.isna().any():
            reason = 'invalid_dates'
        elif ((block.statDate > cutoff) | (block.pubDate > cutoff)
              | (block.pubDate < block.statDate)).any():
            reason = 'not_visible'
        elif block.statDate.duplicated().any():
            reason = 'ambiguous_quarter'
        else:
            row['profit_report'] = block.statDate.iloc[-1]
            row['profit_last_published'] = block.pubDate.max()
            if _python_builtins.len(block) != 8:
                reason = 'insufficient_quarters' if _python_builtins.len(block) < 8 else 'unexpected_quarter_count'
            elif (np.diff([date.to_period('Q').ordinal for date in block.statDate]) != 1).any():
                reason = 'nonconsecutive_quarters'
            elif (cutoff - block.statDate.iloc[-1]).days > max_report_age:
                reason = 'stale_report'
            elif block.statDate.iloc[-1] != pd.Timestamp(original.report).normalize():
                reason = 'report_differs_from_original'
            elif block[PROFIT_FIELD].isna().any():
                reason = 'missing_profit'
            else:
                profits = block[PROFIT_FIELD].values.astype(_python_builtins.float)
                current, prior = _python_builtins.float(profits[4:].sum()), _python_builtins.float(profits[:4].sum())
                row.update(current_ttm=current, prior_ttm=prior,
                           latest_quarter_change=_python_builtins.float(profits[-1] - profits[3]))
                if current <= 0:
                    reason = 'nonpositive_current_ttm'
                elif prior <= 0:
                    reason = 'nonpositive_prior_ttm'
                else:
                    row['profit_yoy'] = current / prior - 1
                    if original.pe_ratio <= 0 or original.market_cap <= 0:
                        reason = 'invalid_original_valuation'
                    else:
                        implied = original.market_cap * 1e8 / original.pe_ratio
                        row['implied_ttm'] = implied
                        row['ttm_relative_mismatch'] = current / implied - 1
        row['profit_status'] = reason or 'ok'
        records.append(row)
    return pd.DataFrame(records)

def freeze_profit_groups(snapshot, features):
    """所有分组只使用原信号和已披露利润，与未来报价无关。"""
    keys = ['signal', 'asof', 'code']
    joined = snapshot.merge(features, on=keys, how='left', validate='one_to_one')
    if joined.profit_status.isna().any():
        raise _python_builtins.ValueError('Missing feature coverage for original members')
    common = joined[joined.profit_status == 'ok'].copy()
    common['low_expectations'] = common.pe_ratio < PE_LIMIT
    common['profit_maintained'] = common.current_ttm >= common.prior_ttm
    common['cell'] = np.select([
        common.low_expectations & common.profit_maintained,
        common.low_expectations & ~common.profit_maintained,
        ~common.low_expectations & common.profit_maintained,
    ], ['A', 'B', 'C'], default='D')
    return common

def stable_regression(frame, future_return, min_n=100):
    """离散条件＋连续EP/行业/市值/动量；信号时定秩，缺报价后不重算。"""
    frame = frame.set_index('code')
    low = frame.low_expectations.astype(_python_builtins.float)
    stable = frame.profit_maintained.astype(_python_builtins.float)
    cap = np.log(clean(frame.market_cap))
    cap_sd = cap.std(ddof=0)
    cap = (cap - cap.mean()) / cap_sd if cap_sd > 0 else cap * 0
    x = pd.DataFrame(_python_builtins.dict(intercept=1.0, low=low, stable=stable, interaction=low * stable,
                          value=centered_rank(1 / frame.pe_ratio), log_cap=cap,
                          momentum=centered_rank(frame.momentum120)))
    industry = pd.get_dummies(frame.industry, prefix='industry', drop_first=True).astype(_python_builtins.float)
    x = pd.concat([x, industry], axis=1, sort=False)
    y = clean(future_return).reindex(x.index)
    valid = x.notna().all(axis=1) & y.notna()
    x, y = x.loc[valid], y.loc[valid]
    out = _python_builtins.dict(regression_n=_python_builtins.len(y), regression_stable=np.nan, regression_interaction=np.nan,
               regression_stable_within_low=np.nan, regression_status='ok')
    drop = [c for c in x if c not in ('intercept', 'low', 'stable', 'interaction')
            and x[c].nunique() <= 1]
    x = x.drop(columns=drop)
    if _python_builtins.len(y) < _python_builtins.max(min_n, _python_builtins.len(x.columns) + 3):
        out['regression_status'] = 'insufficient_pairs'
    elif np.linalg.matrix_rank(x.values) != _python_builtins.len(x.columns):
        out['regression_status'] = 'not_identifiable'
    else:
        beta = pd.Series(np.linalg.lstsq(x.values, y.values, rcond=None)[0], index=x.columns)
        out.update(regression_stable=_python_builtins.float(beta.stable),
                   regression_interaction=_python_builtins.float(beta.interaction),
                   regression_stable_within_low=_python_builtins.float(beta.stable + beta.interaction))
    return out

def evaluate_profit_period(frame, future_return, benchmark=np.nan, min_cell_n=20, min_n=100):
    stats = _python_builtins.dict(common_n=_python_builtins.len(frame), benchmark=benchmark)
    for name in GROUP_NAMES:
        members = frame[frame.low_expectations] if name == 'LOW' else frame[frame.cell == name]
        values = clean(future_return).reindex(members.code)
        valid_n = _python_builtins.int(values.notna().sum())
        stats[name + '_n'] = _python_builtins.len(members)
        stats[name + '_valid_n'] = valid_n
        stats[name + '_coverage'] = valid_n / _python_builtins.len(members) if _python_builtins.len(members) else np.nan
        stats[name + '_observed'] = values.mean() if valid_n >= min_cell_n else np.nan
        stats[name + '_strict'] = (values.mean() if _python_builtins.len(members) >= min_cell_n
                                   and values.notna().all() else np.nan)
    for mode in ('observed', 'strict'):
        stats['A_minus_LOW_' + mode] = stats['A_' + mode] - stats['LOW_' + mode]
        stats['A_minus_B_' + mode] = stats['A_' + mode] - stats['B_' + mode]
    stats['interaction_observed'] = stats['A_minus_B_observed'] - (
        stats['C_observed'] - stats['D_observed'])
    stats['A_observed_minus_benchmark'] = stats['A_observed'] - benchmark
    stats['LOW_observed_minus_benchmark'] = stats['LOW_observed'] - benchmark
    stats.update(stable_regression(frame, future_return, min_n))
    return stats

def summarize_profit_periods(periods, rotate_every=20):
    records = []
    for horizon, block in periods.groupby('horizon', sort=True):
        block = block.sort_values('signal')
        lags = _python_builtins.max(3, _python_builtins.int(np.ceil(horizon / rotate_every)) - 1)
        scopes = [('all', block)] + [(_python_builtins.str(year), piece) for year, piece in
                                    block.groupby(pd.to_datetime(block.signal).dt.year)]
        for scope, piece in scopes:
            for metric in METRICS:
                row = summarize_ic(piece[metric], lags)
                row.update(horizon=_python_builtins.int(horizon), scope=scope, metric=metric,
                           scheduled_n=_python_builtins.len(piece), hac_lags=lags)
                records.append(row)
    return pd.DataFrame(records)

"""独立聚宽诊断：自行取数，按到期窗口计算报价收益，不依赖旧文件。"""
import json
from jqdata import *


def chunks(codes):
    for start in _python_builtins.range(0, _python_builtins.len(codes), QUERY_BATCH):
        yield codes[start:start + QUERY_BATCH]

def export_frame(name, frame):
    """平台文件写失败直接报错；不宣布导出成功，不悄悄丢失逐期证据。"""
    if frame.empty:
        return
    path = OUTPUT_PREFIX + name + '.csv'
    first = path not in g.written
    write_file(path, frame.to_csv(index=False, header=first), append=not first)
    g.written.add(path)

def price_frame(codes, asof, fields, count=None):
    frames = []
    for batch in chunks(codes):
        kwargs = _python_builtins.dict(end_date=asof, fields=fields, frequency='daily', panel=False,
                      fq='pre', skip_paused=False, fill_paused=False)
        if count is None:
            kwargs['start_date'] = asof
        else:
            kwargs['count'] = count
        data = get_price(batch, **kwargs)
        if data is not None and not data.empty:
            frames.append(data)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=['code', 'time'] + fields)

def opening_quotes(codes):
    current = get_current_data()
    quotes = pd.Series(np.nan, index=codes, dtype=_python_builtins.float)
    for code in codes:
        try:
            quote = current[code]
        except _python_builtins.KeyError:
            continue  # 退市等导致的端点缺报价：保留成员和缺测，不终止全轮
        if quote is None:
            continue
        value = quote.day_open
        if not quote.paused and value is not None and np.isfinite(value) and value > 0:
            quotes.loc[code] = value
    return quotes

def on_open(context):
    g.day += 1
    date = pd.Timestamp(context.current_dt).normalize()
    g.first_day = date if g.first_day is None else g.first_day
    g.last_day = date
    due = [(batch, h) for batch in g.active for h in HORIZONS
           if g.day - batch['entry_day'] == h and h not in batch['completed']]
    codes = {BENCHMARK}
    for batch, _ in due:
        codes.update(batch['frame'].index)
    if g.pending is not None:
        if date <= g.pending['signal']:
            raise _python_builtins.ValueError('Entry must follow signal')
        codes.update(g.pending['frame'].index)
    if due or g.pending is not None:
        quotes = opening_quotes(_python_builtins.sorted(codes))
    for batch, horizon in due:
        finish_period(batch, horizon, date, quotes)
        batch['completed'].add(horizon)
    g.active = [batch for batch in g.active if _python_builtins.len(batch['completed']) < _python_builtins.len(HORIZONS)]
    if g.pending is not None:
        batch = g.pending
        batch['entry'], batch['entry_day'] = date, g.day
        batch['entry_valid'] = quotes.reindex(_python_builtins.list(batch['frame'].index) + [BENCHMARK]).notna()
        g.active.append(batch)
        g.pending = None

def format_number(value, scale=1):
    return 'NA' if not np.isfinite(value) else '%+.5f' % (value * scale)


ROTATE_EVERY = 20
HORIZONS = (20, 60, 120)
PRIMARY_HORIZON = 60
MIN_LISTING_DAYS = 365
MAX_REPORT_AGE = 240
MIN_IC_N = 100
MIN_CELL_N = 20
QUERY_BATCH = 400
BENCHMARK = '000906.XSHG'
OUTPUT_PREFIX = 'low_expectations_v2_standalone_'
STUDY_VERSION = 'low_expectations_v2_standalone_2026-10-06'


def initialize(context):
    if (PRIMARY_HORIZON not in HORIZONS or ROTATE_EVERY < 1 or not HORIZONS
            or _python_builtins.any(_python_builtins.isinstance(h, _python_builtins.bool) or not _python_builtins.isinstance(h, _python_builtins.int) or h < 1 for h in HORIZONS)
            or _python_builtins.len(_python_builtins.set(HORIZONS)) != _python_builtins.len(HORIZONS)):
        raise _python_builtins.ValueError('Invalid HORIZONS/PRIMARY_HORIZON/ROTATE_EVERY')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    log.set_level('order', 'error')
    g.day, g.pending = -1, None
    g.active, g.rows, g.audit = [], [], []
    g.written = _python_builtins.set()
    g.signal_count, g.eligible_records = 0, 0
    g.first_day, g.last_day = None, None
    run_daily(on_open, time='09:31')
    run_daily(on_signal, time='14:55')
    log.info('低预期＋盈利维持v2独立实验：无需上传旧CSV。'
             '个位数PE(TTM) × 已披露TTM归母利润不下降；每20日冻结，主60日。'
             '诊断脚本不下单，请看日志与导出，空账户收益不是实验结果。')


def empty_profit_snapshot():
    frame = pd.DataFrame(columns=['code', 'signal', 'asof', 'report', 'pe_ratio',
                                  'market_cap', 'industry', 'momentum120',
                                  'low_expectations', 'profit_maintained', 'cell'])
    for name in ('low_expectations', 'profit_maintained'):
        frame[name] = frame[name].astype(_python_builtins.bool)
    return frame


def build_profit_snapshot(asof, signal):
    """T-1截面；利润仅取已披露八个单季度，控制变量完整后冻结共同样本。"""
    asof, signal = pd.Timestamp(asof).normalize(), pd.Timestamp(signal).normalize()
    securities = get_all_securities(['stock'], date=asof.date())
    codes = [c for c in securities.index if c[:2] in ('60', '00')
             and pd.notna(securities.loc[c, 'start_date'])
             and (asof - pd.Timestamp(securities.loc[c, 'start_date'])).days >= MIN_LISTING_DAYS]
    audit = _python_builtins.dict(signal=signal, asof=asof, listed_mainboard_n=_python_builtins.len(codes),
                 nonst_unpaused_n=0, positive_pe_n=0, profit_eligible_n=0,
                 control_complete_n=0, common_sample_n=0, A_n=0, B_n=0, C_n=0, D_n=0,
                 profit_exclusions='{}')
    empty = empty_profit_snapshot()
    no_features = pd.DataFrame(columns=['code', 'signal', 'asof', 'profit_status'])
    if not codes:
        return empty, empty.copy(), no_features, audit
    eligible = []
    for batch in chunks(codes):
        st = get_extras('is_st', batch, start_date=asof, end_date=asof, df=True)
        eligible += [c for c in batch if c in st and not st.empty
                     and pd.notna(st[c].iloc[-1]) and not _python_builtins.bool(st[c].iloc[-1])]
    status = price_frame(eligible, asof, ['paused'])
    status = status.set_index('code')['paused'] if not status.empty else pd.Series(dtype=_python_builtins.float)
    codes = [c for c in eligible if c in status.index and status.loc[c] == 0]
    audit['nonst_unpaused_n'] = _python_builtins.len(codes)
    if not codes:
        return empty, empty.copy(), no_features, audit
    valuations = []
    for batch in chunks(codes):
        valuations.append(get_fundamentals(query(valuation.code, valuation.pe_ratio,
                                                 valuation.market_cap).filter(
                                                     valuation.code.in_(batch)), date=asof))
    val = pd.concat(valuations, ignore_index=True).set_index('code')
    if val.index.has_duplicates or _python_builtins.set(val.index) - _python_builtins.set(codes):
        raise _python_builtins.ValueError('Invalid valuation membership')
    val['pe_ratio'], val['market_cap'] = clean(val.pe_ratio), clean(val.market_cap)
    val = val[(val.pe_ratio > 0) & (val.market_cap > 0)]
    audit['positive_pe_n'] = _python_builtins.len(val)
    if val.empty:
        return empty, empty.copy(), no_features, audit
    financials = []
    for batch in chunks(_python_builtins.list(val.index)):
        rows = get_history_fundamentals(
            batch, [income.statDate, income.pubDate, income.np_parent_company_owners],
            watch_date=asof, count=8, interval='1q', stat_by_year=False)
        if rows is None or _python_builtins.set(HISTORY_COLUMNS) - _python_builtins.set(rows.columns):
            raise _python_builtins.ValueError('Missing profit history schema')
        if _python_builtins.set(rows.code) - _python_builtins.set(batch):
            raise _python_builtins.ValueError('Unexpected profit history membership')
        financials.append(rows[_python_builtins.list(HISTORY_COLUMNS)])
    history = pd.concat(financials, ignore_index=True)
    # 保留取回的全部记录，让财务验证明确排除未披露、重复、缺季等问题。
    history['statDate'] = history.statDate.apply(report_date)
    snapshot = val.reset_index()
    snapshot['signal'], snapshot['asof'] = signal, asof
    snapshot['report'] = snapshot.code.map(history.groupby('code').statDate.max())
    features = prepare_profit_features(history, snapshot, MAX_REPORT_AGE)
    export_frame('profit_history', history.assign(signal=signal, asof=asof))
    audit['profit_eligible_n'] = _python_builtins.int((features.profit_status == 'ok').sum())
    excluded = features.loc[features.profit_status != 'ok', 'profit_status'].value_counts()
    audit['profit_exclusions'] = json.dumps({key: _python_builtins.int(value) for key, value in excluded.items()},
                                           ensure_ascii=False, sort_keys=True)
    prices = price_frame(_python_builtins.list(val.index), asof, ['close'], count=121)
    prices['time'] = pd.to_datetime(prices['time']).dt.normalize()
    closes = prices.pivot(index='time', columns='code', values='close').reindex(columns=val.index)
    momentum = pd.Series(np.nan, index=val.index, dtype=_python_builtins.float)
    if _python_builtins.len(closes) == 121 and closes.index[-1] == asof:
        valid = closes.notna().all() & (closes > 0).all()
        momentum = clean(closes.iloc[-1] / closes.iloc[0] - 1).where(valid)
    snapshot['momentum120'] = snapshot.code.map(momentum)
    industries = {}
    for batch in chunks(_python_builtins.list(val.index)):
        industries.update(get_industry(batch, date=asof))
    snapshot['industry'] = snapshot.code.map({
        c: industries.get(c, {}).get('sw_l1', {}).get('industry_code') for c in val.index})
    controlled = snapshot.dropna(subset=['industry', 'momentum120'])
    audit['control_complete_n'] = _python_builtins.len(controlled)
    common = freeze_profit_groups(controlled, features).set_index('code', drop=False)
    audit['common_sample_n'] = _python_builtins.len(common)
    for name in ('A', 'B', 'C', 'D'):
        audit[name + '_n'] = _python_builtins.int((common.cell == name).sum())
    return common, snapshot, features, audit


def on_signal(context):
    if g.day < 0 or g.day % ROTATE_EVERY:
        return
    signal = pd.Timestamp(context.current_dt).normalize()
    calendar = get_trade_days(end_date=signal.date(), count=2)
    if _python_builtins.len(calendar) != 2:
        raise _python_builtins.ValueError('No T-1 trading date')
    asof = pd.Timestamp(calendar[0]).normalize()
    frame, snapshot, features, audit = build_profit_snapshot(asof, signal)
    export_frame('snapshots', snapshot.reset_index(drop=True))
    export_frame('profit_features', features)
    export_frame('frozen_groups', frame.reset_index(drop=True))
    export_frame('audit', pd.DataFrame([audit]))
    exposures = []
    for name in GROUP_NAMES:
        members = frame[frame.low_expectations] if name == 'LOW' else frame[frame.cell == name]
        for industry, piece in members.groupby('industry'):
            exposures.append(_python_builtins.dict(signal=signal, group=name, industry=industry, n=_python_builtins.len(piece),
                                  share=_python_builtins.len(piece) / _python_builtins.len(members), mean_pe=piece.pe_ratio.mean(),
                                  mean_log_cap=np.log(piece.market_cap).mean()))
    export_frame('industry_exposure', pd.DataFrame(exposures))
    g.audit.append(audit)
    g.eligible_records += _python_builtins.len(frame)
    g.signal_count += 1
    # 空样本也记录成熟窗口，避免压缩时间轴及HAC缺期位置。
    g.pending = _python_builtins.dict(signal=signal, asof=asof, frame=frame, completed=_python_builtins.set())
    log.info('冻结 %s | 主板=%d 正PE=%d 八季利润有效=%d 共同样本=%d | A/B/C/D=%d/%d/%d/%d'
             % (signal.date(), audit['listed_mainboard_n'], audit['positive_pe_n'],
                audit['profit_eligible_n'], _python_builtins.len(frame), audit['A_n'], audit['B_n'],
                audit['C_n'], audit['D_n']))
    if audit['profit_exclusions'] != '{}':
        log.info('利润排除原因：' + audit['profit_exclusions'])


def finish_period(batch, horizon, date, quotes):
    codes = _python_builtins.list(batch['frame'].index) + [BENCHMARK]
    if not batch['asof'] < batch['signal'] < batch['entry'] < date:
        raise _python_builtins.ValueError('Invalid asof/signal/entry/exit ordering')
    # 到期时重查历史入场开盘，使其与当前真实报价处于同一动态前复权参照。
    history = price_frame(codes, batch['entry'], ['open'])
    opening = (history.set_index('code')['open'].reindex(codes) if not history.empty
               else pd.Series(np.nan, index=codes, dtype=_python_builtins.float))
    entry_valid = batch['entry_valid'].reindex(codes, fill_value=False)
    exit_open = quotes.reindex(codes)
    future = clean(exit_open / opening - 1).where((opening > 0) & entry_valid)
    returns = pd.DataFrame(_python_builtins.dict(code=codes, future_return=future.values,
                                entry_valid=entry_valid.values,
                                exit_valid=exit_open.notna().values,
                                entry_adjusted_open=opening.values, exit_open=exit_open.values))
    returns['signal'], returns['entry'], returns['exit'], returns['horizon'] = (
        batch['signal'], batch['entry'], date, horizon)
    export_frame('returns', returns)
    row = evaluate_profit_period(batch['frame'], future, future.get(BENCHMARK, np.nan),
                                 MIN_CELL_N, MIN_IC_N)
    row.update(signal=batch['signal'], asof=batch['asof'], horizon=horizon,
               entry=batch['entry'], exit=date)
    g.rows.append(row)
    export_frame('periods', pd.DataFrame([row]))
    log.info('完成 %s→%s %d日 | A有效/冻结=%d/%d B=%d/%d | A−LOW=%s A−B=%s A−基准=%s'
             % (batch['entry'].date(), date.date(), horizon, row['A_valid_n'], row['A_n'],
                row['B_valid_n'], row['B_n'], format_number(row['A_minus_LOW_observed'], 100),
                format_number(row['A_minus_B_observed'], 100),
                format_number(row['A_observed_minus_benchmark'], 100)))
    if horizon == PRIMARY_HORIZON:
        display = {label: row[key] * 100 for label, key in (
            ('A减低PE_pp', 'A_minus_LOW_observed'), ('A减B_pp', 'A_minus_B_observed'),
            ('A减基准_pp', 'A_observed_minus_benchmark')) if np.isfinite(row[key])}
        if display:
            record(**display)


def on_strategy_end(context):
    log.info('[低预期＋盈利维持汇总] 报价收益，不含费用、滑点和成交限制；空账户曲线无研究意义。')
    if g.rows:
        summary = summarize_profit_periods(pd.DataFrame(g.rows), ROTATE_EVERY)
        export_frame('summary', summary)
        main = summary[(summary.horizon == PRIMARY_HORIZON) & (summary.scope == 'all')]
        for row in main.itertuples(index=False):
            log.info('60日 %s | 有效=%d/%d 均值=%s t_HAC=%s' % (
                row.metric, row.n, row.scheduled_n, format_number(row.mean, 100),
                format_number(row.t_hac)))
    unfinished = _python_builtins.sum(_python_builtins.len(HORIZONS) - _python_builtins.len(batch['completed']) for batch in g.active)
    metadata = _python_builtins.dict(version=STUDY_VERSION, first_day=_python_builtins.str(g.first_day), last_day=_python_builtins.str(g.last_day),
                    horizons=_python_builtins.list(HORIZONS), primary_horizon=PRIMARY_HORIZON,
                    rotate_every=ROTATE_EVERY, benchmark=BENCHMARK, pe_limit=PE_LIMIT,
                    min_cell_n=MIN_CELL_N, min_ic_n=MIN_IC_N, query_batch=QUERY_BATCH,
                    min_listing_days=MIN_LISTING_DAYS, max_report_age=MAX_REPORT_AGE,
                    signal_count=g.signal_count, eligible_records=g.eligible_records,
                    mature_windows=_python_builtins.len(g.rows), unfinished_horizons=unfinished,
                    pending_signal=g.pending is not None, no_orders=True,
                    no_external_input_files=True, historical_revisions_audited=False,
                    observed_means_are_not_portfolio_returns=True,
                    status=('no_eligible_eight_quarter_sample' if not g.eligible_records else
                            'no_mature_windows' if not g.rows else 'diagnostics_completed'),
                    signal='0<PE(TTM)<10; latest disclosed TTM parent profit>=prior TTM, both positive',
                    controls='continuous EP rank/industry/log cap/momentum120',
                    quote_policy='T+1 open to H trading days later open; dynamic pre-adjusted entry requery',
                    exported_csv=_python_builtins.sorted(g.written))
    write_file(OUTPUT_PREFIX + 'metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
    log.info('[导出完成] 文件前缀%s；信号=%d 共同样本记录=%d 成熟窗口=%d 未完成窗口=%d。'
             % (OUTPUT_PREFIX, g.signal_count, g.eligible_records, _python_builtins.len(g.rows), unfinished))
    log.info('A低PE且盈利维持，B低PE且下降，C其他正PE且维持，D其他正PE且下降；'
             'LOW=A+B。观察均值须结合覆盖率；本实验是原文特例的代理检验。')
