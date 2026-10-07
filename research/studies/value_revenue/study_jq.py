# -*- coding: utf-8 -*-
# 定价位置 × 经营改善：独立信号诊断，聚宽单文件，不下单。
# 自动组装源：tools/single_factor/single_factor_test.py + value_revenue/internal/core.py
#              + value_revenue/internal/platform_adapter.py；修改源后运行build_platform.py。
# 聚宽设置：2016-01-04～2026-09-30，频率“天”，基准中证800，资金任意。
# 主检验60交易日；20/120日是预先固定的辅助窗口，每20日冻结一次信号。
# 请看[预期差诊断汇总]及CSV；空账户的收益/夏普不是研究结果。
import numpy as np
import pandas as pd
import builtins as _python_builtins

def clean(series):
    return pd.to_numeric(series, errors='coerce').replace([np.inf, -np.inf], np.nan)

def neutralize(factor, industry, market_cap):
    """按代码对齐、完整样本回归；缺失行业/市值不伪装成0。"""
    frame = pd.concat([clean(factor).rename('factor'), industry.rename('industry'),
                       clean(market_cap).rename('cap')], axis=1).dropna()
    frame = frame[frame['cap'] > 0]
    result = pd.Series(np.nan, index=factor.index, dtype=_python_builtins.float)
    if frame.empty:
        return result
    cap = np.log(frame['cap'])
    cap_std = cap.std(ddof=0)
    cap = (cap - cap.mean()) / cap_std if cap_std > 0 else cap * 0
    dummies = pd.get_dummies(frame['industry'], drop_first=True).astype(_python_builtins.float)
    x = np.column_stack([np.ones(_python_builtins.len(frame)), cap.values, dummies.values])
    if _python_builtins.len(frame) <= np.linalg.matrix_rank(x) + 2:
        return result  # 自由度不足，明确缺测
    y = frame['factor'].values
    residual = y - x @ np.linalg.lstsq(x, y, rcond=None)[0]
    if np.std(residual) <= 1e-12 * _python_builtins.max(1.0, np.std(y)):
        return result  # 被解释变量完全由控制变量解释，不能凭数值噪声排名
    result.loc[frame.index] = residual
    return result

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

def evaluate_period(factor, future_return, n_groups=5, min_ic_stocks=20):
    """先按因子定组，再对齐收益；不按未来缺失情况调整成员。"""
    factor, future_return = clean(factor), clean(future_return)
    members = assign_groups(factor, n_groups)
    matched = pd.concat([factor.rename('factor'), future_return.rename('return')],
                        axis=1, sort=False).reindex(factor.index).dropna()
    ic = np.nan
    if (_python_builtins.len(matched) >= min_ic_stocks and matched['factor'].nunique() > 1
            and matched['return'].nunique() > 1):
        ic = matched['factor'].rank().corr(matched['return'].rank())
    stats = {'ic': ic, 'factor_n': _python_builtins.int(factor.notna().sum()), 'paired_n': _python_builtins.len(matched)}
    stats['coverage'] = _python_builtins.len(matched) / stats['factor_n'] if stats['factor_n'] else np.nan
    for group in _python_builtins.range(1, n_groups + 1):
        codes = members.index[members == group]
        returns = future_return.reindex(codes)
        stats['G%d_n' % group] = _python_builtins.len(codes)
        stats['G%d_coverage' % group] = returns.notna().mean() if _python_builtins.len(codes) else np.nan
        # 任一成员没有有效退出报价，本组收益缺测，不删股票后重算均值。
        stats['G%d' % group] = (returns.mean() if _python_builtins.len(codes) and returns.notna().all()
                               else np.nan)
    stats['high_minus_low'] = stats['G%d' % n_groups] - stats['G1']
    return stats, members

def membership_turnover(previous, current):
    """等权目标名单的半L1差异；不是含权重漂移/成交约束的实际换手。"""
    if previous is None or not previous or not current:
        return np.nan
    codes = _python_builtins.set(previous) | _python_builtins.set(current)
    return _python_builtins.sum(_python_builtins.abs((1 / _python_builtins.len(previous) if c in previous else 0)
                   - (1 / _python_builtins.len(current) if c in current else 0)) for c in codes) / 2

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

