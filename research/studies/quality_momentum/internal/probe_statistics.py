"""候选探针汇总；缺测期与右侧截尾规则在平台、本地保持一致。"""
import builtins as _python_builtins
import pandas as pd
from research.tools.factor_analysis import summarize_ic


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

def response_summary(periods, sample_every=20):
    periods = periods.copy()
    periods['signal_day'] = pd.to_datetime(periods.signal_day)
    windows = [('full', periods), ('2020_onward', periods[periods.signal_day.dt.year.ge(2020)])]
    windows += [(_python_builtins.str(year), block) for year, block in periods.groupby(periods.signal_day.dt.year)]
    rows = []
    for window, block in windows:
        for (scope, h, group, metric), subset in block.groupby(['scope', 'horizon', 'group', 'metric']):
            mature = subset.sort_values('signal_day')
            mature = mature[mature.window_status.ne('censored')]
            stats = summarize_ic(mature.value, _python_builtins.int(h // sample_every))
            rows.append(_python_builtins.dict(
                window=window, scope=scope, horizon=h, group=group, metric=metric,
                periods=_python_builtins.len(subset), censored=_python_builtins.int(subset.window_status.eq('censored').sum()),
                valid_periods=stats['n'], mean_value=stats['mean'], positive_period_share=stats['positive_fraction'],
                t_iid=stats['t_iid'], t_hac=stats['t_hac'], available_mean=mature.available.mean(),
                coverage_mean=mature.coverage.mean(), members_mean=mature.n.mean()))
    return pd.DataFrame(rows)
