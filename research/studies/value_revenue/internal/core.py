"""定价位置 × 经营改善：无平台依赖的信号诊断。

财务和报价可见性由调用方负责；分组在观察未来收益之前冻结。
"""
import numpy as np
import pandas as pd

from research.tools.single_factor.single_factor_test import (
    assign_groups, clean, evaluate_period, membership_turnover, neutralize,
    summarize_ic,
)


CELL_NAMES = ('A', 'B', 'C', 'D')
FACTOR_NAMES = ('value', 'improvement', 'additive', 'product')


def report_date(value):
    if pd.isna(value):
        return pd.NaT
    try:
        text = str(value).strip().lower()
        if len(text) == 6 and text[:4].isdigit() and text[4] == 'q' and text[5] in '1234':
            return pd.Period(text, freq='Q').end_time.normalize()
        date = pd.Timestamp(value).normalize()
        return date if date.is_quarter_end else pd.NaT
    except (ValueError, TypeError, OverflowError):
        return pd.NaT


def prepare_financials(rows, asof, max_report_age=240):
    """最近两个连续季度；不跳过最新一期缺值，不用过期/未披露报表替代。"""
    columns = ['code', 'statDate', 'pubDate', 'inc_revenue_year_on_year', 'roe']
    missing = set(columns) - set(rows.columns)
    if missing:
        raise ValueError('Missing financial columns: %s' % sorted(missing))
    data = rows[columns].copy()
    data['statDate'] = data['statDate'].apply(report_date)
    data['pubDate'] = pd.to_datetime(data['pubDate'], errors='coerce').dt.normalize()
    cutoff = pd.Timestamp(asof).normalize()
    visible = (data['statDate'].notna() & data['pubDate'].notna()
               & (data['statDate'] <= cutoff) & (data['pubDate'] <= cutoff)
               & (data['pubDate'] >= data['statDate']))
    data = data.loc[visible].copy()
    # 同一报告期的歧义不靠输入行顺序选择版本；排除该公司并记录覆盖损失。
    ambiguous = set(data.loc[data.duplicated(['code', 'statDate'], keep=False), 'code'])
    data = data[~data['code'].isin(ambiguous)]
    for name in ('inc_revenue_year_on_year', 'roe'):
        data[name] = clean(data[name])
    records = []
    for code, block in data.groupby('code'):
        block = block.sort_values('statDate').tail(2)
        if len(block) != 2:
            continue
        previous, latest = block.iloc[0], block.iloc[1]
        if latest['statDate'].to_period('Q').ordinal - previous['statDate'].to_period('Q').ordinal != 1:
            continue
        if (cutoff - latest['statDate']).days > max_report_age:
            continue
        values = [latest['inc_revenue_year_on_year'], previous['inc_revenue_year_on_year'], latest['roe']]
        if not np.isfinite(values).all():
            continue
        records.append(dict(code=code, report=latest['statDate'], published=latest['pubDate'],
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
        views[mode] = pd.DataFrame(dict(value=value, improvement=improvement,
                                        additive=(v + i) / 2, product=v * i))
    return views


def assign_cells(value, improvement):
    """独立二分，不把同分股票按代码拆开；EP高=低估值。"""
    value_group, improvement_group = assign_groups(value, 2), assign_groups(improvement, 2)
    cells = pd.Series(None, index=value.index, dtype=object)
    for v, i, name in ((2, 1, 'A'), (2, 2, 'B'), (1, 1, 'C'), (1, 2, 'D')):
        cells.loc[(value_group == v) & (improvement_group == i)] = name
    return cells


def evaluate_cells(cells, future_return, min_cell_n=20):
    """成员固定；任一缺报价则主口径该格缺测，不删除后重新等权。"""
    returns = clean(future_return).reindex(cells.index)
    stats = {'cell_factor_n': int(cells.notna().sum())}
    for name in CELL_NAMES:
        codes = cells.index[cells == name]
        observed = returns.reindex(codes)
        stats[name + '_n'] = len(codes)
        stats[name + '_valid_n'] = int(observed.notna().sum())
        stats[name + '_coverage'] = observed.notna().mean() if len(codes) else np.nan
        stats[name] = (observed.mean() if len(codes) >= min_cell_n and observed.notna().all()
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
    x = pd.DataFrame(dict(intercept=1.0, value=v, improvement=i, interaction=v * i,
                          log_cap=cap, momentum=centered_rank(controls['momentum120']),
                          revenue_level=centered_rank(controls['revenue_yoy'])))
    industry = controls['industry'].reindex(x.index)
    dummies = pd.get_dummies(industry, prefix='industry', drop_first=True).astype(float)
    x = pd.concat([x, dummies], axis=1)
    x.loc[industry.isna(), :] = np.nan
    y = clean(future_return).reindex(x.index)
    valid = x.notna().all(axis=1) & y.notna()
    x, y = x.loc[valid], y.loc[valid]
    stats = dict(regression_n=len(y), regression_interaction=np.nan,
                 regression_value=np.nan, regression_improvement=np.nan)
    # 常数控制变量不提供信息；主效应/交互项常数时仍应报告不可识别。
    controls_to_drop = [c for c in x if c not in ('intercept', 'value', 'improvement', 'interaction')
                        and x[c].nunique() <= 1]
    x = x.drop(columns=controls_to_drop)
    if len(y) < max(min_n, len(x.columns) + 3) or np.linalg.matrix_rank(x.values) != len(x.columns):
        return stats
    beta = pd.Series(np.linalg.lstsq(x.values, y.values, rcond=None)[0], index=x.columns)
    for name in ('interaction', 'value', 'improvement'):
        stats['regression_' + name] = float(beta[name])
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
    lags = max(3, int(np.ceil(horizon / rotate_every)) - 1)
    metrics = ['interaction', 'cheap_improvement_spread', 'expensive_improvement_spread',
               'B_minus_D', 'B_minus_A', 'regression_interaction',
               'value_ic', 'improvement_ic', 'additive_ic', 'product_ic',
               'value_high_minus_low', 'improvement_high_minus_low',
               'additive_high_minus_low', 'product_high_minus_low']
    records = []
    scopes = [('all', table)] + [(str(year), rows) for year, rows in
                                 table.groupby(pd.to_datetime(table['signal']).dt.year)]
    for scope, rows in scopes:
        for metric in metrics:
            stats = summarize_ic(rows[metric], lags)
            stats.update(scope=scope, metric=metric, horizon=horizon, hac_lags=lags,
                         scheduled_n=len(rows))
            records.append(stats)
        # 四格均值使用共同完整期，避免不同缺测期制造排序。
        common = rows[list(CELL_NAMES)].dropna()
        for name in CELL_NAMES:
            records.append(dict(scope=scope, metric=name + '_paired_mean', horizon=horizon,
                                hac_lags=lags, n=len(common), scheduled_n=len(rows),
                                mean=common[name].mean()))
    return records
