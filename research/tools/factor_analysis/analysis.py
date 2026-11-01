"""对调用方给定的因子和未来收益矩阵做统一诊断，不取数、不构造收益。"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .core import evaluate_period, membership_turnover, summarize_ic


@dataclass
class FactorResult:
    name: str
    periods: pd.DataFrame
    memberships: pd.DataFrame
    ic_summary: dict
    groups: int


def _validate_panel(frame, *, ordered=True):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError('因子与收益必须为非空 DataFrame')
    if not frame.index.is_unique or not frame.columns.is_unique:
        raise ValueError('日期和股票代码必须唯一')
    if ordered and not frame.index.is_monotonic_increasing:
        raise ValueError('日期必须升序排列')


def analyze_factor(factor, future_returns, *, name='factor', groups=5,
                   min_ic_stocks=20, hac_lags=3):
    """同一行表示同轮信号；future_returns 是调用方定义的入场到退出收益。

    缺收益的成员仍保留在原分组。收益矩阵中的额外日期和代码不影响选组。
    """
    _validate_panel(factor)
    _validate_panel(future_returns, ordered=False)
    if (isinstance(groups, bool) or not isinstance(groups, int) or groups < 2
            or isinstance(min_ic_stocks, bool) or not isinstance(min_ic_stocks, int)
            or min_ic_stocks < 2 or not isinstance(hac_lags, int) or hac_lags < 0):
        raise ValueError('分组数和最低配对数至少为 2，HAC 滞后为非负整数')
    returns = future_returns.reindex(index=factor.index, columns=factor.columns)
    records, membership_rows = [], []
    previous = {group: None for group in range(1, groups + 1)}
    for date in factor.index:
        stats, members = evaluate_period(factor.loc[date], returns.loc[date], groups, min_ic_stocks)
        for group in previous:
            current = set(members.index[members.eq(group)])
            stats[f'G{group}_membership_turnover'] = membership_turnover(previous[group], current)
            previous[group] = current or None
        records.append(stats)
        membership_rows.append(members)
    periods = pd.DataFrame(records, index=factor.index)
    memberships = pd.DataFrame(membership_rows, index=factor.index).reindex(columns=factor.columns)
    return FactorResult(name, periods, memberships, summarize_ic(periods['ic'], hac_lags), groups)


def combine_factors(factors, weights=None):
    """默认等权；显式权重原样使用并保留符号，缺任一有效权重因子则缺测。"""
    if not factors:
        raise ValueError('至少提供一个因子')
    for frame in factors.values():
        _validate_panel(frame)
    weights = dict.fromkeys(factors, 1. / len(factors)) if weights is None else dict(weights)
    if set(weights) != set(factors) or not all(np.isfinite(w) for w in weights.values()):
        raise ValueError('权重必须覆盖全部因子且为有限数值')
    active = [name for name, weight in weights.items() if weight != 0]
    if not active:
        raise ValueError('权重不能全部为零')
    index, columns = factors[active[0]].index, factors[active[0]].columns
    for name in active[1:]:
        index = index.union(factors[name].index).sort_values()
        columns = columns.union(factors[name].columns, sort=False)
    parts = [factors[name].reindex(index=index, columns=columns)
             .apply(pd.to_numeric, errors='coerce').replace([np.inf, -np.inf], np.nan)
             * weights[name] for name in active]
    # 普通相加会传播 NaN，避免缺失因子被当成零贡献。
    return sum(parts[1:], parts[0])


def analyze_factors(factors, future_returns, *, weights=None, **kwargs):
    """逐因子与等权/给定权重合成结果都调用同一个截面诊断函数。"""
    if 'composite' in factors:
        raise ValueError('composite 为合成结果的保留名称')
    composite = combine_factors(factors, weights)
    results = {name: analyze_factor(frame, future_returns, name=name, **kwargs)
               for name, frame in factors.items()}
    results['composite'] = analyze_factor(composite, future_returns, name='composite', **kwargs)
    return results
