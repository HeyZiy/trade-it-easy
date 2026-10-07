# -*- coding: utf-8 -*-
"""行业动量排名的预测期限与回看窗口：独立诊断，不下单。

聚宽策略回测编辑器：复制整个文件，选择回测日期后运行。
聚宽研究 Notebook: %run study_jq.py
收盘形成信号，次日开盘进入，各区间终点为交易日开盘。

下列日志为v1旧口径（含债券），保留作历史记录；新口径使用v2输出前缀。

2026-10-01 23:59:59.001000 - INFO  - [排名诊断结束] 实际区间2016-01-04至2026-09-30，运行2611交易日，信号131批，完整成熟127批，未完成4批。

2026-10-01 23:59:59.001000 - INFO  - [排名预测力对照] IC依次为0—20 / 20—40 / 40—60 / 60—80独立区间；更新IC看20—40；换名单优势单位为百分点。

2026-10-01 23:59:59.001000 - INFO  - L=10 | IC=-0.0160 / -0.0569 / 0.0310 / 0.0511 | 更新IC=-0.0172 | IC增量=0.0398 | 换名单优势=0.6394 pp

2026-10-01 23:59:59.001000 - INFO  - L=15 | IC=-0.0316 / -0.0512 / 0.0322 / 0.0638 | 更新IC=-0.0352 | IC增量=0.0172 | 换名单优势=0.6883 pp

2026-10-01 23:59:59.001000 - INFO  - L=20 | IC=-0.0150 / -0.0611 / 0.0368 / 0.0539 | 更新IC=-0.0225 | IC增量=0.0362 | 换名单优势=0.3653 pp

2026-10-01 23:59:59.001000 - INFO  - L=25 | IC=-0.0053 / -0.0665 / 0.0329 / 0.0403 | 更新IC=-0.0142 | IC增量=0.0504 | 换名单优势=0.0402 pp

2026-10-01 23:59:59.001000 - INFO  - L=40 | IC=-0.0292 / -0.0023 / 0.0568 / 0.0445 | 更新IC=-0.0449 | IC增量=-0.0451 | 换名单优势=-0.0939 pp

2026-10-01 23:59:59.001000 - INFO  - L=60 | IC=-0.0162 / 0.0309 / 0.0375 / 0.0369 | 更新IC=-0.0328 | IC增量=-0.0655 | 换名单优势=0.1315 pp

2026-10-01 23:59:59.001000 - INFO  - L=120 | IC=0.0174 / 0.0243 / 0.0489 / 0.0633 | 更新IC=0.0134 | IC增量=-0.0115 | 换名单优势=0.6922 pp
"""
from jqdata import *

import argparse
import builtins as b
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

START, END = '2016-01-04', '2026-09-30'
LOOKBACKS = (10, 15, 20, 25, 40, 60, 120)
WINDOWS = ((0, 5), (5, 10), (10, 20), (0, 20), (20, 40),
           (40, 60), (60, 80), (0, 40), (0, 60), (0, 80))
ROTATE, CORR, MIN_IC = 20, 0.90, 8
PHASE = 0  # 策略编辑器的20日网格偏移；日期以界面设置为准。
OUTPUT_PREFIX = 'industry_momentum_rank_v2_'
EXCLUDE_CODE_PREFIXES = ('511', '513', '518')  # 511债券/货币，513跨境，518黄金现货。
EXCLUDE_KW = ('沪深300', '中证500', '中证1000', '中证800', '中证全指', '2000', '200',
              '中证A500', 'A500', 'A50', 'A100', '上证50', '上证180', '上证380', '科创50',
              '科创100', '科创综', '创业板50', '创业板综', '创业板指', '创业板',
              '双创', '北证', '深证100', '基本面50', '红利', '股息', '国债', '政金',
              '信用', '债券', '转债', '货币', '现金', '黄金', '白银', '原油',
              '纳斯达克', '纳指', '标普', '道琼', '日经', '德国', '法国',
              '亚太', '东南亚', '恒生', '香港', 'H股', '港股', '央企', '国企',
              '龙头', 'ESG', '养老', 'FOF', '联动', '增强', '价值', '成长',
              '质量', '低波', '动量', '多因子', '自由现金流',
              'HK', '225', '东证', '中韩', '美国', '恒指', '油气',
              '债', '城投', '上海金', '短融', '日利', '添益', '快线')