CELL_NAMES = ('A', 'B', 'C', 'D')

FACTOR_NAMES = ('value', 'improvement', 'additive', 'product')

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

def prepare_financials(rows, asof, max_report_age=240):
    """最近两个连续季度；不跳过最新一期缺值，不用过期/未披露报表替代。"""
    columns = ['code', 'statDate', 'pubDate', 'inc_revenue_year_on_year', 'roe']
    missing = _python_builtins.set(columns) - _python_builtins.set(rows.columns)
    if missing:
        raise _python_builtins.ValueError('Missing financial columns: %s' % _python_builtins.sorted(missing))
    data = rows[columns].copy()
    data['statDate'] = data['statDate'].apply(report_date)
    data['pubDate'] = pd.to_datetime(data['pubDate'], errors='coerce').dt.normalize()
    cutoff = pd.Timestamp(asof).normalize()
    visible = (data['statDate'].notna() & data['pubDate'].notna()
               & (data['statDate'] <= cutoff) & (data['pubDate'] <= cutoff)
               & (data['pubDate'] >= data['statDate']))
    data = data.loc[visible].copy()
    # 同一报告期的歧义不靠输入行顺序选择版本；排除该公司并记录覆盖损失。
    ambiguous = _python_builtins.set(data.loc[data.duplicated(['code', 'statDate'], keep=False), 'code'])
    data = data[~data['code'].isin(ambiguous)]
    for name in ('inc_revenue_year_on_year', 'roe'):
        data[name] = clean(data[name])
    records = []
    for code, block in data.groupby('code'):
        block = block.sort_values('statDate').tail(2)
        if _python_builtins.len(block) != 2:
            continue
        previous, latest = block.iloc[0], block.iloc[1]
        if latest['statDate'].to_period('Q').ordinal - previous['statDate'].to_period('Q').ordinal != 1:
            continue
        if (cutoff - latest['statDate']).days > max_report_age:
            continue
        values = [latest['inc_revenue_year_on_year'], previous['inc_revenue_year_on_year'], latest['roe']]
        if not np.isfinite(values).all():
            continue
        records.append(_python_builtins.dict(code=code, report=latest['statDate'], published=latest['pubDate'],
                            previous_report=previous['statDate'], previous_published=previous['pubDate'],
                            revenue_yoy=values[0], previous_revenue_yoy=values[1],
                            improvement=values[0] - values[1], roe=values[2]))
    names = ['code', 'report', 'published', 'previous_report', 'previous_published',
             'revenue_yoy', 'previous_revenue_yoy', 'improvement', 'roe']
    return pd.DataFrame(records, columns=names).set_index('code')

def centered_rank(values):
    """同分同秩，截面中心化；产品项不等价于只买低估值且改善强。"""
    ranks = clean(values).rank(method='average', pct=True)
    return ranks - ranks.mean()

def make_views(frame):
    """原始/行业-log市值残差两套，单因子、等权秩和、中心秩乘积。"""
    views = {}
    for mode in ('raw', 'neutral'):
        value = clean(frame['value'])
        improvement = clean(frame['improvement'])
        if mode == 'neutral':
            value = neutralize(value, frame['industry'], frame['market_cap'])
            improvement = neutralize(improvement, frame['industry'], frame['market_cap'])
        # 每种口径的四个因子与二维分组使用相同的有效样本。
        valid = value.notna() & improvement.notna()
        value, improvement = value.where(valid), improvement.where(valid)
        v, i = centered_rank(value), centered_rank(improvement)
        views[mode] = pd.DataFrame(_python_builtins.dict(value=value, improvement=improvement,
                                        additive=(v + i) / 2, product=v * i))
    return views

