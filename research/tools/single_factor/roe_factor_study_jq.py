# -*- coding: utf-8 -*-
# ROE完整候选池的1日/5日因子诊断，聚宽回测编辑器单文件版本。
# 无需上传其他文件，无需本地JQData账户；不下单，平台策略收益不是诊断收益。
# 因子截至T-1，信号T日14:55，端点为下一交易日9:31可见的开盘报价。
# 使用到期后的数据计算IC，不在选股时查询未来收益。
# 诊断报价收益不含费用、滑点、涨跌停成交约束及停牌延迟退出。
# 脚本由v1.1.2候选池函数和独立因子分析函数组装；运行时没有外部文件依赖。
import hashlib
import math
import numpy as np
import pandas as pd
from jqdata import *
import builtins as _python_builtins


TOP_N = 10

HOLD_DAYS = 20

ROTATE_EVERY = 20

ROE_MIN = 12.0

PE_MAX = 30.0

PB_MAX = 5.0

NP_YOY_MIN = 10.0

VOL60_MAX = 35.0

VOL_LOOKBACK = 61

LIMIT_UP_PAD = 0.001

FEE_COMMISSION = 0.00025

FEE_TAX = 0.001

BAN_WINDOW = 90

RANK_MODE = 'prev_change'

RANDOM_SEED = 0

PLOT_DIAGNOSTICS = False

PLOT_RANK_CONTRIBUTIONS = True


# 诊断参数，不改变原候选池筛选条件
FACTOR_WINDOWS = (1, 5)
N_GROUPS = 5
MIN_IC_STOCKS = 20
NEUTRALIZE = True
HAC_LAGS = 3
PLOT_MODE = 'spread'  # spread / ic / groups / coverage / off


def clean(series):
    return pd.to_numeric(series, errors='coerce').replace([np.inf, -np.inf], np.nan)

