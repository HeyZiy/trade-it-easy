"""可调用的聚宽研究流程；股票池、时间窗口和收益端点由配置或研究回调指定。"""
from dataclasses import dataclass
import numpy as np
import pandas as pd
from .core import (clean, mad_outlier, neutralize, evaluate_period,
                   membership_turnover, summarize_ic)


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
