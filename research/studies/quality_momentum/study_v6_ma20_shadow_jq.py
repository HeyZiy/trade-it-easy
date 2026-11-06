# -*- coding: utf-8 -*-
"""聚宽投资研究单文件。沿多空线组的实际持仓做 MA20 影子退出。

把上轮的 quality_momentum_v6_ma_exit_{summary,positions,holding_periods}.csv
放进 Notebook 当前目录，再把本文件整份粘贴到一个 Python 3 单元格运行。
导出 quality_momentum_v6_ma20_shadow_{trades,summary}.csv。

买入后连续三个收盘严格低于各自当天 MA20，次日开盘尝试卖出。
停牌或开盘跌停则延后，按最新三个收盘复核，信号解除就撤销。
在实际多空线卖出日或回测截止日停止寻找影子成交。ST 等其他退出单列。
价格比较统一用前复权开盘，未模拟滑点、费用、资金再投入或替换股票。
"""
from pathlib import Path

import numpy as np
import pandas as pd


INPUT_PREFIX = 'quality_momentum_v6_ma_exit'
OUTPUT_PREFIX = 'quality_momentum_v6_ma20_shadow'
ARM = 'blend'
MA_DAYS = 20
CONFIRM_DAYS = 3
FORWARD_DAYS = 20
REBOUND_PCT = 5.0
LIMIT_PAD = 0.001


def _read_arm(folder, name, columns):
    path = folder / ('%s_%s.csv' % (INPUT_PREFIX, name))
    if not path.is_file():
        raise FileNotFoundError('请把上轮聚宽导出的 %s 放进当前研究目录' % path.name)
    rows = pd.read_csv(path, usecols=['account'] + columns)
    return rows.loc[rows['account'].eq(ARM), columns].copy()


def _dates(values):
    return pd.DatetimeIndex(pd.to_datetime(values)).normalize()


def _holding_spans(positions, closed, calendar):
    """按实际持仓重建连续持有区间，并与平仓流水逐笔核对。"""
    positions = positions.copy()
    positions['date'] = _dates(positions['date'])
    positions['session'] = calendar.get_indexer(positions['date'])
    if (positions.empty or positions['code'].isna().any() or positions['session'].lt(0).any()
            or positions.duplicated(['date', 'code']).any()):
        raise ValueError('多空线组持仓明细为空、重复或含无效交易日期')
    positions = positions.sort_values(['code', 'session'])
    spell = positions.groupby('code')['session'].diff().ne(1).cumsum()
    spans = positions.groupby(['code', spell]).agg(
        entry_date=('date', 'first'), last_held=('date', 'last')).reset_index(level=1, drop=True).reset_index()
    closed = closed.copy()
    for column in ('entry_date', 'exit_date'):
        closed[column] = _dates(closed[column])
    if closed['exit_date'].isna().any():
        raise ValueError('平仓明细缺少退出日期')
    spans = spans.merge(closed, on=['code', 'entry_date'], how='outer',
                        indicator=True, validate='one_to_one')
    end = calendar[-1]
    live = spans['_merge'].eq('left_only')
    exits = spans.loc[spans['_merge'].eq('both')]
    if (spans['_merge'].eq('right_only').any() or spans.loc[live, 'last_held'].ne(end).any()
            or (calendar.get_indexer(exits['exit_date'])
                != calendar.get_indexer(exits['last_held']) + 1).any()):
        raise ValueError('持仓与平仓明细不一致，请核对三个 CSV 是否来自同一次完整回测')
    return spans.drop(columns='_merge')