def assign_cells(value, improvement):
    """独立二分，不把同分股票按代码拆开；EP高=低估值。"""
    value_group, improvement_group = assign_groups(value, 2), assign_groups(improvement, 2)
    cells = pd.Series(None, index=value.index, dtype=_python_builtins.object)
    for v, i, name in ((2, 1, 'A'), (2, 2, 'B'), (1, 1, 'C'), (1, 2, 'D')):
        cells.loc[(value_group == v) & (improvement_group == i)] = name
    return cells

def evaluate_cells(cells, future_return, min_cell_n=20):
    """成员固定；任一缺报价则主口径该格缺测，不删除后重新等权。"""
    returns = clean(future_return).reindex(cells.index)
    stats = {'cell_factor_n': _python_builtins.int(cells.notna().sum())}
    for name in CELL_NAMES:
        codes = cells.index[cells == name]
        observed = returns.reindex(codes)
        stats[name + '_n'] = _python_builtins.len(codes)
        stats[name + '_valid_n'] = _python_builtins.int(observed.notna().sum())
        stats[name + '_coverage'] = observed.notna().mean() if _python_builtins.len(codes) else np.nan
        stats[name] = (observed.mean() if _python_builtins.len(codes) >= min_cell_n and observed.notna().all()
                       else np.nan)
    stats['cheap_improvement_spread'] = stats['B'] - stats['A']
    stats['expensive_improvement_spread'] = stats['D'] - stats['C']
    stats['interaction'] = stats['cheap_improvement_spread'] - stats['expensive_improvement_spread']
    stats['B_minus_D'] = stats['B'] - stats['D']
    stats['B_minus_A'] = stats['B'] - stats['A']
    return stats

def interaction_regression(view, controls, future_return, min_n=100):
    """秩交互项控制秩主效应、营收增长水平、120日动量、行业/log市值。

    所有秩和设计矩阵在观察收益前用完整信号样本计算。回归仅用有效报价配对，
    可能受退出报价缺失影响；系数的跨期推断另用HAC，不把截面t当时间序列t。
    """
    v, i = centered_rank(view['value']), centered_rank(view['improvement'])
    cap = np.log(clean(controls['market_cap']).where(controls['market_cap'] > 0))
    cap_std = cap.std(ddof=0)
    cap = (cap - cap.mean()) / cap_std if cap_std > 0 else cap * 0
    x = pd.DataFrame(_python_builtins.dict(intercept=1.0, value=v, improvement=i, interaction=v * i,
                          log_cap=cap, momentum=centered_rank(controls['momentum120']),
                          revenue_level=centered_rank(controls['revenue_yoy'])))
    industry = controls['industry'].reindex(x.index)
    dummies = pd.get_dummies(industry, prefix='industry', drop_first=True).astype(_python_builtins.float)
    x = pd.concat([x, dummies], axis=1)
    x.loc[industry.isna(), :] = np.nan
    y = clean(future_return).reindex(x.index)
    valid = x.notna().all(axis=1) & y.notna()
    x, y = x.loc[valid], y.loc[valid]
    stats = _python_builtins.dict(regression_n=_python_builtins.len(y), regression_interaction=np.nan,
                 regression_value=np.nan, regression_improvement=np.nan)
    # 常数控制变量不提供信息；主效应/交互项常数时仍应报告不可识别。
    controls_to_drop = [c for c in x if c not in ('intercept', 'value', 'improvement', 'interaction')
                        and x[c].nunique() <= 1]
    x = x.drop(columns=controls_to_drop)
    if _python_builtins.len(y) < _python_builtins.max(min_n, _python_builtins.len(x.columns) + 3) or np.linalg.matrix_rank(x.values) != _python_builtins.len(x.columns):
        return stats
    beta = pd.Series(np.linalg.lstsq(x.values, y.values, rcond=None)[0], index=x.columns)
    for name in ('interaction', 'value', 'improvement'):
        stats['regression_' + name] = _python_builtins.float(beta[name])
    return stats