def allowed(code, name):
    return not code.startswith(EXCLUDE_CODE_PREFIXES) and not b.any(k in name for k in EXCLUDE_KW)


def scores(prices):
    """L+1完整收盘；w²回归/R²，近三日跳水否决，四位小数镜像原评分。

    缺失不是0分；列向量闭式回归与np.polyfit(w=linspace(1,2))等价。
    """
    values = np.asarray(prices.values, dtype=float)  # 聚宽旧版 pandas 没有 to_numpy。
    out = pd.Series(np.nan, index=prices.columns, dtype=float)
    valid = np.isfinite(values).all(axis=0) & (values > 0).all(axis=0)
    if not valid.any():
        return out
    y = np.log(values[:, valid])
    x = np.arange(len(y), dtype=float)
    weight = np.linspace(1, 2, len(y)) ** 2
    mx = np.average(x, weights=weight)
    my = np.average(y, axis=0, weights=weight)
    xc, yc = x - mx, y - my
    slope = (weight[:, None] * xc[:, None] * yc).sum(axis=0) / (weight * xc ** 2).sum()
    residual = yc - xc[:, None] * slope
    total = (weight[:, None] * yc ** 2).sum(axis=0)
    r2 = np.zeros_like(total)
    np.divide((weight[:, None] * residual ** 2).sum(axis=0), total,
              out=r2, where=total > 0)
    r2 = np.where(total > 0, np.clip(1 - r2, 0, 1), 0)
    with np.errstate(over='ignore', invalid='ignore'):
        score = np.expm1(slope * 250) * r2
    recent = values[-4:, valid]
    score[(recent[1:] / recent[:-1]).min(axis=0) < 0.95] = 0
    score[~np.isfinite(score)] = np.nan
    out.loc[valid] = np.round(score, 4)
    return out


def pool_from_history(close, money, eligible):
    """仅使用信号日之前250日；固定流动性优先0.90，与扫描窗口无关。"""
    amount = money.tail(20).mean()
    candidates = [c for c in eligible if close[c].notna().sum() >= 249
                  and pd.notna(amount.get(c)) and amount[c] >= 50_000_000]
    candidates.sort(key=lambda c: -amount[c])
    correlation = close[candidates].pct_change(fill_method=None).corr(min_periods=120)
    kept = []
    for c in candidates:
        if not b.any(pd.notna(correlation.at[c, k]) and correlation.at[c, k] >= CORR
                     for k in kept):
            kept.append(c)
    return kept, len(candidates)


def rank_ic(factor, future):
    paired = pd.concat([factor.rename('f'), future.rename('r')], axis=1).dropna()
    if len(paired) < MIN_IC or paired.f.nunique() < 2 or paired.r.nunique() < 2:
        return np.nan
    return paired.f.rank().corr(paired.r.rank())


def strict_mean(future, members):
    values = future.reindex(members)
    return values.mean() if len(values) and values.notna().all() else np.nan


def top_three(factor):
    # mergesort 在旧版 NumPy 中也可用，且保留同分成员的冻结池顺序。
    positive = factor[factor > 0].sort_values(ascending=False, kind='mergesort')
    if len(positive) < 3:
        return [], False
    tied = len(positive) > 3 and positive.iloc[2] == positive.iloc[3]
    return list(positive.index[:3]), bool(tied)


def evaluate(factor, future, members, top):
    positive = list(factor.index[factor > 0])
    row = dict(ic=rank_ic(factor, future), ic_positive=rank_ic(factor.reindex(positive), future),
               coverage=future.reindex(factor.index).notna().mean(), n=len(factor),
               n_positive=len(positive), top3=strict_mean(future, top),
               pool=strict_mean(future, factor.index), positive_pool=strict_mean(future, positive))
    for group in (1, 2, 3):
        row['G%d' % group] = strict_mean(future, members.index[members == group])
    row['high_minus_low'] = row['G3'] - row['G1']
    row['top3_minus_pool'] = row['top3'] - row['pool']
    row['top3_minus_positive'] = row['top3'] - row['positive_pool']
    return row


