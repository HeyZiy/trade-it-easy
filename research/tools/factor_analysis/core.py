"""因子截面计算；调用方显式提供已按研究时点构造的未来收益。"""
import numpy as np
import pandas as pd


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
    result = pd.Series(np.nan, index=factor.index, dtype=float)
    if frame.empty:
        return result
    cap = np.log(frame['cap'])
    cap_std = cap.std(ddof=0)
    cap = (cap - cap.mean()) / cap_std if cap_std > 0 else cap * 0
    dummies = pd.get_dummies(frame['industry'], drop_first=True).astype(float)
    x = np.column_stack([np.ones(len(frame)), cap.values, dummies.values])
    if len(frame) <= np.linalg.matrix_rank(x) + 2:
        return result  # 自由度不足，明确缺测
    y = frame['factor'].values
    residual = y - x @ np.linalg.lstsq(x, y, rcond=None)[0]
    if np.std(residual) <= 1e-12 * max(1.0, np.std(y)):
        return result  # 被解释变量完全由控制变量解释，不能凭数值噪声排名
    result.loc[frame.index] = residual
    return result


def assign_groups(factor, n_groups):
    """分位数分组；保留同分，边界重复导致缺组时整轮标记缺测。"""
    values = clean(factor).dropna()
    empty = pd.Series(np.nan, index=factor.index, dtype=float)
    if len(values) < n_groups or values.nunique() < n_groups:
        return empty
    labels = pd.qcut(values, q=n_groups, labels=False, duplicates='drop')
    if labels.nunique() != n_groups:
        return empty
    empty.loc[values.index] = labels + 1
    return empty


def rank_ic(factor, future_return, min_ic_stocks=20):
    """按代码配对后的 Spearman IC；不生成收益、不补缺失。"""
    matched = pd.concat([clean(factor).rename('factor'), clean(future_return).rename('return')],
                        axis=1, sort=False).reindex(factor.index).dropna()
    if (len(matched) < min_ic_stocks or matched['factor'].nunique() < 2
            or matched['return'].nunique() < 2):
        return np.nan
    from scipy.stats import spearmanr
    return float(spearmanr(matched['factor'], matched['return'])[0])


def evaluate_period(factor, future_return, n_groups=5, min_ic_stocks=20):
    """先按因子定组，再对齐收益；不按未来缺失情况调整成员。"""
    factor, future_return = clean(factor), clean(future_return)
    members = assign_groups(factor, n_groups)
    matched = pd.concat([factor.rename('factor'), future_return.rename('return')],
                        axis=1, sort=False).reindex(factor.index).dropna()
    ic = rank_ic(matched['factor'], matched['return'], min_ic_stocks)
    stats = {'ic': ic, 'factor_n': int(factor.notna().sum()), 'paired_n': len(matched)}
    stats['coverage'] = len(matched) / stats['factor_n'] if stats['factor_n'] else np.nan
    for group in range(1, n_groups + 1):
        codes = members.index[members == group]
        returns = future_return.reindex(codes)
        stats['G%d_n' % group] = len(codes)
        stats['G%d_coverage' % group] = returns.notna().mean() if len(codes) else np.nan
        # 任一成员没有有效退出报价，本组收益缺测，不删股票后重算均值。
        stats['G%d' % group] = (returns.mean() if len(codes) and returns.notna().all()
                               else np.nan)
    stats['high_minus_low'] = stats['G%d' % n_groups] - stats['G1']
    return stats, members


def membership_turnover(previous, current):
    """等权目标名单的半L1差异；不是含权重漂移/成交约束的实际换手。"""
    if previous is None or not previous or not current:
        return np.nan
    codes = set(previous) | set(current)
    return sum(abs((1 / len(previous) if c in previous else 0)
                   - (1 / len(current) if c in current else 0)) for c in codes) / 2


def summarize_ic(ic, lags=3):
    """ICIR保留符号；普通t及Bartlett/Newey-West均值t均报告，不硬设合格线。"""
    full = clean(ic)
    values = full.dropna().values.astype(float)
    n = len(values)
    result = {'n': n, 'mean': np.nan, 'icir': np.nan, 't_iid': np.nan,
              't_hac': np.nan, 'positive_fraction': np.nan}
    if n == 0:
        return result
    mean = values.mean()
    std = values.std(ddof=1) if n > 1 else np.nan
    result.update(mean=mean, positive_fraction=float((values > 0).mean()))
    if n > 1 and std > 0:
        result.update(icir=mean / std, t_iid=mean / std * np.sqrt(n))
    # 缺测间隔不能压缩成相邻期；有缺测时不输出规则等间距HAC。
    if n > 1 and full.notna().all():
        residual = values - mean
        lag_count = min(lags, n - 1)
        variance = residual @ residual / n
        for lag in range(1, lag_count + 1):
            gamma = residual[lag:] @ residual[:-lag] / n
            variance += 2 * (1 - lag / (lag_count + 1)) * gamma
        variance *= n / (n - 1)
        if variance > 0:
            result['t_hac'] = mean / np.sqrt(variance / n)
    return result