def evaluate_snapshot(frame, view, cells, future_return, groups=5, min_ic_n=100, min_cell_n=20):
    stats = evaluate_cells(cells, future_return, min_cell_n)
    for name in FACTOR_NAMES:
        values, _ = evaluate_period(view[name], future_return, groups, min_ic_n)
        stats.update({name + '_' + key: value for key, value in values.items()})
    stats.update(interaction_regression(view, frame, future_return, min_ic_n))
    return stats

def summary_records(table, horizon, rotate_every=20):
    """重叠窗口按交易日跨度设置HAC；保留缺期位置，不压缩缺测。"""
    lags = _python_builtins.max(3, _python_builtins.int(np.ceil(horizon / rotate_every)) - 1)
    metrics = ['interaction', 'cheap_improvement_spread', 'expensive_improvement_spread',
               'B_minus_D', 'B_minus_A', 'regression_interaction',
               'value_ic', 'improvement_ic', 'additive_ic', 'product_ic',
               'value_high_minus_low', 'improvement_high_minus_low',
               'additive_high_minus_low', 'product_high_minus_low']
    records = []
    scopes = [('all', table)] + [(_python_builtins.str(year), rows) for year, rows in
                                 table.groupby(pd.to_datetime(table['signal']).dt.year)]
    for scope, rows in scopes:
        for metric in metrics:
            stats = summarize_ic(rows[metric], lags)
            stats.update(scope=scope, metric=metric, horizon=horizon, hac_lags=lags,
                         scheduled_n=_python_builtins.len(rows))
            records.append(stats)
        # 四格均值使用共同完整期，避免不同缺测期制造排序。
        common = rows[_python_builtins.list(CELL_NAMES)].dropna()
        for name in CELL_NAMES:
            records.append(_python_builtins.dict(scope=scope, metric=name + '_paired_mean', horizon=horizon,
                                hac_lags=lags, n=_python_builtins.len(common), scheduled_n=_python_builtins.len(rows),
                                mean=common[name].mean()))
    return records

"""聚宽适配层，数据导入与引擎注入函数均按平台标准全局入口调用。

本文件供build_platform.py组装；组装时限定所有Python内置函数引用，
粘贴运行研究目录的study_jq.py。
"""
import json
from jqdata import *


ROTATE_EVERY = 20
HORIZONS = (20, 60, 120)
PRIMARY_HORIZON = 60
MIN_LISTING_DAYS = 365  # 自然日；另外要求完整121根收盘
MAX_REPORT_AGE = 240   # 自然日；不把长期未更新的财务当作当前经营信号
MIN_IC_N = 100
MIN_CELL_N = 20
N_GROUPS = 5
QUERY_BATCH = 400
BENCHMARK = '000906.XSHG'
OUTPUT_PREFIX = 'expectations_v1_'
STUDY_VERSION = 'expectations_v1_2026-10-06_runtime_fix'


def initialize(context):
    if (PRIMARY_HORIZON not in HORIZONS or ROTATE_EVERY < 1 or not HORIZONS
            or _python_builtins.any(_python_builtins.isinstance(h, _python_builtins.bool) or not _python_builtins.isinstance(h, _python_builtins.int) or h < 1 for h in HORIZONS)
            or _python_builtins.len(_python_builtins.set(HORIZONS)) != _python_builtins.len(HORIZONS)):
        raise _python_builtins.ValueError('Invalid HORIZONS/PRIMARY_HORIZON/ROTATE_EVERY')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    log.set_level('order', 'error')
    g.day = -1
    g.pending = None
    g.active = []
    g.rows = []
    g.audit = []
    g.written = _python_builtins.set()
    g.previous_cells = {(mode, h, cell): None for mode in ('raw', 'neutral')
                        for h in HORIZONS for cell in CELL_NAMES}
    g.signal_count = 0
    g.first_day = None
    g.last_day = None
    run_daily(on_open, time='09:31')
    run_daily(on_signal, time='14:55')
    log.info('预期差v1：盈利收益率×营收同比加速；主窗口60日，辅助20/120日；'
                '全A主板正PE，无质量/估值上限/低波/解禁筛选；不下单。')


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