def summarize(series, lags):
    """等间距按信号日聚合Newey-West；不压缩缺失期，不把ETF行当独立样本。"""
    full = pd.to_numeric(series, errors='coerce').replace([np.inf, -np.inf], np.nan)
    values = np.asarray(full.dropna().values, dtype=float)
    n = len(values)
    out = dict(n=n, scheduled_n=len(full), mean=np.nan, t_hac=np.nan,
               positive_fraction=np.nan, hac_lags=lags)
    if not n:
        return out
    mean = values.mean()
    out.update(mean=mean, positive_fraction=float((values > 0).mean()))
    if n > 1 and full.notna().all():
        residual = values - mean
        var = residual @ residual / n
        for lag in range(1, min(lags, n - 1) + 1):
            var += 2 * (1 - lag / (lags + 1)) * (residual[lag:] @ residual[:-lag] / n)
        var *= n / (n - 1)
        if var > 0:
            out['t_hac'] = mean / np.sqrt(var / n)
    return out


class InjectedAPI:
    """Resolve jqdata imports and engine-provided APIs in the strategy namespace."""

    def __getattr__(self, name):
        api = globals().get(name)
        if not callable(api):
            raise RuntimeError('接口 %s 未导入；请保留文件开头的 from jqdata import *，'
                               '并在聚宽策略回测编辑器或研究环境运行。' % name)
        return api


class JoinQuantData:
    quote_policy = 'signal close; T+1 open; consistent post-adjusted prices; paused quotes missing'

    def __init__(self, start, end):
        if callable(globals().get('get_trade_days')):
            self.api = InjectedAPI()
        else:
            import jqdata
            self.api = jqdata
        self.calendar = pd.DatetimeIndex(pd.to_datetime(self.api.get_trade_days(
            start_date=pd.Timestamp(start) - pd.Timedelta(days=500), end_date=end)))

    def prices(self, codes, start, end):
        chunks = []
        for offset in range(0, len(codes), 150):
            chunks.append(self.api.get_price(codes[offset:offset + 150], start_date=start,
                end_date=end, frequency='daily', fields=['close', 'open', 'money', 'paused'],
                skip_paused=False, fill_paused=False, fq='post', panel=False))
        if not chunks:
            return {}
        raw = pd.concat(chunks, ignore_index=True)
        raw['time'] = pd.to_datetime(raw['time']).dt.normalize()
        panels = {field: raw.pivot(index='time', columns='code', values=field).reindex(columns=codes)
                  for field in ('close', 'open', 'money', 'paused')}
        for field in ('close', 'open'):
            panels[field] = panels[field].where((panels['paused'] == 0) & (panels['money'] > 0))
        return panels

    def signal_history(self, day, dates):
        """Build the pool before observing any post-signal quotes."""
        universe = self.api.get_all_securities(['etf'], date=day.date())
        eligible = [c for c, row in universe.iterrows() if allowed(c, row.display_name)
                    and (day - pd.Timestamp(row.start_date)).days >= 365]
        if not eligible:
            self.signal_names = {}
            return [], 0, pd.DataFrame(index=dates)
        # Fetch past only for pool selection; future data is not passed to that function.
        past = self.prices(eligible, dates[0].date(), day.date())
        close = past['close'].reindex(dates)
        money = past['money'].reindex(dates)
        pool, liquid_n = pool_from_history(close.iloc[:-1], money.iloc[:-1], eligible)
        self.signal_names = {c: str(universe.loc[c, 'display_name']) for c in pool}
        return pool, liquid_n, close.reindex(columns=pool)

    def load_signal(self, position):
        day = self.calendar[position]
        dates = self.calendar[position - 250:position + 1]
        pool, liquid_n, close = self.signal_history(day, dates)
        future = self.prices(pool, day.date(), self.calendar[position + 81].date())
        if not pool:
            return [], liquid_n, pd.DataFrame(index=dates), pd.DataFrame()
        full_dates = self.calendar[position - 250:position + 82]
        full_close = pd.concat([close[pool].iloc[:-1], future['close']]).reindex(full_dates)
        return pool, liquid_n, full_close, future['open'].reindex(full_dates)


