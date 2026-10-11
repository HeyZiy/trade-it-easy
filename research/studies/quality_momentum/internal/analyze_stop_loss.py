"""只读两版聚宽导出与本地原始/HFQ缓存，生成止损配对与价格路径诊断。

运行：python research/studies/quality_momentum/internal/analyze_stop_loss.py
价格路径是事后收盘近似，不模拟新规则、不推算补仓反事实净值。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
CACHE = ROOT / 'research/studies/roe_quality/_cache/pool'
OUT = HERE / 'reports/stop_loss_2026-10-10'
KINDS = ('nav', 'orders', 'holding_periods', 'decisions', 'events', 'signals', 'members')
HORIZONS = (5, 20, 60)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def bool_column(series):
    values = series.astype(str).str.lower()
    require(values.isin(['true', 'false']).all(), '布尔列格式错误')
    return values.eq('true')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_version(version, suffix):
    frames, hashes = {}, {}
    exports = HERE / 'exports'
    for kind in KINDS:
        path = exports / f'pool_rotation_{version}_{kind}{suffix}.csv'
        frame = pd.read_csv(path, encoding='utf-8-sig')
        require(not frame.empty, f'{version}/{kind}为空')
        for field in ('date', 'entry_date', 'exit_date', 'signal_day', 'asof', 'time',
                      'stop_trigger_time'):
            if field in frame:
                frame[field] = pd.to_datetime(frame[field], errors='raise')
        frames[kind] = frame
        hashes[path.name] = sha(path)
    n, o, h = (frames[k] for k in ('nav', 'orders', 'holding_periods'))
    require(n.date.is_unique and n.date.is_monotonic_increasing, '净值日期不唯一/乱序')
    require(o.id.is_unique and not o.duplicated(['date', 'code']).any(), '订单重复')
    require(o.date.isin(n.date).all(), '订单日期不在净值日历')
    o['is_buy'] = bool_column(o.is_buy)
    require((o.filled > 0).all() and (o.price > 0).all(), '存在未成交/无效价格订单')
    require(np.isfinite(o[['filled', 'price', 'commission']]).all().all(), '成交/费用缺测')
    o['notional'] = o.filled * o.price
    tax = np.where(o.date >= pd.Timestamp('2023-08-28'), .0005, .001)
    expected = np.maximum(o.notional * .00025, 5) + np.where(o.is_buy, 0, tax * o.notional)
    require(np.allclose(o.commission, expected, atol=.011, rtol=0), '平台commission费用不符')
    turnover = o.groupby('date').notional.sum().reindex(n.date, fill_value=0).to_numpy()
    require(np.allclose(turnover / n.equity, n.turnover, atol=1e-8, rtol=0), '换手成交额未闭合')
    require(h.entry_source.eq('filled_order').all() and h.closure_source.eq('filled_order').all(),
            '持有期缺少实际成交归属')
    require(not h.duplicated(['code', 'entry_date']).any(), '持有期开仓键重复')
    require((h.exit_date > h.entry_date).all(), '实际持仓日期无效')
    o['cash_flow'] = np.where(o.is_buy, -o.notional, o.notional) - o.commission
    changes = n.cash.diff().fillna(n.cash.iloc[0] - n.equity.iloc[0]).to_numpy()
    n['non_trade_cash_flow'] = changes - o.groupby('date').cash_flow.sum().reindex(
        n.date, fill_value=0).to_numpy()
    n['cash_pct'] = n.cash / n.equity
    n['return'] = n.equity.pct_change().fillna(0)
    n['normalized_equity'] = n.equity / n.equity.iloc[0]
    n['drawdown'] = n.equity / n.equity.cummax() - 1
    return frames, hashes


def episodes(frames, version):
    """按平台确认的开平仓日期分配订单，避免将送转数量猜成额外交易。"""
    n, o, h = (frames[k] for k in ('nav', 'orders', 'holding_periods'))
    calendar = pd.DatetimeIndex(n.date)
    used, rows = set(), []
    for record in h.to_dict('records'):
        orders = o[o.code.eq(record['code']) & o.date.between(record['entry_date'], record['exit_date'])]
        require(not orders.empty, '持有期没有订单')
        require(orders.iloc[0].is_buy and not orders.iloc[-1].is_buy, '持仓首尾交易方向错误')
        require(orders.date.iloc[0] == record['entry_date'] and orders.date.iloc[-1] == record['exit_date'],
                '持仓首尾日期与成交不符')
        require(not used.intersection(orders.index), '订单被多个持有期重复分配')
        used.update(orders.index)
        days = calendar.get_loc(record['exit_date']) - calendar.get_loc(record['entry_date'])
        require(days == record['holding_days'], '平台持有天数与日历不符')
        record.update(version=version, is_closed=True)
        rows.append(episode_amounts(record, orders))
    for code, orders in o.loc[~o.index.isin(used)].groupby('code', sort=False):
        require(orders.iloc[0].is_buy, '未分配交易不是期末持仓')
        record = dict(version=version, code=code, entry_date=orders.date.iloc[0],
                      exit_date=pd.NaT, holding_days=len(calendar) - 1 - calendar.get_loc(orders.date.iloc[0]),
                      exit_reason='open_at_end', is_closed=False)
        used.update(orders.index)
        rows.append(episode_amounts(record, orders))
    require(len(used) == len(o), '成交数量未闭合')
    result = pd.DataFrame(rows)
    require(not result.duplicated(['code', 'entry_date']).any(), '重建开仓键重复')
    roster = np.zeros(len(n), dtype=int)
    for row in result.itertuples():
        begin = calendar.get_loc(row.entry_date)
        end = calendar.get_loc(row.exit_date) if row.is_closed else len(calendar)
        roster[begin:end] += 1
    require(np.array_equal(roster, n.holdings.to_numpy()), '持仓轮次与每日实际持仓数不符')
    require(np.isclose(result.buy_amount.sum(), o.loc[o.is_buy, 'notional'].sum()), '买入额未闭合')
    require(np.isclose(result.sell_amount.sum(), o.loc[~o.is_buy, 'notional'].sum()), '卖出额未闭合')
    require(np.isclose(result.fees.sum(), o.commission.sum()), '费用未闭合')
    return result


def episode_amounts(record, orders):
    buys, sells = orders[orders.is_buy], orders[~orders.is_buy]
    record.update(buy_amount=buys.notional.sum(), sell_amount=sells.notional.sum(),
                  fees=orders.commission.sum(), order_count=len(orders),
                  first_fill_price=buys.iloc[0].price,
                  exit_fill_price=sells.iloc[-1].price if record['is_closed'] else np.nan,
                  trade_cash_difference=sells.notional.sum() - buys.notional.sum() - orders.commission.sum(),
                  buy_shares=buys.filled.sum(), sell_shares=sells.filled.sum())
    record['trade_cash_return'] = record['trade_cash_difference'] / record['buy_amount']
    return record


class Prices:
    def __init__(self, calendar):
        self.calendar = calendar
        self.data, self.hashes = {}, {}
        index_path = CACHE / 'idx_000906.pkl'
        index = pd.read_pickle(index_path)
        self.history_calendar = pd.DatetimeIndex(pd.to_datetime(index.index)).normalize()
        self.history_calendar = self.history_calendar[self.history_calendar <= calendar[-1]]
        require(self.history_calendar.is_unique and self.history_calendar.is_monotonic_increasing,
                '历史行情日历无效')
        require(calendar.isin(self.history_calendar).all(), '历史日历缺少账户交易日')
        self.hashes[index_path.name] = sha(index_path)

    def get(self, code):
        if code not in self.data:
            series = []
            for prefix in ('bars', 'bars_hfq'):
                path = CACHE / f'{prefix}_{code[:6]}.pkl'
                if not path.exists():
                    self.data[code] = None
                    return None
                frame = pd.read_pickle(path)
                require(list(frame.columns) == ['date', 'close'], '行情结构错误')
                dates = pd.DatetimeIndex(pd.to_datetime(frame.date)).normalize()
                require(dates.is_unique and dates.is_monotonic_increasing, '行情日期重复/乱序')
                close = pd.Series(pd.to_numeric(frame.close, errors='raise').to_numpy(), index=dates)
                require(np.isfinite(close).all() and close.gt(0).all(), '行情含无效值')
                series.append(close.reindex(self.history_calendar))
                self.hashes[path.name] = sha(path)
            self.data[code] = tuple(series)
        return self.data[code]

    def path(self, episode):
        pair = self.get(episode['code'])
        if pair is None:
            return {'path_status': 'missing_cache'}
        raw_full, adjusted_full = pair
        raw, adjusted = raw_full.reindex(self.calendar), adjusted_full.reindex(self.calendar)
        entry, exit = episode['entry_date'], episode['exit_date']
        if pd.isna(exit):
            return {'path_status': 'open_at_end'}
        begin, end = self.calendar.get_loc(entry), self.calendar.get_loc(exit)
        endpoints = [raw.loc[entry], adjusted.loc[entry], raw.loc[exit], adjusted.loc[exit]]
        if not np.isfinite(endpoints).all():
            return {'path_status': 'missing_endpoint'}
        buy = episode['first_fill_price'] * adjusted.loc[entry] / raw.loc[entry]
        sell = episode['exit_fill_price'] * adjusted.loc[exit] / raw.loc[exit]
        path = adjusted.iloc[begin:end + 1] / buy - 1
        complete = not path.isna().any()
        prior = adjusted_full[adjusted_full.index < entry].tail(61)
        fields = dict(path_status='ok' if complete else 'missing_holding_close',
                      missing_closes=int(path.isna().sum()),
                      mae_close=min(0, float(path.min())) if complete else np.nan,
                      mfe_close=max(0, float(path.max())) if complete else np.nan,
                      approx_exit_return=sell / buy - 1,
                      first_loss10_day=np.nan, same_day_close_from_exit=adjusted.loc[exit] / sell - 1,
                      prior20_return=np.nan, prior_above_ma20=np.nan, prior_below_high60=np.nan)
        hits = np.flatnonzero(path.to_numpy() <= -.10)
        if complete and len(hits):
            fields['first_loss10_day'] = int(hits[0])
        if len(prior) >= 21 and prior.iloc[-21:].notna().all():
            fields['prior20_return'] = prior.iloc[-1] / prior.iloc[-21] - 1
            fields['prior_above_ma20'] = prior.iloc[-1] / prior.iloc[-20:].mean() - 1
        if len(prior) >= 60 and prior.iloc[-60:].notna().all():
            fields['prior_below_high60'] = prior.iloc[-1] / prior.iloc[-60:].max() - 1
        for horizon in HORIZONS:
            key = f'post{horizon}'
            if end + horizon >= len(self.calendar):
                fields[key + '_status'] = 'end_of_backtest'
                continue
            future = adjusted.iloc[end:end + horizon + 1]
            if future.isna().any():
                fields[key + '_status'] = 'missing_close'
                continue
            fields.update({key + '_status': 'ok', key + '_end': self.calendar[end + horizon],
                           key + '_from_close': future.iloc[-1] / future.iloc[0] - 1,
                           key + '_from_exit': future.iloc[-1] / sell - 1,
                           key + '_min_from_exit': future.min() / sell - 1,
                           key + '_max_from_exit': future.max() / sell - 1})
        return fields


def names():
    old = ROOT / 'research/studies/roe_quality/pool_rotation_v1_trades.csv'
    frame = pd.read_csv(old, encoding='gb18030', usecols=['标的'])
    values = frame['标的'].str.extract(r'^(.*)\(([^)]+)\)$').dropna()
    return dict(zip(values[1], values[0]))


def performance(frames, version):
    n = frames['nav']
    benchmark = n.benchmark_close / n.benchmark_close.iloc[0]
    relative = n.normalized_equity / benchmark
    return dict(version=version, initial_equity=n.equity.iloc[0], end_equity=n.equity.iloc[-1],
                cumulative_return=n.normalized_equity.iloc[-1] - 1,
                max_drawdown=n.drawdown.min(), relative_max_drawdown=(relative / relative.cummax() - 1).min(),
                average_cash_pct=n.cash_pct.mean(), average_holdings=n.holdings.mean(),
                fees=frames['orders'].commission.sum(),
                non_trade_cash_flow=n.non_trade_cash_flow.sum(),
                non_trade_cash_flow_days=int((n.non_trade_cash_flow.abs() > .011).sum()),
                closed_periods=len(frames['holding_periods']),
                stop_periods=int(frames['holding_periods'].exit_reason.eq('stop_loss').sum()))


def markdown_table(frame, labels, percentages=(), money=()):
    counts = {'horizon', 'observable', 'missing_or_censored', 'positive_count',
              'above10_count', 'below_minus10_count', 'positive_from_close_count',
              'episodes', 'prior20_observable', 'mfe_observable', 'closed_periods',
              'year', 'execution_delay_days'}
    rows = ['| ' + ' | '.join(labels.values()) + ' |', '| ' + ' | '.join(['---'] * len(labels)) + ' |']
    for _, record in frame.iterrows():
        cells = []
        for key in labels:
            value = record.get(key, np.nan)
            if pd.isna(value):
                text = '缺测'
            elif key in percentages:
                text = f'{value:.2%}'
            elif key in money:
                text = f'{value:,.2f}'
            elif key in counts:
                text = str(int(value))
            elif isinstance(value, pd.Timestamp):
                text = value.strftime('%Y-%m-%d')
            elif isinstance(value, (float, np.floating)):
                text = f'{value:.2f}'
            else:
                text = str(value)
            cells.append(text.replace('|', '\\|'))
        rows.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(rows)


def main():
    baseline, hashes_a = read_version('v4_1', ' (1)')
    stopped, hashes_b = read_version('v4_1_1', '')
    a, b = baseline['nav'], stopped['nav']
    require(a.date.equals(b.date), '两版净值日历不同')
    require(a.equity.iloc[0] == b.equity.iloc[0], '两版初始资金不同')
    require(np.allclose(a.benchmark_close, b.benchmark_close), '两版基准不同')
    for field in ('signal_day', 'asof', 'pool_size', 'ranked_size'):
        require(baseline['signals'][field].equals(stopped['signals'][field]), f'信号{field}不同')
    keys = ['signal_day', 'code', 'rank', 'momentum', 'roe_ann', 'profit_yoy', 'pe', 'pb', 'vol60']
    pd.testing.assert_frame_equal(baseline['members'][keys], stopped['members'][keys])
    calendar = pd.DatetimeIndex(a.date)
    prices = Prices(calendar)
    name_map = names()
    episodes_a, episodes_b = episodes(baseline, 'v4_1'), episodes(stopped, 'v4_1_1')
    for table in (episodes_a, episodes_b):
        details = pd.DataFrame([prices.path(row) for row in table.to_dict('records')])
        for key in details:
            table[key] = details[key]
        table['name'] = table.code.map(name_map).fillna(table.code)
    stops = episodes_b[episodes_b.exit_reason.eq('stop_loss')].copy()
    require(len(stops) == int(stopped['events'].kind.eq('STOP_LOSS').sum()), '触发与清仓轮数不闭合')
    stops['trigger_day'] = stops.stop_trigger_time.dt.normalize()
    stops['execution_delay_days'] = [calendar.get_loc(x) - calendar.get_loc(y)
                                     for x, y in zip(stops.exit_date, stops.trigger_day)]
    reentry, waiting = [], []
    eligibility = stopped['events'][stopped['events'].kind.eq('STOP_REENTRY_ELIGIBLE')]
    for row in stops.itertuples():
        later = episodes_b[episodes_b.code.eq(row.code) & episodes_b.entry_date.gt(row.exit_date)]
        reentry.append(calendar.get_loc(later.entry_date.min()) - calendar.get_loc(row.exit_date)
                       if not later.empty else np.nan)
        eligible = eligibility[eligibility.code.eq(row.code) & eligibility.time.ge(row.exit_date)]
        if eligible.empty:
            waiting.append(np.nan)
        else:
            day = eligible.time.min().normalize()
            waiting.append(calendar.get_loc(day) + 1 - calendar.get_loc(row.exit_date)
                           if calendar.get_loc(day) + 1 < len(calendar) else np.nan)
    stops['actual_reentry_days'], stops['earliest_reentry_days'] = reentry, waiting
    baseline_cols = ['code', 'entry_date', 'exit_date', 'is_closed', 'holding_days',
                     'approx_exit_return', 'trade_cash_return', 'buy_amount', 'exit_fill_price', 'path_status']
    paired = stops.merge(episodes_a[baseline_cols], on=['code', 'entry_date'], how='left',
                         suffixes=('', '_baseline'), validate='one_to_one', indicator=True)
    paired['matched_same_entry'] = paired['_merge'].eq('both')
    paired['baseline_minus_stop_price_return'] = paired.approx_exit_return_baseline - paired.approx_exit_return
    paired['baseline_minus_stop_cash_return'] = paired.trade_cash_return_baseline - paired.trade_cash_return
    paired = paired.drop(columns='_merge')
    horizon_rows = []
    for horizon in HORIZONS:
        valid = stops[stops[f'post{horizon}_status'].eq('ok')]
        horizon_rows.append(dict(horizon=horizon, observable=len(valid),
            missing_or_censored=len(stops) - len(valid),
            mean_from_exit=valid[f'post{horizon}_from_exit'].mean(),
            median_from_exit=valid[f'post{horizon}_from_exit'].median(),
            positive_count=int(valid[f'post{horizon}_from_exit'].gt(0).sum()),
            median_from_close=valid[f'post{horizon}_from_close'].median(),
            positive_from_close_count=int(valid[f'post{horizon}_from_close'].gt(0).sum()),
            above10_count=int(valid[f'post{horizon}_from_exit'].ge(.1).sum()),
            below_minus10_count=int(valid[f'post{horizon}_from_exit'].le(-.1).sum())))
    horizon_table = pd.DataFrame(horizon_rows)
    annual = []
    for year, indices in a.groupby(a.date.dt.year).groups.items():
        na, nb = a.loc[indices], b.loc[indices]
        ra, rb = np.prod(1 + na['return']) - 1, np.prod(1 + nb['return']) - 1
        annual.append(dict(year=year, days=len(indices), baseline_return=ra, stop_return=rb,
                           difference=rb - ra, baseline_cash=na.cash_pct.mean(), stop_cash=nb.cash_pct.mean()))
    annual = pd.DataFrame(annual)
    valid_pairs = paired[paired.matched_same_entry & paired.approx_exit_return.notna()
                         & paired.approx_exit_return_baseline.notna()].copy()
    severe = episodes_a[episodes_a.is_closed & episodes_a.approx_exit_return.notna()].copy()
    severe['group'] = np.where(severe.approx_exit_return <= -.15, '首次成交至退出跌幅>=15%', '其他闭合持仓')
    entry_summary = severe.groupby('group').agg(episodes=('code', 'size'),
        median_prior20=('prior20_return', 'median'),
        median_ma20_gap=('prior_above_ma20', 'median'),
        negative_prior20_share=('prior20_return', lambda s: s.dropna().lt(0).mean()),
        below_ma20_share=('prior_above_ma20', lambda s: s.dropna().lt(0).mean()),
        median_mfe=('mfe_close', 'median'), median_holding_days=('holding_days', 'median')).reset_index()
    entry_summary['prior20_observable'] = severe.groupby('group').prior20_return.count().reindex(
        entry_summary.group).to_numpy()
    entry_summary['mfe_observable'] = severe.groupby('group').mfe_close.count().reindex(
        entry_summary.group).to_numpy()
    metrics = pd.DataFrame([performance(baseline, 'v4_1'), performance(stopped, 'v4_1_1')])
    summary = dict(start=str(a.date.iloc[0].date()), end=str(a.date.iloc[-1].date()),
        initial_equity=float(a.equity.iloc[0]), days=len(a), input_rows={
            v: {k: len(f) for k, f in frames.items()}
            for v, frames in [('v4_1', baseline), ('v4_1_1', stopped)]},
        matched_pool_and_ranks=True, roster_matches_actual=True,
        stop_periods=len(stops), stop_path_status=stops.path_status.value_counts().to_dict(),
        same_entry_matched=int(paired.matched_same_entry.sum()), valid_closed_pairs=len(valid_pairs),
        baseline_better_pairs=int(valid_pairs.baseline_minus_stop_price_return.gt(0).sum()),
        stop_better_pairs=int(valid_pairs.baseline_minus_stop_price_return.lt(0).sum()),
        equal_pairs=int(valid_pairs.baseline_minus_stop_price_return.abs().le(1e-10).sum()),
        median_baseline_minus_stop_price_return=float(valid_pairs.baseline_minus_stop_price_return.median()),
        mean_baseline_minus_stop_price_return=float(valid_pairs.baseline_minus_stop_price_return.mean()),
        median_stop_holding_days=float(stops.holding_days.median()),
        stops_within10_days=int(stops.holding_days.le(10).sum()),
        median_stop_trigger_return=float(stops.stop_return.median()),
        stop_mfe_observable=int(stops.mfe_close.notna().sum()),
        stop_mfe_not_above5=int(stops.mfe_close.le(.05).sum()),
        stop_delay_counts={str(k): int(v) for k, v in stops.execution_delay_days.value_counts().items()},
        median_earliest_reentry_days=float(stops.earliest_reentry_days.median()),
        median_actual_reentry_days=float(stops.actual_reentry_days.median()),
        later_reentry_count=int(stops.actual_reentry_days.notna().sum()),
        mean_extra_cash_pct=float(b.cash_pct.mean() - a.cash_pct.mean()),
        final_equity_gap=float(b.equity.iloc[-1] - a.equity.iloc[-1]),
        sha256={**hashes_a, **hashes_b}, cache_sha256=prices.hashes,
        methods={'commission': '平台commission已含卖出印花税，不重复加税',
                 'cash_difference': '卖出额-买入额-commission，不含未逐股导出的现金分红，非完整已实现盈亏',
                 'paths': '实际成交价按当日HFQ/原始收盘比换尺度，收盘路径为近似；缺测不填充',
                 'post_sale': '未来收盘相对换尺度实际退出成交价，含卖出当日剩余价格变动；所有窗口止于回测末日',
                 'pairing': '同代码同实际买入日期；不同资金规模的现金损益不直接相加归因账户差额'})
    OUT.mkdir(parents=True, exist_ok=True)
    tables = {'performance': metrics, 'annual': annual, 'stop_cases': paired,
              'post_sale_summary': horizon_table, 'entry_state_summary': entry_summary,
              'baseline_episodes': episodes_a, 'stop_episodes': episodes_b}
    for key, frame in tables.items():
        frame.to_csv(OUT / f'{key}.csv', index=False, encoding='utf-8-sig')
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    write_report(summary, tables, baseline, stopped)
    verify_hashes(summary)
    print(json.dumps({k: v for k, v in summary.items() if k not in ('sha256', 'cache_sha256')}, ensure_ascii=True))
    print(horizon_table.to_string(index=False))
    print(valid_pairs.nlargest(10, 'baseline_minus_stop_price_return')[
        ['code', 'entry_date', 'exit_date', 'exit_date_baseline', 'approx_exit_return',
         'approx_exit_return_baseline', 'baseline_minus_stop_price_return']].to_string(index=False))


def verify_hashes(summary):
    require(all(sha(HERE / name) == value for name, value in summary['sha256'].items()), '原始CSV发生变化')
    require(all(sha(CACHE / name) == value for name, value in summary['cache_sha256'].items()), '行情缓存发生变化')


def write_report(summary, tables, baseline, stopped):
    m, annual, cases, horizons, entries = [tables[k] for k in
        ('performance', 'annual', 'stop_cases', 'post_sale_summary', 'entry_state_summary')]
    pairs = cases[cases.matched_same_entry & cases.approx_exit_return.notna()
                  & cases.approx_exit_return_baseline.notna()]
    losers = pairs.nlargest(12, 'baseline_minus_stop_price_return')
    winners = pairs.nsmallest(8, 'baseline_minus_stop_price_return')
    delayed = cases[cases.execution_delay_days.gt(0)]
    lines = [
        '# 4.1与4.1.1：10%清仓止损诊断（2026-10-10）', '',
        '**结论：当前结果不支持将10%开盘直接清仓作为默认退出。最强线索是卖后反弹与现金等待，'
        '同时止损也保护了一部分继续下跌的持仓。先研究退出确认，近期弱势入场过滤暂缺支持。**', '',
        f'区间{summary["start"]}至{summary["end"]}，{summary["days"]}个交易日。两版实际初始资金均为'
        f'{summary["initial_equity"]:,.0f}元；不是历史脚本说明中的100万元。', '',
        '本报告复核已发生的聚宽交易并分析事后价格路径；没有生成新规则反事实回测。', '',
        '## 账户表现与数据核验', '',
        markdown_table(m, {'version':'版本', 'end_equity':'期末权益/元', 'cumulative_return':'累计收益',
            'max_drawdown':'最大回撤', 'relative_max_drawdown':'相对最大回撤',
            'average_cash_pct':'平均现金', 'average_holdings':'平均持仓', 'closed_periods':'闭合轮次'},
            ('cumulative_return','max_drawdown','relative_max_drawdown','average_cash_pct'), ('end_equity',)), '',
        f'止损版期末权益比基线少{-summary["final_equity_gap"]:,.2f}元，平均现金增加'
        f'{summary["mean_extra_cash_pct"] * 100:.2f}个百分点。两版130个信号、12,383条池成员记录的财务、名次和动量完全一致；'
        '差异来自风险退出及随之演化的留存、现金和持仓。', '',
        '成交额与每日换手闭合；所有订单均归属一个已闭合或期末未闭合持仓；'
        '按开平仓日期重建的每日持仓数与平台记录逐日一致；无整批数据暂停。', '',
        '平台commission已含卖出印花税，不能重复扣税。净值现金变化中基线有'
        f'{m.iloc[0].non_trade_cash_flow:,.2f}元、止损版有{m.iloc[1].non_trade_cash_flow:,.2f}元'
        '非交易现金流（导出未逐股说明来源）。因此交易净现金差不代表含分红的完整持仓收益，'
        '也不能把配对现金差直接加总为账户差额。', '',
        '## 止损发生在什么阶段', '',
        f'触发与实际清仓均为{summary["stop_periods"]}轮；持有天数中位数'
        f'{summary["median_stop_holding_days"]:.0f}天，其中{summary["stops_within10_days"]}轮在10天内清仓。'
        f'首次触发亏损中位数{summary["median_stop_trigger_return"]:.2%}。', '',
        f'完整收盘路径有{summary["stop_mfe_observable"]}轮，其中'
        f'{summary["stop_mfe_not_above5"]}轮持有期最高收盘浮盈不超过5%；多数止损仓在入场后没有明显赚过钱。', '',
        f'最早允许重新买入的等待中位数为{summary["median_earliest_reentry_days"]:.0f}交易日，'
        f'后来实际重新买入{summary["later_reentry_count"]}轮，实际再入间隔中位数'
        f'{summary["median_actual_reentry_days"]:.0f}交易日；后者也包含重新满足入选条件所需时间。', '',
        '## 卖出后价格路径', '',
        markdown_table(horizons, {'horizon':'卖后交易日', 'observable':'可观察轮次',
            'missing_or_censored':'缺测/末端不足', 'mean_from_exit':'平均涨跌', 'median_from_exit':'中位涨跌',
            'positive_count':'上涨轮次', 'above10_count':'涨>=10%', 'below_minus10_count':'跌<=-10%'},
            ('mean_from_exit','median_from_exit')), '',
        '以上为本地原始/HFQ收盘缓存，将实际退出成交价按退出日调整因子换尺度；'
        '包含卖出当日剩余价格变动。它描述卖后路径，不保证停牌/涨跌停时可成交，'
        '也不等于账户可追回收益。没有跨过2026-09-01，没有填补缺失行情。', '',
        '改以卖出日收盘为起点，排除当日开盘至收盘变化后的稳健性检查：', '',
        markdown_table(horizons, {'horizon':'卖后交易日','observable':'轮次',
            'median_from_close':'从卖出日收盘计算的中位涨跌',
            'positive_from_close_count':'从卖出日收盘计算的上涨轮次'}, ('median_from_close',)), '',
        '## 同买入日配对', '',
        f'{summary["same_entry_matched"]}轮能匹配基线同代码、同买入日；'
        f'其中{summary["valid_closed_pairs"]}轮两版均已闭合且入场/退出端点有效。'
        f'按首次买入到最终退出的复权价格近似，基线更好{summary["baseline_better_pairs"]}轮、'
        f'止损版更好{summary["stop_better_pairs"]}轮；基线减止损的价格收益差中位数'
        f'{summary["median_baseline_minus_stop_price_return"] * 100:.2f}个百分点。', '',
        '配对不要求之后的追加买入与减仓相同，因此这是相同入场的退出路径比较，'
        '不是冻结同一仓位的严格因果收益归因。未匹配案例仍保留在stop_cases.csv。', '',
        '### 基线相对更好的重点案例', '',
        case_table(losers), '', '### 止损相对更好的重点案例', '', case_table(winners), '',
        '## 开盘止损与卖出受阻', '',
        '执行延迟按触发日至实际清仓日的交易日数统计：'
        + '；'.join(f'{k}日={v}轮' for k,v in summary['stop_delay_counts'].items()) + '。', '',
        markdown_table(delayed, {'code':'代码','name':'名称','stop_trigger_time':'触发时间',
            'exit_date':'实际清仓','execution_delay_days':'延迟交易日','stop_return':'触发亏损',
            'approx_exit_return':'首次买入至退出价格涨跌'}, ('stop_return','approx_exit_return')), '',
        '全量SELL_BLOCKED事件仍在原始events中；不能将触发亏损当作实际成交亏损。'
        '本次订单CSV只记录成交日期，事件表记录09:31提交。现有日频数据不能验证09:31/10:00/尾盘的价格优劣。', '',
        '## 入场前近期状态：先看是否能区分大亏仓', '',
        markdown_table(entries, {'group':'基线价格路径分组','episodes':'轮次','prior20_observable':'前20日可观察',
            'median_prior20':'前20日收益中位数','negative_prior20_share':'前20日下跌占比',
            'below_ma20_share':'入场前低于MA20占比','median_mfe':'持有期最高收盘浮盈中位数',
            'mfe_observable':'完整极值路径轮次'},
            ('median_prior20','negative_prior20_share','below_ma20_share','median_mfe')), '',
        '分组依据首次买入到实际退出的调整后价格跌幅，含再平衡的完整持仓盈亏并未用于分组。'
        '入场前指标仅使用买入日前一交易日及更早收盘；事后分组只能诊断，不能直接证明新门槛有效。', '',
        '实际买入日前一日收盘在09:31已可见；若将条件加到原14:55信号，必须退至信号asof，'
        '不能使用信号日尚未完成的收盘。完整持有路径缺测的轮次不计算MAE/MFE，但端点有效时仍可比较退出与卖后窗口。', '',
        '## 年份与收益差', '',
        markdown_table(annual, {'year':'年份','baseline_return':'4.1收益','stop_return':'4.1.1收益',
            'difference':'止损减基线','baseline_cash':'4.1平均现金','stop_cash':'止损平均现金'},
            ('baseline_return','stop_return','difference','baseline_cash','stop_cash')), '',
        '年度收益使用前一交易日权益衔接，2016与2026为实际回测覆盖的部分年度。', '',
        '## 下一项正式实验', '',
        '先根据卖后路径、配对退出和入场前分组，选一个问题做同口径聚宽对照。'
        '若近期状态无法区分大亏仓，避免直接加MA20或前20日收益硬门槛；'
        '若卖后反弹较多，研究退出确认或补仓等待；二者必须分别回测。'
        '交易钟点实验需要统一分钟频率，调仓日期平移实验保持20日间隔。', '',
        '本轮优先实验：保留10%阈值，将退出触发改为收盘确认、下一交易日09:31提交清仓；'
        '选股、轮动、补仓等待、费用与资金保持相同，同时与4.1和4.1.1对照。'
        '这项实验验证退出确认时点；是否改善不能由本报告的事后价格路径直接推出。', '',
        '## 文件与复算', '',
        '- stop_cases.csv：全部止损案例、配对基线、卖后窗口与重新入选等待。',
        '- baseline_episodes.csv / stop_episodes.csv：每个持仓轮次的订单现金差与近似价格路径。',
        '- performance.csv / annual.csv / post_sale_summary.csv / entry_state_summary.csv：汇总。',
        '- summary.json：源CSV、行情缓存SHA256、方法限制与数据校验。',
        '原始CSV和缓存保持原样。复算入口：../internal/analyze_stop_loss.py。', '',
    ]
    (OUT / 'report.md').write_text('\n'.join(lines), encoding='utf-8')


def case_table(frame):
    return markdown_table(frame, {'name':'名称','code':'代码','entry_date':'共同买入',
        'exit_date':'止损清仓','exit_date_baseline':'基线清仓','approx_exit_return':'止损价格收益',
        'approx_exit_return_baseline':'基线价格收益','baseline_minus_stop_price_return':'基线减止损'},
        ('approx_exit_return','approx_exit_return_baseline','baseline_minus_stop_price_return'))


if __name__ == '__main__':
    main()