def build_snapshot(asof):
    """所有筛选、财务、估值、控制变量截止T-1；请求失败终止而非放行。"""
    asof = pd.Timestamp(asof).normalize()
    securities = get_all_securities(['stock'], date=asof.date())
    codes = [c for c in securities.index
             if c[:2] in ('60', '00')
             and pd.notna(securities.loc[c, 'start_date'])
             and (asof - pd.Timestamp(securities.loc[c, 'start_date'])).days >= MIN_LISTING_DAYS]
    audit = _python_builtins.dict(asof=asof, listed_mainboard_n=_python_builtins.len(codes), nonst_unpaused_n=0,
                 positive_pe_n=0, financial_pair_n=0, common_sample_n=0)
    if not codes:
        return empty_snapshot(), audit
    eligible = []
    for batch in chunks(codes):
        st = get_extras('is_st', batch, start_date=asof, end_date=asof, df=True)
        eligible += [c for c in batch if c in st and not st.empty
                     and pd.notna(st[c].iloc[-1]) and not _python_builtins.bool(st[c].iloc[-1])]
    status = price_frame(eligible, asof, ['paused'])
    if not status.empty:
        status = status.set_index('code')['paused']
    else:
        status = pd.Series(dtype=_python_builtins.float)
    codes = [c for c in eligible if c in status.index and status.loc[c] == 0]
    audit['nonst_unpaused_n'] = _python_builtins.len(codes)
    if not codes:
        return empty_snapshot(), audit
    valuations, financials = [], []
    for batch in chunks(codes):
        valuations.append(get_fundamentals(query(valuation.code, valuation.pe_ratio,
                                                 valuation.market_cap).filter(
                                                     valuation.code.in_(batch)), date=asof))
        financials.append(get_history_fundamentals(
            batch, [indicator.statDate, indicator.pubDate,
                    indicator.inc_revenue_year_on_year, indicator.roe],
            watch_date=asof, count=2, interval='1q', stat_by_year=False))
    val = pd.concat(valuations, ignore_index=True).set_index('code')
    if val.index.has_duplicates:
        raise _python_builtins.ValueError('Duplicate valuation codes')
    val['pe_ratio'], val['market_cap'] = clean(val['pe_ratio']), clean(val['market_cap'])
    val = val[(val['pe_ratio'] > 0) & (val['market_cap'] > 0)]
    audit['positive_pe_n'] = _python_builtins.len(val)
    fin = prepare_financials(pd.concat(financials, ignore_index=True), asof, MAX_REPORT_AGE)
    frame = val.join(fin, how='inner')
    audit['financial_pair_n'] = _python_builtins.len(frame)
    frame['value'] = 1.0 / frame['pe_ratio']
    if frame.empty:
        return empty_snapshot(), audit
    prices = price_frame(_python_builtins.list(frame.index), asof, ['close'], count=121)
    prices['time'] = pd.to_datetime(prices['time']).dt.normalize()
    closes = prices.pivot(index='time', columns='code', values='close').reindex(columns=frame.index)
    momentum = pd.Series(np.nan, index=frame.index, dtype=_python_builtins.float)
    if _python_builtins.len(closes) == 121 and closes.index[-1] == asof:
        valid = closes.notna().all() & (closes > 0).all()
        momentum = clean(closes.iloc[-1] / closes.iloc[0] - 1).where(valid)
    frame['momentum120'] = momentum
    industries = {}
    for batch in chunks(_python_builtins.list(frame.index)):
        industries.update(get_industry(batch, date=asof))
    frame['industry'] = pd.Series({c: industries.get(c, {}).get('sw_l1', {}).get('industry_code')
                                  for c in frame.index}, dtype=_python_builtins.object)
    # 同一完整样本比较原始与控制口径；缺失控制变量不补零。
    frame = frame.dropna(subset=['value', 'improvement', 'revenue_yoy', 'market_cap',
                                'momentum120', 'industry'])
    audit['common_sample_n'] = _python_builtins.len(frame)
    return frame, audit