TABLE_NAMES = ('periods', 'updates', 'snapshots', 'returns', 'audit')
EMPTY_COLUMNS = dict(periods=['signal', 'lookback', 'first', 'last'],
                     updates=['signal', 'lookback'],
                     snapshots=['signal', 'lookback', 'code', 'score', 'rank', 'group'],
                     returns=['signal', 'code', 'first', 'last', 'entry', 'exit', 'future_return'],
                     audit=['signal', 'pool_n', 'liquid_n', 'common_n', 'pool', 'common', 'pool_names'],
                     summary=['scope', 'metric', 'lookback', 'first', 'last', 'n', 'mean', 't_hac'])


def collect_signal(calendar, position, pool, liquid_n, close, quotes, names=None):
    """Both entries use the same frozen-sample calculations."""
    periods, snapshots, returns, updates, audits = [], [], [], [], []
    signal = calendar[position]
    # One signal sample shared by all seven L values, selected with past prices only.
    history = close.loc[:signal].tail(max(LOOKBACKS) + 1)
    common = [c for c in pool if len(history) == max(LOOKBACKS) + 1
              and history[c].notna().all() and (history[c] > 0).all()]
    audits.append(dict(signal=signal, pool_n=len(pool), liquid_n=liquid_n, common_n=len(common),
                       pool='|'.join(pool), common='|'.join(common),
                       pool_names='|'.join('%s=%s' % (c, (names or {}).get(c, '')) for c in pool)))
    for length in LOOKBACKS:
        factor = scores(close.loc[:signal, common].tail(length + 1))
        groups = pd.Series(np.nan, index=common, dtype=float)
        if len(factor) >= 3 and factor.nunique() >= 3:
            assigned = pd.qcut(factor, 3, labels=False, duplicates='drop')
            if assigned.nunique() == 3:
                groups = assigned + 1
        top, tied = top_three(factor)
        ranks = factor.rank(ascending=False, method='average')
        refreshed_day = calendar[position + 20]
        fresh = scores(close.loc[:refreshed_day, common].tail(length + 1))
        fresh_top, fresh_tied = top_three(fresh)
        for c in common:
            snapshots.append(dict(signal=signal, lookback=length, code=c, score=factor[c],
                rank=ranks[c], group=groups[c],
                top3=c in top, score_day20=fresh[c], refreshed_top3=c in fresh_top,
                top3_cutoff_tie=tied, refresh_cutoff_tie=fresh_tied))
        for first, last in WINDOWS:
            entry, exit_day = calendar[position + 1 + first], calendar[position + 1 + last]
            opening = quotes.reindex(index=[entry, exit_day], columns=common)
            future_return = opening.loc[exit_day] / opening.loc[entry] - 1
            future_return = future_return.where((opening.loc[entry] > 0) & (opening.loc[exit_day] > 0))
            row = evaluate(factor, future_return, groups, top)
            row.update(signal=signal, lookback=length, first=first, last=last,
                       entry=entry, exit=exit_day, top3_cutoff_tie=tied)
            periods.append(row)
            if (first, last) == (20, 40):
                old_mean, new_mean = strict_mean(future_return, top), strict_mean(future_return, fresh_top)
                changed = len(set(fresh_top) - set(top)) if len(top) == len(fresh_top) == 3 else np.nan
                advantage = new_mean - old_mean
                positive_old = len(top) == 3 and fresh.reindex(top).notna().all() and (fresh.reindex(top) > 0).all()
                # Compare old/new IC on identical refresh-visible scored members.
                matched_codes = list(fresh.index[fresh.notna()])
                old_ic = rank_ic(factor.reindex(matched_codes), future_return)
                new_ic = rank_ic(fresh.reindex(matched_codes), future_return)
                updates.append(dict(signal=signal, lookback=length, entry=entry, exit=exit_day,
                    aged_ic=old_ic, fresh_ic=new_ic, ic_gain=new_ic-old_ic,
                    refresh_score_coverage=len(matched_codes)/len(common) if common else np.nan,
                    old_top3=old_mean, fresh_top3=new_mean, switch_advantage=advantage,
                    changed=changed, positive_incumbents=positive_old,
                    switch_event_advantage=advantage if changed > 0 else np.nan,
                    switch_positive_incumbents=advantage if changed > 0 and positive_old else np.nan,
                    old_members='|'.join(top), fresh_members='|'.join(fresh_top)))
            # Audit every factor/future pair without altering frozen membership.
            if length == LOOKBACKS[0]:
                for c in common:
                    returns.append(dict(signal=signal, code=c, first=first, last=last,
                                        entry=entry, exit=exit_day, future_return=future_return[c]))
    return dict(periods=periods, updates=updates, snapshots=snapshots,
                returns=returns, audit=audits)