def _daily_prices(get_price, code, calendar):
    def fetch(fields, fq):
        prices = get_price(code, start_date=calendar[0].date(), end_date=calendar[-1].date(),
                           frequency='daily', fields=fields, skip_paused=False, fq=fq)
        if prices is None or prices.empty:
            return pd.DataFrame(index=calendar, columns=fields, dtype=float)
        prices = prices.loc[:, fields].copy()
        prices.index = _dates(prices.index)
        if prices.index.has_duplicates:
            raise ValueError('%s 行情含重复日期' % code)
        return prices.reindex(calendar).apply(pd.to_numeric, errors='coerce')

    adjusted = fetch(['open', 'close'], 'pre')
    raw = fetch(['open', 'low_limit', 'high_limit', 'paused'], None)
    tape = adjusted.join(raw.rename(columns={'open': 'raw_open'}))
    valid = tape['close'].gt(0) & np.isfinite(tape['close'])
    tape['close'] = tape['close'].where(valid)
    tape['ma20'] = tape['close'].rolling(MA_DAYS).mean()
    return tape


def _positive(*prices):
    return all(np.isfinite(price) and price > 0 for price in prices)


def _giveback(tape, entry, sell):
    """持有中最高可见收盘到开盘卖出的回撤，开盘创新高时为零。"""
    peak = max(tape['open'].iloc[entry], tape['close'].iloc[entry:sell].max())
    return max(0.0, 1 - tape['open'].iloc[sell] / peak) * 100


def _shadow_trade(span, tape):
    entry = tape.index.get_loc(span.entry_date)
    live = pd.isna(span.exit_date)
    last = tape.index.get_loc(span.last_held if live else span.exit_date)
    row = dict(code=span.code, entry_date=span.entry_date, blend_exit_date=span.exit_date,
               year=tape.index[last].year, blend_exit_reason=span.exit_reason,
               blend_open_at_end=live, outcome='open_censored' if live else 'no_earlier_exit',
               first_confirmation_date=pd.NaT, ma20_confirmation_date=pd.NaT,
               ma20_exit_date=pd.NaT, blocked_opens=0, lead_sessions=np.nan,
               blend_giveback_pct=np.nan, ma20_giveback_pct=np.nan, giveback_saved_pp=np.nan,
               exit_price_edge_pct=np.nan, rebound20_pct=np.nan, data_issue='')
    if not live and (span.exit_reason != 'trend_break' or span.closure_source != 'filled_order'):
        row['outcome'] = 'other_exit'
        return row

    if (not _positive(tape['open'].iloc[entry])
            or tape['close'].iloc[entry:last + int(live)].isna().any()):
        return dict(row, outcome='data_missing', data_issue='missing_held_prices')
    confirmation = pd.NaT
    for sell in range(entry + CONFIRM_DAYS, last + 1):
        observed = tape.iloc[sell - CONFIRM_DAYS:sell]
        if observed['ma20'].isna().any():
            return dict(row, outcome='data_missing', data_issue='missing_ma20_window')
        if not observed['close'].lt(observed['ma20']).all():
            confirmation = pd.NaT
            continue
        if pd.isna(confirmation):
            confirmation = tape.index[sell - 1]
            if pd.isna(row['first_confirmation_date']):
                row['first_confirmation_date'] = confirmation
        opening = tape.iloc[sell]
        if opening['paused'] == 1:
            row['blocked_opens'] += 1
            continue
        raw_open, low, high = opening['raw_open'], opening['low_limit'], opening['high_limit']
        if opening['paused'] != 0 or not (_positive(raw_open, low, high) and high > low):
            return dict(row, outcome='data_missing', data_issue='missing_open_or_limits')
        if raw_open <= low + LIMIT_PAD:
            row['blocked_opens'] += 1
            continue
        sell_open = opening['open']
        blend_open = tape['open'].iloc[last]
        if not _positive(sell_open) or (not live and not _positive(blend_open)):
            return dict(row, outcome='data_missing', data_issue='missing_adjusted_open')
        giveback = _giveback(tape, entry, sell)
        row.update(ma20_confirmation_date=confirmation, ma20_exit_date=tape.index[sell],
                   ma20_giveback_pct=giveback)
        if live:
            row['outcome'] = 'earlier_while_blend_open'
        else:
            blend_giveback = _giveback(tape, entry, last)
            row.update(outcome='earlier' if sell < last else 'same_day', lead_sessions=last - sell,
                       blend_giveback_pct=blend_giveback, giveback_saved_pp=blend_giveback - giveback,
                       exit_price_edge_pct=(sell_open / blend_open - 1) * 100)
        forward = sell + FORWARD_DAYS
        if forward < len(tape) and _positive(tape['close'].iloc[forward]):
            row['rebound20_pct'] = (tape['close'].iloc[forward] / sell_open - 1) * 100
        return row
    return row