def empty_snapshot():
    return pd.DataFrame(columns=['value', 'improvement', 'industry', 'market_cap',
                                 'momentum120', 'revenue_yoy'])


def on_signal(context):
    if g.day < 0 or g.day % ROTATE_EVERY:
        return
    signal = pd.Timestamp(context.current_dt).normalize()
    calendar = get_trade_days(end_date=signal.date(), count=2)
    if _python_builtins.len(calendar) != 2:
        raise _python_builtins.ValueError('No T-1 trading date')
    asof = pd.Timestamp(calendar[0]).normalize()
    frame, audit = build_snapshot(asof)
    views = make_views(frame)
    cells = {mode: assign_cells(view['value'], view['improvement']) for mode, view in views.items()}
    snapshot = frame.copy()
    snapshot['code'] = snapshot.index
    snapshot['signal'], snapshot['asof'] = signal, asof
    for mode, view in views.items():
        snapshot[mode + '_cell'] = cells[mode]
        for name in FACTOR_NAMES:
            snapshot[mode + '_' + name] = view[name]
        audit[mode + '_factor_n'] = _python_builtins.int(view['value'].notna().sum())
    audit['signal'] = signal
    g.audit.append(audit)
    export_frame('snapshots', snapshot.reset_index(drop=True))
    export_frame('audit', pd.DataFrame([audit]))
    g.signal_count += 1
    g.pending = _python_builtins.dict(signal=signal, asof=asof, frame=frame, views=views, cells=cells,
                     completed=_python_builtins.set())
    log.info('冻结信号 %s | 主板上市=%d | 完整样本=%d | 中性有效=%d' % (
        signal.date(), audit['listed_mainboard_n'], _python_builtins.len(frame), audit['neutral_factor_n']))


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


def finish_period(batch, horizon, date, quotes):
    codes = _python_builtins.list(batch['frame'].index) + [BENCHMARK]
    if not batch['asof'] < batch['signal'] < batch['entry'] < date:
        raise _python_builtins.ValueError('Invalid asof/signal/entry/exit ordering')
    # 到期后重查入场开盘，沿用既有平台诊断的动态前复权口径。
    history = price_frame(codes, batch['entry'], ['open'])
    opening = (history.set_index('code')['open'].reindex(codes) if not history.empty
               else pd.Series(np.nan, index=codes, dtype=_python_builtins.float))
    future = clean(quotes.reindex(codes) / opening - 1).where(
        (opening > 0) & batch['entry_valid'].reindex(codes, fill_value=False))
    returns = pd.DataFrame(_python_builtins.dict(code=codes, future_return=future.reindex(codes).values,
                                entry_valid=batch['entry_valid'].reindex(codes).values))
    returns['signal'], returns['entry'], returns['exit'], returns['horizon'] = (
        batch['signal'], batch['entry'], date, horizon)
    export_frame('returns', returns)
    rows = []
    for mode, view in batch['views'].items():
        cells = batch['cells'][mode]
        stats = evaluate_snapshot(batch['frame'], view, cells, future,
                                  N_GROUPS, MIN_IC_N, MIN_CELL_N)
        stats.update(mode=mode, horizon=horizon, signal=batch['signal'], asof=batch['asof'],
                     entry=batch['entry'], exit=date, pool_n=_python_builtins.len(batch['frame']),
                     benchmark=future.get(BENCHMARK, np.nan))
        for name in CELL_NAMES:
            current = _python_builtins.set(cells.index[cells == name])
            key = (mode, horizon, name)
            stats[name + '_membership_turnover'] = membership_turnover(g.previous_cells[key], current)
            g.previous_cells[key] = current or None
        rows.append(stats)
        g.rows.append(stats)
        if horizon == PRIMARY_HORIZON and mode == 'raw':
            display = {name: stats[key] * 100 for name, key in (
                ('60日交互差_pp', 'interaction'), ('60日配对覆盖_pct', 'value_coverage'))
                       if np.isfinite(stats[key])}
            if display:
                record(**display)
    export_frame('periods', pd.DataFrame(rows))
    primary = rows[0]
    log.info('完成 %s→%s %d日 | 配对覆盖=%.1f%% | 四格交互差=%s' % (
        batch['entry'].date(), date.date(), horizon, primary['value_coverage'] * 100,
        format_number(primary['interaction'], 100)))
    log.info('  四格有效/冻结人数 %s | EP_IC=%s 改善_IC=%s 回归交互系数=%s' % (
        ' '.join('%s:%d/%d' % (name, primary[name + '_valid_n'], primary[name + '_n'])
                 for name in CELL_NAMES),
        format_number(primary['value_ic']), format_number(primary['improvement_ic']),
        format_number(primary['regression_interaction'])))