def mad_outlier(series, scale=None):
    series = clean(series)
    if scale is None:
        return series
    median = series.median()
    mad = (series - median).abs().median()
    if not np.isfinite(mad) or mad == 0:
        return series
    width = scale * 1.4826 * mad
    return series.clip(median - width, median + width)

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
                        axis=1).reindex(factor.index).dropna()
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
    return _python_builtins.sum(abs((1 / _python_builtins.len(previous) if c in previous else 0)
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

def _rank_candidates(cands, change, asof):
    if RANK_MODE == 'prev_change':
        # 保留 v1.1 的稳定排序及同分时输入顺序。
        return _python_builtins.sorted(cands, key=lambda code: -change[code])
    if RANK_MODE != 'random':
        raise ValueError('RANK_MODE must be random or prev_change')
    prefix = '%s|%s|' % (RANDOM_SEED, pd.Timestamp(asof).strftime('%Y-%m-%d'))
    return _python_builtins.sorted(cands, key=lambda code: (
        hashlib.sha256((prefix + code).encode('utf-8')).hexdigest(), code))

def _report_date(value):
    """解析真实季度末日期；兼容 YYYYqN，缺失或非法值不猜测。"""
    if pd.isna(value):
        return pd.NaT
    text = _python_builtins.str(value).strip().lower()
    try:
        if (_python_builtins.len(text) == 6 and text[:4].isdigit()
                and text[4] == 'q' and text[5] in '1234'):
            return pd.Period(text, freq='Q').end_time.normalize()
        date = pd.Timestamp(value).normalize()
        return date if date.is_quarter_end else pd.NaT
    except (TypeError, ValueError, OverflowError):
        return pd.NaT

def _prepare_fundamentals(rows, asof):
    """仅保留截止日已披露的有效季度指标，ROE(%)按单季度×4年化近似。"""
    ind = rows.copy().set_index('code')
    cutoff = pd.Timestamp(asof).normalize()
    report_dates = ind['statDate'].apply(_report_date)
    publication_dates = pd.to_datetime(ind['pubDate'], errors='coerce')
    visible = (report_dates.notna() & publication_dates.notna()
               & (report_dates <= cutoff) & (publication_dates <= cutoff))
    ind = ind.loc[visible].copy()
    for field in ('roe', 'inc_net_profit_year_on_year'):
        ind[field] = pd.to_numeric(ind[field], errors='coerce')
    ind['roe_ann'] = ind['roe'] * 4.0
    ind = ind.replace([_python_builtins.float('inf'), _python_builtins.float('-inf')], _python_builtins.float('nan'))
    return ind.dropna(subset=['roe_ann', 'inc_net_profit_year_on_year'])

def _prev_trade_day(dt):
    """T-1 交易日：统一财务、估值和历史日线的可见截止日。"""
    return get_trade_days(end_date=dt.date(), count=2)[0]

def _mainboard_pool(context):
    """全 A 主板（时点）：剔科创板(68x)、创业板(30x)、北交所(4/8/92 前缀)。"""
    secs = get_all_securities(['stock'], date=context.current_dt)
    return [c for c in secs.index
            if not (c[:2] in ('68', '30') or c[0] in ('4', '8') or c[:2] == '92')]

def _st_filter(cands, dt):
    """剔除信号日 ST（含*ST）。"""
    try:
        st = get_extras('is_st', security_list=cands, end_date=dt, count=1)
        return [c for c in cands if not _python_builtins.bool(st[c].iloc[-1])]
    except Exception as exc:
        g.factor_audit.append((dt, '_st_filter', type(exc).__name__))
        log.warn('筛选请求失败后沿用原版放行：_st_filter %s' % type(exc).__name__)
        return cands

def _ban_filter(cands, dt):
    """剔除未来 BAN_WINDOW 自然日内有解禁的标的（数据缺失时放行）。"""
    try:
        rows = get_locked_shares(stock_list=cands, start_date=dt,
                                 end_date=dt + pd.Timedelta(days=BAN_WINDOW))
        banned = _python_builtins.set(rows['code']) if rows is not None and _python_builtins.len(rows) else _python_builtins.set()
        return [c for c in cands if c not in banned]
    except Exception as exc:
        g.factor_audit.append((dt, '_ban_filter', type(exc).__name__))
        log.warn('筛选请求失败后沿用原版放行：_ban_filter %s' % type(exc).__name__)
        return cands

def build_signal(context):
    """14:55 选股：沿用 v1.1 筛选，按 RANK_MODE 排序。"""
    g.diag_reference_ranked = []  # 所有空池提前返回也必须清空影子名单
    dt = context.current_dt
    asof = _prev_trade_day(dt)
    pool = _mainboard_pool(context)

    ind = get_fundamentals(
        query(indicator.code, indicator.roe,
              indicator.inc_net_profit_year_on_year, indicator.statDate,
              indicator.pubDate), date=asof)
    ind = _prepare_fundamentals(ind, asof)
    ind = ind[(ind['roe_ann'] > ROE_MIN)
              & (ind['inc_net_profit_year_on_year'] > NP_YOY_MIN)]
    cands = [c for c in pool if c in ind.index]
    if not cands:
        return []

    cands = _st_filter(cands, dt)
    if not cands:
        return []

    val = get_fundamentals(
        query(valuation).filter(valuation.code.in_(cands)), date=asof)
    val = val.set_index('code')
    val = val[(val['pe_ratio'] < PE_MAX) & (val['pb_ratio'] < PB_MAX)]
    cands = [c for c in cands if c in val.index]
    if not cands:
        return []

    cd = get_current_data()
    cands = [c for c in cands if not cd[c].paused]

    closes = history(VOL_LOOKBACK, '1d', 'close', security_list=cands)
    cands = [c for c in cands if closes[c].notna().sum() >= VOL_LOOKBACK]
    if not cands:
        return []
    rets = closes[cands].pct_change()
    vol60 = rets.std() * math.sqrt(250) * 100
    cands = [c for c in cands if vol60[c] < VOL60_MAX]
    if not cands:
        return []

    cands = _ban_filter(cands, dt)
    if not cands:
        return []

    chg = {c: closes[c].iloc[-1] / closes[c].iloc[-2] - 1 for c in cands}
    g.diag_reference_ranked = _python_builtins.sorted(cands, key=lambda code: -chg[code])
    return _rank_candidates(cands, chg, asof)


# 保留平台history，并在候选池查询时捕获同一份行情供两个窗口复用。
_jq_history = history


def history(count, unit='1d', field='close', security_list=None, **kwargs):
    values = _jq_history(count, unit, field, security_list=security_list, **kwargs)
    if unit == '1d' and field == 'close':
        g.factor_closes = values.copy()
    return values


def initialize(context):
    if PLOT_MODE not in ('spread', 'ic', 'groups', 'coverage', 'off'):
        raise ValueError('Unsupported PLOT_MODE')
    if (not FACTOR_WINDOWS or _python_builtins.any(_python_builtins.isinstance(w, _python_builtins.bool) or not _python_builtins.isinstance(w, _python_builtins.int)
                                 or not 1 <= w < VOL_LOOKBACK for w in FACTOR_WINDOWS)):
        raise ValueError('FACTOR_WINDOWS must contain integers from 1 to VOL_LOOKBACK-1')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark('000300.XSHG')
    g.day = 0
    g.diag_reference_ranked = []
    g.factor_closes = None
    g.factor_pending = None
    g.factor_active = None
    g.factor_audit = []
    g.factor_signals = []
    styles = ['raw', 'neutral'] if NEUTRALIZE else ['raw']
    g.factor_modes = ['M%d_%s' % (w, style) for w in FACTOR_WINDOWS for style in styles]
    g.factor_rows = {m: [] for m in g.factor_modes}
    g.factor_previous_groups = {m: {i: None for i in _python_builtins.range(1, N_GROUPS + 1)}
                                for m in g.factor_modes}
    g.factor_plot = {}
    run_daily(factor_open, time='9:31')
    run_daily(factor_close, time='14:55')
    run_daily(factor_mark, time='14:56')
    log.info('ROE因子诊断：窗口=%s；每%d交易日选股；完整候选池，不下单。'
             % (FACTOR_WINDOWS, ROTATE_EVERY))
    log.info('请看自定义画线及日志；平台账户收益不代表分组收益。')


def _factor_controls(codes, asof):
    if not codes:
        return pd.Series(dtype=object), pd.Series(dtype=_python_builtins.float)
    info = get_industry(codes, date=asof)
    industry = pd.Series({c: info.get(c, {}).get('sw_l1', {}).get('industry_code')
                          for c in codes}, dtype=object)
    rows = get_fundamentals(query(valuation.code, valuation.market_cap).filter(
        valuation.code.in_(codes)), date=asof)
    return industry, rows.set_index('code')['market_cap']


def factor_close(context):
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY != 0:
        return
    g.factor_closes = None
    ranked = build_signal(context)
    signal = pd.Timestamp(context.current_dt).normalize()
    asof = pd.Timestamp(_prev_trade_day(context.current_dt)).normalize()
    closes = g.factor_closes
    if closes is not None and (pd.DatetimeIndex(closes.index).normalize() > asof).any():
        raise ValueError('Candidate history contains signal-day/future data')
    factors = {}
    if NEUTRALIZE:
        industry, cap = _factor_controls(ranked, asof)
    for window in FACTOR_WINDOWS:
        values = pd.Series(np.nan, index=ranked, dtype=_python_builtins.float)
        if closes is not None:
            block = closes.reindex(columns=ranked).tail(window + 1)
            if _python_builtins.len(block) == window + 1 and pd.Timestamp(block.index[-1]).normalize() == asof:
                values = clean(block.iloc[-1] / block.iloc[0] - 1).where(
                    block.notna().all() & (block > 0).all())
        factors['M%d_raw' % window] = values
        if NEUTRALIZE:
            factors['M%d_neutral' % window] = neutralize(values, industry, cap)
    # 此时只保存因子，不访问下一期收益。分组由这些因子固定。
    g.factor_pending = {'signal': signal, 'asof': asof, 'codes': ranked,
                        'factors': factors,
                        'groups': {m: assign_groups(v, N_GROUPS) for m, v in factors.items()}}
    g.factor_signals.append((signal, _python_builtins.len(ranked)))


def _opening_quotes(codes):
    current = get_current_data()
    quotes = pd.Series(np.nan, index=codes, dtype=_python_builtins.float)
    for code in codes:
        state = current[code]
        if not state.paused and np.isfinite(state.day_open) and state.day_open > 0:
            quotes.loc[code] = state.day_open
    return quotes


def _adjusted_entry_quotes(codes, entry):
    """在退出日重取已过去的入场报价，按当时动态前复权口径处理送转除权。

    use_real_price=True下历史fq=pre以当前回测日为基准，与退出日day_open相配。
    不直接用两次保存的未复权开盘价相除；无需请求今日未结束的日线。
    """
    if not codes:
        return pd.Series(dtype=_python_builtins.float)
    rows = get_price(codes, start_date=entry, end_date=entry, frequency='daily',
                     fields=['open'], fq='pre', panel=False,
                     skip_paused=False, fill_paused=False)
    if rows.empty:
        return pd.Series(np.nan, index=codes, dtype=_python_builtins.float)
    return clean(rows.set_index('code')['open']).reindex(codes)


def factor_open(context):
    if g.factor_pending is None:
        return
    date = pd.Timestamp(context.current_dt).normalize()
    pending = g.factor_pending
    if date <= pending['signal']:
        return
    active = g.factor_active
    codes = _python_builtins.set(pending['codes']) | {'000300.XSHG'}
    if active is not None:
        codes.update(active['codes'])
    quotes = _opening_quotes(_python_builtins.sorted(codes))
    if active is not None:
        old_codes = _python_builtins.list(_python_builtins.dict.fromkeys(active['codes'] + ['000300.XSHG']))
        opening = _adjusted_entry_quotes(old_codes, active['entry'])
        future = clean(quotes.reindex(old_codes) / opening - 1).where(
            (opening > 0) & active['entry_valid'].reindex(old_codes, fill_value=False))
        for mode, factor in active['factors'].items():
            stats, groups = evaluate_period(factor, future.reindex(factor.index),
                                             N_GROUPS, MIN_IC_STOCKS)
            # 固定分组应与选股时完全一致，不允许未来缺失改变成员。
            if not groups.equals(active['groups'][mode]):
                raise ValueError('Group membership changed after observing returns')
            stats.update(signal=active['signal'], asof=active['asof'], entry=active['entry'],
                         exit=date, pool_n=_python_builtins.len(active['codes']),
                         benchmark=future.get('000300.XSHG', np.nan))
            for group in _python_builtins.range(1, N_GROUPS + 1):
                members = _python_builtins.set(groups.index[groups == group])
                stats['G%d_membership_turnover' % group] = membership_turnover(
                    g.factor_previous_groups[mode][group], members)
                g.factor_previous_groups[mode][group] = members or None
            g.factor_rows[mode].append(stats)
            _update_factor_plot(mode, stats)
        log.info('完成因子区间 %s→%s；信号=%s；候选=%d；有效配对=%d/%d' % (
            active['entry'].date(), date.date(), active['signal'].date(), _python_builtins.len(active['codes']),
            g.factor_rows[g.factor_modes[0]][-1]['paired_n'], _python_builtins.len(active['codes'])))
    pending['entry'] = date
    pending['entry_valid'] = quotes.notna()
    g.factor_active = pending
    g.factor_pending = None


def _update_factor_plot(mode, stats):
    names = {'raw': '原始', 'neutral': '中性'}
    window, style = mode.split('_')
    name = window + names[style]
    values = {}
    if PLOT_MODE == 'spread':
        values[name + '_本期高减低_pp'] = stats['high_minus_low'] * 100
    elif PLOT_MODE == 'ic':
        values[name + '_RankIC_x100'] = stats['ic'] * 100
    elif PLOT_MODE == 'coverage':
        values[name + '_配对覆盖_pct'] = stats['coverage'] * 100
    elif PLOT_MODE == 'groups':
        for group in _python_builtins.range(1, N_GROUPS + 1):
            values[name + '_G%d本期收益_pct' % group] = stats['G%d' % group] * 100
    # 无效值删去，不把缺测画成0；日志/最终表中仍保留NaN。
    for key, value in values.items():
        if np.isfinite(value):
            g.factor_plot[key] = _python_builtins.round(_python_builtins.float(value), 4)
        else:
            g.factor_plot.pop(key, None)


def factor_mark(context):
    if g.factor_plot:
        record(**g.factor_plot)


def _format_factor_number(value, scale=1):
    return 'NA' if not np.isfinite(value) else '%+.4f' % (value * scale)


def on_strategy_end(context):
    log.info('================ ROE单因子诊断汇总（报价口径，不是策略收益） ================')
    sizes = [count for _, count in g.factor_signals]
    log.info('选股轮数=%d；每轮候选 均%.1f/最小%d/最大%d；筛选请求异常放行次数=%d' % (
        _python_builtins.len(sizes), np.mean(sizes) if sizes else 0, _python_builtins.min(sizes) if sizes else 0,
        _python_builtins.max(sizes) if sizes else 0, _python_builtins.len(g.factor_audit)))
    log.info('G1因子最低，G%d最高；高减低为G%d−G1。窗口未完整的期末持仓不纳入。'
             % (N_GROUPS, N_GROUPS))
    log.info('画线是最近完成区间的指标，不是连续净值；缺测不补零。')
    for mode in g.factor_modes:
        rows = g.factor_rows[mode]
        if not rows:
            log.info('%s：无完整收益区间' % mode)
            continue
        table = pd.DataFrame(rows).set_index('signal')
        stats = summarize_ic(table['ic'], HAC_LAGS)
        log.info('%s | 完整区间=%d IC有效=%d | IC=%s ICIR=%s t_iid=%s t_HAC=%s | 配对覆盖=%.1f%%' % (
            mode, _python_builtins.len(rows), stats['n'], _format_factor_number(stats['mean']),
            _format_factor_number(stats['icir']), _format_factor_number(stats['t_iid']),
            _format_factor_number(stats['t_hac']), table['coverage'].mean() * 100))
        for group in _python_builtins.range(1, N_GROUPS + 1):
            column = 'G%d' % group
            log.info('  %s | 有效期=%d/%d | 平均每期收益=%s%% | 平均名单换手=%s%%' % (
                column, table[column].count(), _python_builtins.len(rows),
                _format_factor_number(table[column].mean(), 100),
                _format_factor_number(table[column + '_membership_turnover'].mean(), 100)))
        log.info('  平均每期高减低=%s个百分点；有效期=%d' % (
            _format_factor_number(table['high_minus_low'].mean(), 100),
            table['high_minus_low'].count()))
        for year, periods in table.groupby(pd.DatetimeIndex(table.index).year):
            annual = summarize_ic(periods['ic'], HAC_LAGS)
            log.info('  %d | IC有效=%d/%d IC=%s | 高减低=%s个百分点 | 覆盖=%.1f%%' % (
                year, annual['n'], _python_builtins.len(periods), _format_factor_number(annual['mean']),
                _format_factor_number(periods['high_minus_low'].mean(), 100),
                periods['coverage'].mean() * 100))
    for date, operation, error_type in g.factor_audit:
        log.info('筛选异常：%s %s %s（原版放行）' % (date.date(), operation, error_type))
    log.info('========================================================================')