def _summarize(trades):
    groups = [('full', trades)] + list(trades.groupby('year', sort=True))
    rows = []
    for period, group in groups:
        paired = group[group['outcome'].eq('earlier')]
        evaluated = group['outcome'].isin(['earlier', 'same_day', 'no_earlier_exit']).sum()
        rebound = paired['rebound20_pct'].dropna()
        rows.append(dict(
            period=period, positions=len(group), closed_trend_evaluable=int(evaluated),
            earlier=len(paired), earlier_pct=len(paired) / evaluated * 100 if evaluated else np.nan,
            same_day=int(group['outcome'].eq('same_day').sum()),
            no_earlier_exit=int(group['outcome'].eq('no_earlier_exit').sum()),
            still_held_by_blend=int(group['blend_open_at_end'].sum()),
            earlier_while_blend_open=int(group['outcome'].eq('earlier_while_blend_open').sum()),
            other_exits=int(group['outcome'].eq('other_exit').sum()),
            data_missing=int(group['outcome'].eq('data_missing').sum()),
            median_lead_sessions=paired['lead_sessions'].median(),
            median_giveback_saved_pp=paired['giveback_saved_pp'].median(),
            median_exit_price_edge_pct=paired['exit_price_edge_pct'].median(),
            rebound20_observations=len(rebound),
            median_rebound20_pct=rebound.median() if len(rebound) else np.nan,
            rebound20_ge_5pct_count=int(rebound.ge(REBOUND_PCT).sum())))
    return pd.DataFrame(rows)


def run(data_dir='.'):
    from jqdata import get_price, get_trade_days

    folder = Path(data_dir)
    summary = _read_arm(folder, 'summary', ['first_date', 'last_date', 'trading_days'])
    positions = _read_arm(folder, 'positions', ['date', 'code'])
    closed = _read_arm(folder, 'holding_periods',
                       ['code', 'entry_date', 'exit_date', 'exit_reason', 'closure_source'])
    if len(summary) != 1:
        raise ValueError('summary 应包含唯一一条 blend 账户记录')
    settings = summary.iloc[0]
    start, end = pd.Timestamp(settings.first_date), pd.Timestamp(settings.last_date)
    warm = _dates(get_trade_days(end_date=start.date(), count=MA_DAYS))
    if len(warm) != MA_DAYS or warm[-1] != start:
        raise ValueError('回测起点缺少 MA20 所需的预热交易日')
    calendar = _dates(get_trade_days(start_date=warm[0].date(), end_date=end.date()))
    sessions = calendar[calendar >= start]
    if (len(sessions) != int(settings.trading_days) or sessions[-1] != end
            or not calendar.is_monotonic_increasing or calendar.has_duplicates):
        raise ValueError('交易日历与上轮回测区间不一致')
    spans = _holding_spans(positions, closed, sessions)
    rows = []
    for code, group in spans.groupby('code', sort=True):
        first = calendar.get_loc(group['entry_date'].min()) - MA_DAYS + 1
        last_date = group['exit_date'].fillna(end).max()
        last = calendar.get_loc(last_date) + FORWARD_DAYS
        tape = _daily_prices(get_price, code, calendar[first:last + 1])
        rows.extend(_shadow_trade(span, tape) for span in group.itertuples(index=False))
    trades = pd.DataFrame(rows).sort_values(['year', 'code', 'entry_date'])
    report = _summarize(trades)
    for name, table in [('trades', trades), ('summary', report)]:
        table.to_csv(folder / ('%s_%s.csv' % (OUTPUT_PREFIX, name)),
                     index=False, date_format='%Y-%m-%d', encoding='utf-8-sig')
    print(report.to_string(index=False))
    return report


if __name__ == '__main__':
    run()