def build_summary(periods, updates):
    summary_rows = []
    scopes = [('all', None, None), ('2016_2019', '2016-01-01', '2019-12-31'),
              ('2020_2023', '2020-01-01', '2023-12-31'),
              ('2024_2025', '2024-01-01', '2025-12-31'), ('2026', '2026-01-01', '2026-12-31')]
    for scope, begin, finish in scopes:
        for frame, keys, metrics in (
            (periods, ['lookback', 'first', 'last'], ['ic', 'ic_positive', 'high_minus_low',
                'top3_minus_pool', 'top3_minus_positive', 'top3', 'coverage']),
            (updates, ['lookback'], ['aged_ic', 'fresh_ic', 'ic_gain', 'switch_advantage',
                                   'switch_event_advantage', 'switch_positive_incumbents'])):
            selected = frame if begin is None else frame[(frame.signal >= begin) & (frame.signal <= finish)]
            for values, block in selected.groupby(keys, sort=True):
                values = (values,) if not isinstance(values, tuple) else values
                identity = dict(zip(keys, values))
                lag = max(3, int(np.ceil(identity.get('last', 40) / ROTATE)) - 1)
                for metric in metrics:
                    summary_rows.append(dict(scope=scope, metric=metric, **identity,
                                              **summarize(block[metric], lag)))
    summary = pd.DataFrame(summary_rows)
    return summary


def metadata_for(start, end, cohorts, phase, quote_policy):
    metadata = dict(start=start, end=end, mature_cohorts=cohorts, rotate=ROTATE, phase=phase,
                    version=2, output_prefix=OUTPUT_PREFIX,
                    excluded_code_prefixes=list(EXCLUDE_CODE_PREFIXES), excluded_name_keywords=list(EXCLUDE_KW),
                    universe='domestic sector/theme ETFs plus commodity futures ETFs; exclude bond/money ETFs',
                    pandas_version=pd.__version__, numpy_version=np.__version__,
                    lookbacks=list(LOOKBACKS), windows=list(WINDOWS), balanced_terminal_age=80,
                    quote_policy=quote_policy,
                    dedupe='fixed liquidity-first 0.90; past-only; same pool across L',
                    splits='descriptive time slices; previously examined data, not pristine holdout',
                    update='same original pool; synthetic top3-positive cohorts, no trade exit/stops',
                    selection='no winning parameter selected; exploratory scan',
                    missing='preserved; strict group mean; IC paired with coverage; HAC missing if gaps',
                    multiplicity='exploratory; reported t statistics not corrected for parameter/window scans')
    source = globals().get('__file__')
    metadata['script_sha256'] = hashlib.sha256(Path(source).read_bytes()).hexdigest() if source and Path(source).is_file() else None
    return metadata


