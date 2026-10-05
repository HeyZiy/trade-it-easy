# -*- coding: utf-8 -*-
"""笔记修订版：聚宽研究环境单因子诊断，不是可成交策略回测。

默认保留原文的开盘跳空思路，正确命名 gap；Alpha191_001 用官方函数。
信号日 T，用截至 T-1 的数据；未来收益为 T+1 开盘至下一轮 T+1 开盘。
原始与行业/log市值中性化结果分别报告，G1低因子、G5高因子。
不补零缺失收益，不根据未来可交易性重新选组，不把诊断收益当实盘。
用法：聚宽研究环境 %run single_factor_test.py，或导入后 run_study(Config(...))。
本地调用需要先自行完成 jqdatasdk.auth，脚本不读取账户或自动认证。
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Config:
    start: str = '2016-01-04'
    end: str = '2026-09-01'
    index: str = '000300.XSHG'
    benchmark: str = '000300.XSHG'
    factor: str = 'gap'  # gap / momentum / alpha191_001
    lookback: int = 5   # momentum窗口；1或5可对照ROE已有实验
    frequency: str = 'monthly'  # monthly / trading_days
    rotate_every: int = 20
    groups: int = 5
    min_listing_days: int = 180
    min_ic_stocks: int = 20
    neutralize: bool = True
    mad_scale: float = None  # 默认不截断原始值；可显式设3，零MAD时不处理
    hac_lags: int = 3
    output_dir: str = 'single_factor_output'

    def validate(self):
        if pd.Timestamp(self.start) >= pd.Timestamp(self.end):
            raise ValueError('start must precede end')
        if self.factor not in ('gap', 'momentum', 'alpha191_001'):
            raise ValueError('factor must be gap, momentum or alpha191_001')
        if self.frequency not in ('monthly', 'trading_days'):
            raise ValueError('frequency must be monthly or trading_days')
        for name in ('lookback', 'rotate_every', 'groups', 'min_ic_stocks'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(name + ' must be a positive integer')
        if self.groups < 2 or self.hac_lags < 0 or self.min_listing_days < 0:
            raise ValueError('invalid groups, hac_lags or min_listing_days')
        if self.mad_scale is not None and self.mad_scale <= 0:
            raise ValueError('mad_scale must be positive or None')


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


def evaluate_period(factor, future_return, n_groups=5, min_ic_stocks=20):
    """先按因子定组，再对齐收益；不按未来缺失情况调整成员。"""
    factor, future_return = clean(factor), clean(future_return)
    members = assign_groups(factor, n_groups)
    matched = pd.concat([factor.rename('factor'), future_return.rename('return')],
                        axis=1).reindex(factor.index).dropna()
    ic = np.nan
    if (len(matched) >= min_ic_stocks and matched['factor'].nunique() > 1
            and matched['return'].nunique() > 1):
        ic = matched['factor'].rank().corr(matched['return'].rank())
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


def schedule(calendar, cfg):
    """只保留退出日不晚于end的完整区间，asof < signal < entry < exit。"""
    dates = pd.DatetimeIndex(pd.to_datetime(calendar)).normalize().unique().sort_values()
    in_window = dates[(dates >= pd.Timestamp(cfg.start)) & (dates <= pd.Timestamp(cfg.end))]
    if cfg.frequency == 'monthly':
        # calendar包含end后至少一个月，避免把月中end误认为月末。
        full_months = pd.Series(dates, index=dates).groupby(dates.to_period('M')).last()
        signals = list(full_months[(full_months >= pd.Timestamp(cfg.start))
                                  & (full_months <= pd.Timestamp(cfg.end))])
    else:
        signals = list(in_window[::cfg.rotate_every])
    entries = []
    for signal in signals:
        pos = dates.get_loc(signal)
        if pos > 0 and pos + 1 < len(dates):
            entries.append((dates[pos - 1], signal, dates[pos + 1]))
    return [(a, s, e, entries[i + 1][2]) for i, (a, s, e) in enumerate(entries[:-1])
            if entries[i + 1][2] <= pd.Timestamp(cfg.end)]


def price_panel(api, codes, field, *, start=None, end=None, count=None):
    if not codes:
        return pd.DataFrame()
    kwargs = dict(end_date=end, fields=[field], frequency='daily', panel=False,
                  fq='post', skip_paused=False, fill_paused=False)
    if start is None:
        kwargs['count'] = count
    else:
        kwargs['start_date'] = start
    data = api.get_price(list(codes), **kwargs)
    if data.empty:
        return pd.DataFrame(columns=codes, dtype=float)
    data = data.copy()
    data['time'] = pd.to_datetime(data['time']).dt.normalize()
    return data.pivot(index='time', columns='code', values=field).reindex(columns=codes)


def default_pool(api, asof, signal, cfg):
    """历史指数成分与上市日期，不从当前存活名单倒推历史。"""
    codes = list(api.get_index_stocks(cfg.index, date=asof))
    if not codes:
        return []
    listed = api.get_all_securities(['stock'], date=asof)
    cutoff = pd.Timestamp(asof) - pd.Timedelta(days=cfg.min_listing_days)
    codes = [c for c in codes if c in listed.index
             and pd.Timestamp(listed.loc[c, 'start_date']) <= cutoff]
    if not codes:
        return []
    st = api.get_extras('is_st', codes, start_date=asof, end_date=asof, df=True)
    paused = price_panel(api, codes, 'paused', start=asof, end=asof)
    return [c for c in codes if c in st and not st.empty and pd.notna(st[c].iloc[-1])
            and not bool(st[c].iloc[-1]) and c in paused and not paused.empty
            and pd.notna(paused[c].iloc[-1]) and paused[c].iloc[-1] == 0]


def factor_values(api, codes, asof, cfg):
    if cfg.factor == 'alpha191_001':
        try:
            from jqlib.alpha191 import alpha_001
        except ImportError as exc:
            raise RuntimeError('alpha191_001需要聚宽研究环境的jqlib；本地可测gap/momentum') from exc
        return clean(alpha_001(codes, end_date=asof)).reindex(codes)
    count = cfg.lookback + 1 if cfg.factor == 'momentum' else 2
    close = price_panel(api, codes, 'close', end=asof, count=count)
    result = pd.Series(np.nan, index=codes, dtype=float)
    if len(close) != count or pd.Timestamp(asof) not in close.index:
        return result
    if cfg.factor == 'momentum':
        result = close.iloc[-1] / close.iloc[0] - 1
        result = result.where(close.notna().all() & (close > 0).all())
    else:
        opening = price_panel(api, codes, 'open', start=asof, end=asof)
        if not opening.empty and pd.Timestamp(asof) in opening.index:
            result = opening.loc[pd.Timestamp(asof)] / close.iloc[-2] - 1
            result = result.where((opening.loc[pd.Timestamp(asof)] > 0) & (close.iloc[-2] > 0))
    return clean(result)


def controls(api, codes, asof):
    info = api.get_industry(codes, date=asof)
    industry = pd.Series({c: info.get(c, {}).get('sw_l1', {}).get('industry_code')
                          for c in codes}, dtype=object)
    q = api.query(api.valuation.code, api.valuation.market_cap).filter(
        api.valuation.code.in_(codes))
    cap = api.get_fundamentals(q, date=asof).set_index('code')['market_cap']
    return industry, cap


def endpoint_return(api, codes, entry, exit_date):
    """端点同一复权口径；不获取整个持有窗口，不将停牌报价当作可成交开盘。"""
    first = price_panel(api, codes, 'open', start=entry, end=entry)
    last = price_panel(api, codes, 'open', start=exit_date, end=exit_date)
    if (pd.Timestamp(entry) not in first.index or pd.Timestamp(exit_date) not in last.index):
        return pd.Series(np.nan, index=codes, dtype=float)
    start = first.loc[pd.Timestamp(entry)]
    end = last.loc[pd.Timestamp(exit_date)]
    return clean(end / start - 1).where((start > 0) & (end > 0))


def run_study(cfg=None, api=None, pool_provider=None, lookbacks=None):
    """自定义池回调 pool_provider(api, asof, signal)->代码列表；仅用当时已知信息。"""
    cfg = cfg or Config()
    cfg.validate()
    if api is None:
        try:
            import jqdata as api
        except ImportError:
            import jqdatasdk as api
    calendar = api.get_trade_days(start_date=pd.Timestamp(cfg.start) - pd.Timedelta(days=40),
                                  end_date=pd.Timestamp(cfg.end) + pd.Timedelta(days=40))
    periods = schedule(calendar, cfg)
    if not periods:
        raise ValueError('No complete evaluation periods in requested window')
    styles = ['raw', 'neutral'] if cfg.neutralize else ['raw']
    windows = None if lookbacks is None else list(dict.fromkeys(lookbacks))
    if windows is not None and (cfg.factor != 'momentum' or not windows or any(
            isinstance(w, bool) or not isinstance(w, int) or w < 1 for w in windows)):
        raise ValueError('lookbacks requires momentum and positive integer windows')
    modes = (styles if windows is None else
             ['momentum_%d_%s' % (w, style) for w in windows for style in styles])
    records, snapshots = {m: [] for m in modes}, []
    previous_members = {m: {g: None for g in range(1, cfg.groups + 1)} for m in modes}
    for number, (asof, signal, entry, exit_date) in enumerate(periods, 1):
        codes = (default_pool(api, asof, signal, cfg) if pool_provider is None
                 else list(dict.fromkeys(pool_provider(api, asof, signal))))
        if windows is None:
            factors = {'': (factor_values(api, codes, asof, cfg) if codes
                            else pd.Series(dtype=float))}
        else:
            # ROE池适配器已查询61根收盘；复用同一价格与同一日期截面。
            cached = getattr(pool_provider, 'history_closes', None)
            if (cached is not None and getattr(pool_provider, 'history_asof', None) == asof
                    and set(codes).issubset(cached.columns) and len(cached) > max(windows)):
                closes = cached.loc[cached.index <= asof, codes]
            else:
                closes = price_panel(api, codes, 'close', end=asof, count=max(windows) + 1)
            factors = {}
            for window in windows:
                block = closes.tail(window + 1)
                value = pd.Series(np.nan, index=codes, dtype=float)
                if len(block) == window + 1 and block.index[-1] == asof:
                    value = clean(block.iloc[-1] / block.iloc[0] - 1).where(
                        block.notna().all() & (block > 0).all())
                factors['momentum_%d_' % window] = value
        variants = {}
        if cfg.neutralize:
            ind, cap = controls(api, codes, asof) if codes else (pd.Series(dtype=object), pd.Series(dtype=float))
        for prefix, factor in factors.items():
            variants[prefix + 'raw'] = mad_outlier(factor, cfg.mad_scale)
            if cfg.neutralize:
                variants[prefix + 'neutral'] = neutralize(variants[prefix + 'raw'], ind, cap)
        future = endpoint_return(api, codes, entry, exit_date) if codes else pd.Series(dtype=float)
        benchmark = endpoint_return(api, [cfg.benchmark], entry, exit_date).iloc[0]
        for mode, values in variants.items():
            stats, groups = evaluate_period(values, future, cfg.groups, cfg.min_ic_stocks)
            for group in range(1, cfg.groups + 1):
                current = set(groups.index[groups == group])
                stats['G%d_membership_turnover' % group] = membership_turnover(
                    previous_members[mode][group], current)
                previous_members[mode][group] = current or None
            stats.update(signal=signal, asof=asof, entry=entry, exit=exit_date,
                         pool_n=len(codes), benchmark=benchmark)
            records[mode].append(stats)
            snap = pd.DataFrame({'factor': values, 'group': groups, 'future_return': future})
            if hasattr(pool_provider, 'ranked'):
                original_rank = {code: i for i, code in enumerate(pool_provider.ranked, 1)}
                snap['original_rank'] = pd.Series(original_rank).reindex(snap.index)
            snap['code'], snap['mode'], snap['signal'] = snap.index, mode, signal
            snapshots.append(snap.reset_index(drop=True))
        if number == 1 or number % 12 == 0 or number == len(periods):
            print('%d/%d signal=%s pool=%d raw coverage=%.1f%%' % (
                number, len(periods), signal.date(), len(codes), records[modes[0]][-1]['coverage'] * 100))
    tables = {mode: pd.DataFrame(rows).set_index('signal') for mode, rows in records.items()}
    return tables, pd.concat(snapshots, ignore_index=True)


def report(tables, cfg, plot=True):
    for mode, table in tables.items():
        window = int(mode.split('_')[1]) if mode.startswith('momentum_') else cfg.lookback
        print('\n%s %s lookback=%d | complete periods=%d' %
              (cfg.factor, mode, window, len(table)))
        print('Full-sample IC:', summarize_ic(table['ic'], cfg.hac_lags))
        print('Annual IC diagnostics (not unseen out-of-sample):')
        for year, rows in table.groupby(pd.DatetimeIndex(table.index).year):
            print(year, summarize_ic(rows['ic'], cfg.hac_lags))
        cols = ['G%d' % g for g in range(1, cfg.groups + 1)]
        print('Group mean period return:', table[cols].mean().to_dict())
        print('Valid group periods:', table[cols].count().to_dict())
        print('Mean coverage:', table['coverage'].mean())
        print('High-low mean period return:', table['high_minus_low'].mean())
        print('Equal-weight target membership turnover (not actual fills):',
              table[[c + '_membership_turnover' for c in cols]].mean().to_dict())
        # 缺测之后无法形成完整累计净值；不补零、不悄悄跳过继续连线。
        curves = (1 + table[cols + ['benchmark']]).cumprod(skipna=False) - 1
        print('Complete cumulative group returns:', curves.iloc[-1].to_dict())
        if plot:
            import matplotlib.pyplot as plt
            curves.index = pd.DatetimeIndex(table['exit'])
            ax = curves.plot(title='%s / %s: gross endpoint returns, missing stops curve' %
                             (cfg.factor, mode), figsize=(12, 5))
            ax.set_xlabel('Exit date')
            ax.set_ylabel('Cumulative return (no fees/slippage/fill constraints)')
            ax.grid(True)
            plt.tight_layout()
            plt.show()


def main():
    cfg = Config()
    tables, snapshots = run_study(cfg)
    folder = Path(cfg.output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    for mode, table in tables.items():
        table.to_csv(folder / ('periods_%s.csv' % mode), encoding='utf-8-sig')
    snapshots.to_csv(folder / 'snapshots.csv', index=False, encoding='utf-8-sig')
    report(tables, cfg)
    print('Saved:', folder.resolve())


if __name__ == '__main__':
    main()