def format_number(value, scale=1):
    return 'NA' if not np.isfinite(value) else '%+.5f' % (value * scale)


def on_strategy_end(context):
    log.info('[预期差诊断汇总] 报价口径，不含费用/滑点/成交限制；不是策略收益。')
    summaries = []
    if g.rows:
        table = pd.DataFrame(g.rows)
        for (mode, horizon), block in table.groupby(['mode', 'horizon']):
            block = block.sort_values('signal')
            records = summary_records(block, _python_builtins.int(horizon), ROTATE_EVERY)
            for row in records:
                row['mode'] = mode
            summaries += records
            for row in records:
                if row['metric'] in ('interaction', 'regression_interaction', 'value_ic',
                                     'improvement_ic', 'additive_ic', 'product_ic'):
                    log.info('%s %d日 %s %s | 有效=%d/%d 均值=%s t_HAC=%s' % (
                        mode, horizon, row['scope'], row['metric'], row['n'], row['scheduled_n'],
                        format_number(row['mean']), format_number(row.get('t_hac', np.nan))))
            common = block[_python_builtins.list(CELL_NAMES)].dropna()
            log.info('  四格共同有效=%d/%d；A/B/C/D均值=%s；平均配对覆盖=%.1f%%' % (
                _python_builtins.len(common), _python_builtins.len(block), common.mean().to_dict(), block['value_coverage'].mean() * 100))
    export_frame('summary', pd.DataFrame(summaries))
    unfinished = _python_builtins.sum(_python_builtins.len(HORIZONS) - _python_builtins.len(b['completed']) for b in g.active)
    metadata = _python_builtins.dict(version=STUDY_VERSION, first_day=_python_builtins.str(g.first_day), last_day=_python_builtins.str(g.last_day),
                    horizons=_python_builtins.list(HORIZONS), primary_horizon=PRIMARY_HORIZON,
                    rotate_every=ROTATE_EVERY, min_ic_n=MIN_IC_N, min_cell_n=MIN_CELL_N,
                    min_listing_days=MIN_LISTING_DAYS, max_report_age=MAX_REPORT_AGE,
                    query_batch=QUERY_BATCH, n_groups=N_GROUPS, benchmark=BENCHMARK,
                    signal_count=g.signal_count, unfinished_horizons=unfinished,
                    pending_signal=g.pending is not None,
                    factor='EP=1/PE(TTM); improvement=latest quarterly revenue YoY minus prior quarter (pp)',
                    universe='historical mainboard, listing>=365 calendar days, non-ST, unpaused at T-1, PE>0',
                    controls='industry/log cap/momentum120/current revenue YoY',
                    strict_cell_missing=True, historical_revisions_audited=False,
                    exported_csv=_python_builtins.sorted(g.written))
    write_file(OUTPUT_PREFIX + 'metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
    log.info('[导出完成] %d张CSV+metadata.json；文件在聚宽投资研究根目录，前缀%s。'
                '未完成窗口=%d，未生效信号=%d。' % (
                    _python_builtins.len(g.written), OUTPUT_PREFIX, unfinished, _python_builtins.int(g.pending is not None)))
    log.info('四格：(B-A)-(D-C)，A低估值弱改善/B低估值强改善/C高估值弱改善/D高估值强改善。'
                '历史区间已经观察过；正交互差不是已验证的可交易alpha。')