def report_lines(summary, cohorts, quote_policy):
    lines = ['# 排名预测期限诊断',
             '', '报价口径：' + quote_policy, '',
             '成熟信号 %d，每20交易日一轮；各L及年龄区间使用同一批80日成熟信号。' % cohorts,
             '分段窗口互不混淆；0—40等累计收益仍包含前20日，不能单独证明长效。', '',
             '| 回看L | IC 0—20 | IC 20—40 | IC 40—60 | IC 60—80 | 更新后IC 20—40 | 更新IC增量 | 换名单优势 pp |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    all_summary = summary[summary.scope == 'all']
    for length in LOOKBACKS:
        piece = all_summary[all_summary.lookback == length]
        vals = []
        for first, last in ((0, 20), (20, 40), (40, 60), (60, 80)):
            vals.append(piece[(piece.metric == 'ic') & (piece['first'] == first) & (piece['last'] == last)]['mean'].iloc[0])
        for metric in ('fresh_ic', 'ic_gain', 'switch_advantage'):
            value = piece[piece.metric == metric]['mean'].iloc[0]
            vals.append(value * 100 if metric == 'switch_advantage' else value)
        lines.append('| %d | %s |' % (length, ' | '.join('%.4f' % v for v in vals)))
    lines.extend(['', '完整HAC、正分候选IC、Top3相对正分池、分段复核见summary.csv。',
                  '各指标缺测/正分不足时有效期数可不同，比较均值时须同时查看n。',
                  'updates.csv区分全部换名单事件与旧三只在第20日均仍为正分的事件。',
                  '旧持仓为初始Top3的合成信号组；未模拟中间止损，不是原策略持仓或换仓回测。',
                  '不选择样本内最大值作为生产参数；须检查邻近窗口和各时间段的稳定性。'])
    return lines


def export_study(tables, start, end, cohorts, phase, quote_policy, write_text, emit=print):
    periods = pd.DataFrame(tables['periods'])
    updates = pd.DataFrame(tables['updates'])
    summary = build_summary(periods, updates)
    for name in TABLE_NAMES:
        frame = pd.DataFrame(tables[name]) if tables[name] else pd.DataFrame(columns=EMPTY_COLUMNS[name])
        write_text(name + '.csv', frame.to_csv(index=False))
    write_text('summary.csv', summary.to_csv(index=False))
    metadata = metadata_for(start, end, cohorts, phase, quote_policy)
    write_text('metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
    lines = report_lines(summary, cohorts, quote_policy)
    write_text('README.md', '\n'.join(lines) + '\n')
    for line in lines:
        emit(line)
    return summary


def run_study(provider, start=START, end=END, output='momentum_rank_results', phase=0):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    calendar = provider.calendar
    in_window = np.flatnonzero((calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end)))
    if not len(in_window):
        raise ValueError('No calendar in requested window')
    if not 0 <= phase < ROTATE:
        raise ValueError('phase must be in 0..19')
    positions = [int(p) for p in in_window[phase::ROTATE] if p >= 250 and p + 81 < len(calendar)
                 and calendar[p + 81] <= pd.Timestamp(end)]
    if not positions:
        raise ValueError('No complete 80-day cohorts: need signal day plus 81 future trading days')
    tables = {name: [] for name in TABLE_NAMES}
    print('开始排名诊断：%d批完整信号，结果目录 %s' % (len(positions), output), flush=True)
    for sequence, position in enumerate(positions):
        print('signal %d/%d %s: loading data' %
              (sequence + 1, len(positions), calendar[position].date()), flush=True)
        pool, liquid_n, close, quotes = provider.load_signal(position)
        batch = collect_signal(calendar, position, pool, liquid_n, close, quotes,
                               names=getattr(provider, 'signal_names', None))
        for name in TABLE_NAMES:
            tables[name].extend(batch[name])
    def save(name, content):
        (output / name).write_text(content, encoding='utf-8-sig' if name.endswith('.csv') else 'utf-8')
    summary = export_study(tables, start, end, len(positions), phase, provider.quote_policy, save)
    plot(summary, output)
    return summary


def initialize(context):
    """Executable entry for the JoinQuant strategy backtest editor."""
    if not 0 <= PHASE < ROTATE:
        raise ValueError('PHASE must be in 0..19')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark('000300.XSHG')
    g.rank_study = dict(days=[], pending=[], tables={name: [] for name in TABLE_NAMES},
                        mature=0, signal_n=0, written=set())
    run_daily(on_rank_day, time='after_close')
    log.info('[运行环境] pandas=%s，numpy=%s。' % (pd.__version__, np.__version__))
    log.info('[排名诊断启动] 回测编辑器入口已启动；日期采用回测界面设置。'
             'L=%s，每%d交易日冻结，PHASE=%d。' % (LOOKBACKS, ROTATE, PHASE))
    log.info('[输出说明] 第一批完整结果在首个信号后的第81个交易日产生；'
             '之后逐批导出，结束时打印汇总。文件前缀：%s。' % OUTPUT_PREFIX)
    log.info('[结果位置] 日志看进度和对照表，投资研究文件区下载CSV。'
             '本实验不下单，回测收益曲线不展示排名预测力。')


def platform_frame(name, records):
    """Append one mature cohort; fail visibly if the platform rejects an export."""
    state = g.rank_study
    if not records:
        return  # A later nonempty batch must still write its CSV header.
    first = name not in state['written']
    write_file(OUTPUT_PREFIX + name + '.csv',
               pd.DataFrame(records).to_csv(index=False, header=first), append=not first)
    state['written'].add(name)


def on_rank_day(context):
    """Freeze pools at T close and evaluate only after all 80-day windows mature."""
    state = g.rank_study
    day = pd.Timestamp(context.current_dt).normalize()
    if state['days'] and state['days'][-1] == day:
        return
    state['days'].append(day)
    elapsed = len(state['days']) - 1
    data = JoinQuantData.__new__(JoinQuantData)
    data.api = InjectedAPI()
    if elapsed == 0:
        log.info('[实际回测起点] %s；建议区间%s至%s，日期由回测界面设置。' %
                 (day.date(), START, END))
        for name in TABLE_NAMES + ('summary',):
            write_file(OUTPUT_PREFIX + name + '.csv',
                       pd.DataFrame(columns=EMPTY_COLUMNS[name]).to_csv(index=False), append=False)
        write_file(OUTPUT_PREFIX + 'README.md',
                   '本次排名诊断正在运行。完整结果需要首个信号后81个交易日。\n'
                   '当前状态请看回测日志；明细CSV在首批成熟后开始写入。\n')
        write_file(OUTPUT_PREFIX + 'metadata.json', json.dumps(dict(
            status='running', first_day=str(day.date()), phase=PHASE), ensure_ascii=False))

    remaining = []
    for frozen in state['pending']:
        age = elapsed - frozen['position']
        if age < 81:
            remaining.append(frozen)
            continue
        # Every requested quote is now historical. Pool and T-close history remain frozen.
        future_dates = pd.DatetimeIndex(state['days'][frozen['position']:frozen['position'] + 82])
        quotes = data.prices(frozen['pool'], frozen['day'].date(), future_dates[-1].date())
        if frozen['pool']:
            close = pd.concat([frozen['close'], quotes['close'].loc[quotes['close'].index > frozen['day']]])
            opening = quotes['open']
        else:
            close, opening = frozen['close'], pd.DataFrame(index=future_dates)
        calendar = frozen['history_dates'].append(future_dates[1:])
        close = close.reindex(calendar)
        batch = collect_signal(calendar, len(frozen['history_dates']) - 1,
                               frozen['pool'], frozen['liquid_n'], close, opening, names=frozen['names'])
        for name in TABLE_NAMES:
            state['tables'][name].extend(batch[name])
            platform_frame(name, batch[name])
        state['mature'] += 1
        log.info('[排名诊断成熟] 信号%s，已完成%d批；%d个L × %d个区间，明细已导出。' %
                 (frozen['day'].date(), state['mature'], len(LOOKBACKS), len(WINDOWS)))
    state['pending'] = remaining

    if elapsed >= PHASE and (elapsed - PHASE) % ROTATE == 0:
        dates = pd.DatetimeIndex(pd.to_datetime(data.api.get_trade_days(end_date=day.date(), count=251)))
        if len(dates) < 251:
            log.info('[排名诊断跳过] %s，历史不足251个交易日。' % day.date())
            return
        pool, liquid_n, close = data.signal_history(day, dates)
        state['pending'].append(dict(day=day, position=elapsed, pool=pool, liquid_n=liquid_n,
                                     close=close, history_dates=dates, names=data.signal_names))
        state['signal_n'] += 1
        common_n = b.sum(close[c].tail(max(LOOKBACKS) + 1).notna().all()
                         and (close[c].tail(max(LOOKBACKS) + 1) > 0).all() for c in pool)
        log.info('[排名诊断信号] %s，第%d批，去重池%d，共同样本%d，成熟%d，等待%d。' %
                 (day.date(), state['signal_n'], len(pool), common_n,
                  state['mature'], len(state['pending'])))
        if common_n < MIN_IC:
            log.info('[样本不足] 本批共同样本%d，Rank IC至少需要%d只；'
                     '保留该信号和缺测值，不补零。' % (common_n, MIN_IC))


def on_strategy_end(context):
    state = g.rank_study
    start = str(state['days'][0].date()) if state['days'] else str(context.current_dt.date())
    end = str(state['days'][-1].date()) if state['days'] else start
    log.info('[排名诊断结束] 实际区间%s至%s，运行%d交易日，信号%d批，完整成熟%d批，未完成%d批。' %
             (start, end, len(state['days']), state['signal_n'], state['mature'], len(state['pending'])))
    def save(name, content):
        write_file(OUTPUT_PREFIX + name, content, append=False)
    if not state['mature']:
        message = ('没有完整成熟信号：首个信号后需再运行81个交易日，'
                   '即至少82个交易日且有251日建池历史。请延长回测日期。')
        log.info('[排名诊断未完成] ' + message)
        save('README.md', message + '\n')
        metadata = metadata_for(start, end, 0, PHASE, JoinQuantData.quote_policy)
        metadata.update(status='insufficient_history', signal_count=state['signal_n'],
                        pending_cohorts=len(state['pending']), reason=message)
        save('metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
        return
    summary = export_study(state['tables'], start, end, state['mature'], PHASE,
                           JoinQuantData.quote_policy, save, emit=lambda line: None)
    metadata = metadata_for(start, end, state['mature'], PHASE, JoinQuantData.quote_policy)
    metadata.update(status='complete', signal_count=state['signal_n'],
                    pending_cohorts=len(state['pending']))
    save('metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
    log.info('[排名预测力对照] IC依次为0—20 / 20—40 / 40—60 / 60—80独立区间；'
             '更新IC看20—40；换名单优势单位为百分点。')
    base = summary[summary.scope == 'all']
    for length in LOOKBACKS:
        piece = base[base.lookback == length]
        ic = [piece[(piece.metric == 'ic') & (piece['first'] == first) &
                    (piece['last'] == last)]['mean'].iloc[0]
              for first, last in ((0, 20), (20, 40), (40, 60), (60, 80))]
        updated = piece[piece.metric == 'fresh_ic']['mean'].iloc[0]
        gain = piece[piece.metric == 'ic_gain']['mean'].iloc[0]
        advantage = piece[piece.metric == 'switch_advantage']['mean'].iloc[0] * 100
        log.info('L=%d | IC=%s | 更新IC=%.4f | IC增量=%.4f | 换名单优势=%.4f pp' %
                 (length, ' / '.join('%.4f' % value for value in ic), updated, gain, advantage))
    log.info('[导出完成] 聚宽投资研究文件区：%ssummary.csv、periods.csv、updates.csv、'
             'snapshots.csv、returns.csv、audit.csv、README.md、metadata.json。' % OUTPUT_PREFIX)


def plot(summary, output):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        # Plotting is optional; numerical exports remain the complete experiment.
        return
    base = summary[(summary.scope == 'all') & (summary.metric == 'ic')]
    pairs = ((0, 5), (5, 10), (10, 20), (20, 40), (40, 60), (60, 80))
    matrix = np.array([[base[(base.lookback == length) & (base['first'] == first)
                            & (base['last'] == last)]['mean'].iloc[0]
                        for first, last in pairs] for length in LOOKBACKS])
    fig, ax = plt.subplots(figsize=(9, 5))
    limit = max(0.05, float(np.nanmax(np.abs(matrix)))) if np.isfinite(matrix).any() else 0.05
    chart = ax.imshow(matrix, cmap='RdBu_r', vmin=-limit, vmax=limit)
    ax.set(xticks=range(len(pairs)), xticklabels=['%d-%d' % p for p in pairs],
           yticks=range(len(LOOKBACKS)), yticklabels=LOOKBACKS,
           xlabel='Future trading-day segment (frozen initial ranks)', ylabel='Lookback L',
           title='Mean Rank IC')
    for i in range(len(LOOKBACKS)):
        for j in range(len(pairs)):
            ax.text(j, i, '%.3f' % matrix[i, j], ha='center', va='center', fontsize=9)
    fig.colorbar(chart, ax=ax, label='Rank IC')
    fig.tight_layout()
    fig.savefig(output / 'forecast_heatmap.png', dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default=START)
    parser.add_argument('--end', default=END)
    parser.add_argument('--phase', type=int, default=0, help='20-day signal grid offset, 0..19')
    parser.add_argument('--output')
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    data = JoinQuantData(args.start, args.end)
    label = 'joinquant_v2'
    if args.phase:
        label += '_phase%d' % args.phase
    output = args.output or str(here / 'results' / label)
    run_study(data, args.start, args.end, output, args.phase)


if __name__ == '__main__' and 'run_daily' not in globals():
    main()
