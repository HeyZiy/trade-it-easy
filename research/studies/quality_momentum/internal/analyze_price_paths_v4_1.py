"""只读 v4_1 聚宽导出与本地原始/HFQ收盘缓存，把 v1 的价格路径诊断搬到 4.1。

三个问题：大亏损长什么样、盈利是否回吐、按退出原因分组的卖出后表现。
收盘近似诊断，不生成新规则回测；大亏损用"首次成交至退出复权价格跌幅"定义。
运行：python research/studies/quality_momentum/internal/analyze_price_paths_v4_1.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from analyze_stop_loss import (  # noqa: E402
    Prices, episodes, markdown_table, names, read_version, require, sha)

OUT = STUDY / 'reports' / 'price_paths_v4_1_2026-10-10'
REPORT = STUDY / 'reports' / 'price_paths_v4_1_2026-10-10.md'
HORIZONS = (20, 40, 60)
SEVERE_LOSS = -0.15
LABELS = {
    'rank_below_buffer': '跌出排名9（留存线外，仍在池）',
    'vol_high': '波动率达到35%落池外',
    'financial': 'ROE或利润增长不过线落池外',
    'valuation': 'PE或PB越线落池外',
    'other': '停牌或其他',
}


def group_reason(exit_reason, pool_reason):
    reason = pool_reason if exit_reason == 'out_of_pool' else exit_reason
    if 'roe_low' in reason or 'profit_growth_low' in reason:
        return 'financial'
    if 'pe_high' in reason or 'pb_high' in reason:
        return 'valuation'
    return reason if reason in LABELS else 'other'


def path_fields(prices, benchmark, account, calendar, episodes_table, row):
    pair = prices.get(row['code'])
    if pair is None:
        return {'path_status': 'missing_cache'}
    raw_full, adjusted_full = pair
    raw, adjusted = raw_full.reindex(calendar), adjusted_full.reindex(calendar)
    entry, exit = row['entry_date'], row['exit_date']
    begin, end = calendar.get_loc(entry), calendar.get_loc(exit)
    endpoints = [raw.loc[entry], adjusted.loc[entry], raw.loc[exit], adjusted.loc[exit]]
    if not np.isfinite(endpoints).all() or min(endpoints) <= 0:
        return {'path_status': 'missing_endpoint_quote'}
    # 实际成交价按当天复权/原始收盘之比换尺度，只用于近似单份额价格路径。
    buy = row['first_fill_price'] * adjusted.loc[entry] / raw.loc[entry]
    sell = row['exit_fill_price'] * adjusted.loc[exit] / raw.loc[exit]
    path = adjusted.iloc[begin:end + 1] / buy - 1
    complete = not path.isna().any()
    fields = dict(path_status='ok' if complete else 'missing_holding_close',
                  observed_closes=int(path.notna().sum()), missing_closes=int(path.isna().sum()),
                  mae_close=min(0.0, float(path.min())) if complete else np.nan,
                  mfe_close=max(0.0, float(path.max())) if complete else np.nan,
                  peak_date=path.idxmax() if complete else pd.NaT,
                  approximate_exit_return=float(sell / buy - 1),
                  first_loss10_day=np.nan, first_loss10_date=pd.NaT)
    below10 = np.flatnonzero(path.to_numpy() <= -0.10)
    if complete and len(below10):
        fields['first_loss10_day'] = int(below10[0])
        fields['first_loss10_date'] = path.index[below10[0]]
    fields['holding_stock_close_return'] = float(adjusted.loc[exit] / adjusted.loc[entry] - 1)
    fields['holding_benchmark_return'] = float(benchmark.loc[exit] / benchmark.loc[entry] - 1)
    fields['holding_relative_benchmark'] = ((1 + fields['holding_stock_close_return'])
                                            / (1 + fields['holding_benchmark_return']) - 1)
    fields['giveback_percentage_points'] = (fields['mfe_close'] - fields['approximate_exit_return']
                                            if complete else np.nan)
    prior = adjusted_full[adjusted_full.index < entry].tail(61)
    if len(prior) >= 21 and prior.iloc[-21:].notna().all():
        fields['prior20_return'] = prior.iloc[-1] / prior.iloc[-21] - 1
        fields['prior_above_ma20'] = prior.iloc[-1] / prior.iloc[-20:].mean() - 1
    for horizon in HORIZONS:
        prefix = 'post%d_' % horizon
        if end + horizon >= len(calendar):
            fields[prefix + 'status'] = 'end_of_backtest'
            continue
        window = adjusted.iloc[end:end + horizon + 1]
        if window.isna().any():
            fields[prefix + 'status'] = 'missing_close'
            continue
        stock_return = float(window.iloc[-1] / window.iloc[0] - 1)
        index_return = float(benchmark.iloc[end + horizon] / benchmark.iloc[end] - 1)
        portfolio_return = float(account.iloc[end + horizon] / account.iloc[end] - 1)
        fields.update({prefix + 'status': 'ok', prefix + 'end_date': calendar[end + horizon],
                       prefix + 'stock_return': stock_return,
                       prefix + 'benchmark_return': index_return,
                       prefix + 'portfolio_return': portfolio_return,
                       prefix + 'relative_benchmark': (1 + stock_return) / (1 + index_return) - 1,
                       prefix + 'relative_portfolio': (1 + stock_return) / (1 + portfolio_return) - 1})
        later = episodes_table[episodes_table.code.eq(row['code'])
                               & episodes_table.entry_date.gt(exit)
                               & episodes_table.entry_date.le(calendar[end + horizon])]
        fields[prefix + 'reentered'] = not later.empty
    return fields


def main():
    baseline, hashes = read_version('v4_1', ' (1)')
    nav = baseline['nav']
    calendar = pd.DatetimeIndex(nav.date)
    benchmark = pd.Series(nav.benchmark_close.to_numpy(), index=calendar)
    account = pd.Series(nav.equity.to_numpy(), index=calendar)
    prices = Prices(calendar)
    name_map = names()
    eps = episodes(baseline, 'v4_1')
    closed = eps[eps.is_closed].copy()
    require(len(closed) == len(baseline['holding_periods']), '闭合轮数与平台持有期不一致')
    closed['group'] = [group_reason(a, b) for a, b in zip(closed.exit_reason, closed.pool_reason)]
    closed['group_label'] = closed.group.map(LABELS)
    require(closed.group.isin(LABELS).all(), '未知退出分组')
    closed['name'] = closed.code.map(name_map).fillna(closed.code)
    details = [path_fields(prices, benchmark, account, calendar, eps, row)
               for row in closed.to_dict('records')]
    for key in pd.DataFrame(details).columns:
        closed[key] = [item.get(key) for item in details]
    valid = closed[closed.path_status.eq('ok')].copy()
    require(valid.mae_close.le(0).all() and valid.mfe_close.ge(0).all(), '路径极值符号错误')
    severe = closed[closed.approximate_exit_return.le(SEVERE_LOSS)].copy()
    severe_valid = severe[severe.path_status.eq('ok')].copy()
    severe_valid['days_after_first_loss10'] = (severe_valid.holding_days
                                               - severe_valid.first_loss10_day)
    others = valid[~valid.index.isin(severe_valid.index)]
    giveback = valid[valid.mfe_close.ge(.15) & valid.approximate_exit_return.le(.05)]
    rally = valid[valid.post20_relative_portfolio.gt(.1)].nlargest(12, 'post20_relative_portfolio')

    severe_groups = severe_valid.groupby(['group', 'group_label'], observed=True).agg(
        episodes=('code', 'size'), buy_amount=('buy_amount', 'sum'),
        trade_cash_difference=('trade_cash_difference', 'sum'),
        median_holding_days=('holding_days', 'median'),
        median_exit_return=('approximate_exit_return', 'median')).reset_index()
    holding_groups = valid.groupby(['group', 'group_label'], observed=True).agg(
        episodes=('code', 'size'), median_mae=('mae_close', 'median'),
        median_mfe=('mfe_close', 'median'), median_exit_return=('approximate_exit_return', 'median'),
        median_giveback=('giveback_percentage_points', 'median'),
        median_holding_days=('holding_days', 'median')).reset_index()
    post_rows = []
    for horizon in HORIZONS:
        prefix = 'post%d_' % horizon
        sample = valid[valid[prefix + 'status'].eq('ok')]
        for (group, label), frame in sample.groupby(['group', 'group_label'], observed=True):
            post_rows.append(dict(horizon=horizon, group=group, group_label=label,
                episodes=len(frame), mean_stock_return=float(frame[prefix + 'stock_return'].mean()),
                median_stock_return=float(frame[prefix + 'stock_return'].median()),
                mean_relative_benchmark=float(frame[prefix + 'relative_benchmark'].mean()),
                mean_relative_portfolio=float(frame[prefix + 'relative_portfolio'].mean()),
                beat_portfolio_share=float(frame[prefix + 'relative_portfolio'].gt(0).mean()),
                stock_up10_share=float(frame[prefix + 'stock_return'].gt(.1).mean()),
                reentered_share=float(frame[prefix + 'reentered'].mean())))
    post_summary = pd.DataFrame(post_rows)
    entry_rows = []
    for label, frame in (('大亏损', severe_valid), ('其他闭合持仓', others)):
        entry_rows.append(dict(group=label, episodes=len(frame),
            prior20_observable=int(frame.prior20_return.notna().sum()),
            median_prior20=float(frame.prior20_return.median()),
            negative_prior20_share=float(frame.prior20_return.dropna().lt(0).mean()),
            below_ma20_share=float(frame.prior_above_ma20.dropna().lt(0).mean()),
            median_mfe=float(frame.mfe_close.median())))
    entry_state = pd.DataFrame(entry_rows)
    vol = valid[valid.group.eq('vol_high')]
    rank = valid[valid.group.eq('rank_below_buffer')]

    summary = {
        'date': '2026-10-10', 'version': 'v4_1',
        'analysis_end': str(nav.date.iloc[-1].date()),
        'closed_periods': len(closed), 'price_path_periods': len(valid),
        'codes': int(closed.code.nunique()), 'missing_cache_codes': sorted(
            closed.loc[closed.path_status.eq('missing_cache'), 'code'].str[:6].unique()),
        'path_status_counts': {k: int(v) for k, v in closed.path_status.value_counts().items()},
        'exit_groups': {LABELS[k]: int(v) for k, v in closed.group.value_counts().items()},
        'severe_loss_definition': '首次成交至退出复权价格跌幅<=-15%',
        'severe_periods': len(severe), 'severe_price_path_periods': len(severe_valid),
        'severe_share_of_periods': len(severe) / len(closed),
        'severe_buy_amount': float(severe.buy_amount.sum()),
        'severe_trade_cash_difference': float(severe.trade_cash_difference.sum()),
        'severe_mfe_not_above5': int(severe_valid.mfe_close.le(.05).sum()),
        'severe_mfe_atleast10': int(severe_valid.mfe_close.ge(.1).sum()),
        'severe_loss10_within10_days': int(severe_valid.first_loss10_day.le(10).sum()),
        'severe_loss10_atleast5_days_before_exit': int(
            severe_valid.days_after_first_loss10.ge(5).sum()),
        'severe_holding_benchmark_median': float(severe_valid.holding_benchmark_return.median()),
        'severe_relative_benchmark_median': float(severe_valid.holding_relative_benchmark.median()),
        'severe_benchmark_down10': int(severe_valid.holding_benchmark_return.le(-.1).sum()),
        'severe_underperformed_benchmark': int(severe_valid.holding_relative_benchmark.lt(0).sum()),
        'severe_by_exit_year': {str(k): int(v)
                                for k, v in severe.groupby(severe.exit_date.dt.year).size().items()},
        'large_giveback_periods': len(giveback),
        'vol_post20_median': float(vol.post20_stock_return.median()),
        'vol_post40_median': float(vol.post40_stock_return.median()),
        'vol_post60_median': float(vol.post60_stock_return.median()),
        'rank_post20_median': float(rank.post20_stock_return.median()),
        'rank_beat_portfolio_share_post20': float(rank.post20_relative_portfolio.gt(0).mean()),
        'rally_over10_count': int(valid.post20_relative_portfolio.gt(.1).sum()),
        'daily_close_only': True, 'new_execution_backtest': False,
        'sha256': hashes, 'cache_sha256': prices.hashes,
        'methods': {
            'severe': '大亏损按首次成交价至退出成交价的复权价格跌幅定义；现金口径trade_cash_difference'
                      '（卖出额-买入额-commission）不含分红，仅作辅助',
            'paths': '实际成交价按当日HFQ/原始收盘比换尺度，收盘路径为近似；缺测不填充，'
                     '完整路径才算MAE/MFE',
            'post_sale': '卖出日收盘起算20/40/60个净值交易日，对照中证800与实际策略净值；'
                         '窗口止于回测末日，缺测留空不填零',
            'portfolio': '账户净值含未逐股归属的分红现金流，相对策略收益是机会成本辅助对照'},
    }
    OUT.mkdir(parents=True, exist_ok=True)
    tables = {'all_price_paths': closed, 'severe_losses': severe,
              'severe_loss_groups': severe_groups, 'holding_path_groups': holding_groups,
              'post_exit_groups': post_summary, 'large_profit_givebacks': giveback,
              'continued_rally_examples': rally, 'entry_state_summary': entry_state}
    for key, frame in tables.items():
        frame.to_csv(OUT / (key + '.csv'), index=False, encoding='utf-8-sig')
    (OUT / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False, default=str),
        encoding='utf-8')
    (OUT / 'cache_manifest.json').write_text(
        json.dumps({**hashes, **prices.hashes}, indent=2), encoding='utf-8')
    write_report(summary, tables)
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ('sha256', 'cache_sha256')}, ensure_ascii=True, default=str))
    print(post_summary.to_string(index=False))


def write_report(summary, tables):
    severe_valid = tables['severe_losses']
    severe_valid = severe_valid[severe_valid.path_status.eq('ok')]
    entry = tables['entry_state_summary']
    post = tables['post_exit_groups'].query('horizon == 20')
    rally = tables['continued_rally_examples'].head(8).copy()
    losers = severe_valid.nsmallest(10, 'approximate_exit_return').copy()
    losers['first_loss10_day'] = losers.first_loss10_day.astype('Int64')
    lines = [
        '# 4.1 大亏损、盈利回吐与卖出后走势（2026-10-10）', '',
        f'把 v1 的价格路径诊断搬到 v4_1：只读聚宽导出与本地原始/HFQ收盘缓存，'
        f'覆盖{summary["analysis_end"]}前{summary["closed_periods"]}轮已闭合持有期，'
        f'{summary["price_path_periods"]}轮具有可观察收盘路径；'
        f'缺失缓存{len(summary["missing_cache_codes"])}只。收盘近似诊断，没有生成新规则回测。', '',
        'v4_1 的退出只发生在20日信号日：`out_of_pool` 表示落出质量池（波动/财务/估值），'
        '`rank_below_buffer` 表示仍在池但排名跌出9名留存线。没有止损与波动盘中退出，'
        '这是与 v1 样本结构最大的不同。', '',
        '## 大亏损主要呈现买入后走弱', '',
        f'按"首次成交至退出复权价格跌幅不高于−15%"定义大亏损，共{summary["severe_periods"]}轮，'
        f'占{summary["severe_share_of_periods"]:.1%}，对应买入额{summary["severe_buy_amount"]:,.0f}元。'
        f'现金口径合计（卖出−买入−费用，缺分红）{summary["severe_trade_cash_difference"]:,.0f}元。', '',
        f'有完整路径的大亏损{len(severe_valid)}轮中，'
        f'{summary["severe_mfe_not_above5"]}轮持有期最高收盘浮盈不超过5%，'
        f'仅{summary["severe_mfe_atleast10"]}轮曾达到10%浮盈——大亏损主要不是先大赚再回吐。'
        f'{summary["severe_loss10_within10_days"]}轮在买入后前10个交易日内收盘即跌破首次买价10%；'
        f'{summary["severe_loss10_atleast5_days_before_exit"]}轮在最终退出至少5个交易日前曾触及该跌幅。', '',
        f'同期中证800跌至少10%的仅{summary["severe_benchmark_down10"]}轮，'
        f'而{summary["severe_underperformed_benchmark"]}轮个股落后于基准，'
        f'相对基准中位数{summary["severe_relative_benchmark_median"]:.2%}：'
        '大亏损主要来自个股选择，不是市场下跌。', '',
        markdown_table(tables['severe_loss_groups'],
            {'group_label': '退出分组', 'episodes': '大亏损轮数', 'buy_amount': '买入额（元）',
             'trade_cash_difference': '现金差（元）', 'median_holding_days': '持有交易日中位数',
             'median_exit_return': '价格跌幅中位数'},
            ('median_exit_return',), ('buy_amount', 'trade_cash_difference')), '',
        '### 大亏损最深10轮', '',
        markdown_table(losers,
            {'name': '股票', 'entry_date': '买入日', 'exit_date': '卖出日', 'holding_days': '持有交易日',
             'approximate_exit_return': '价格涨跌', 'mfe_close': '最高收盘浮盈',
             'first_loss10_day': '首次收盘跌10%日序'},
            ('approximate_exit_return', 'mfe_close')), '',
        '## 入场前状态：大亏损与其他持仓', '',
        markdown_table(entry,
            {'group': '分组', 'episodes': '轮次', 'prior20_observable': '前20日可观察',
             'median_prior20': '入场前20日收益中位数', 'negative_prior20_share': '前20日下跌占比',
             'below_ma20_share': '入场前低于MA20占比', 'median_mfe': '最高收盘浮盈中位数'},
            ('median_prior20', 'negative_prior20_share', 'below_ma20_share', 'median_mfe')), '',
        '入场前指标只使用买入日前一交易日及更早收盘。事后分组只能诊断，不能直接证明新门槛有效。', '',
        '## 卖出后20日表现（按退出分组）', '',
        markdown_table(post,
            {'group_label': '退出分组', 'episodes': '可观察轮次', 'median_stock_return': '卖后20日涨幅中位数',
             'mean_relative_benchmark': '相对基准均值', 'mean_relative_portfolio': '相对策略净值均值',
             'beat_portfolio_share': '胜过策略占比', 'reentered_share': '20日内重新开仓占比'},
            ('median_stock_return', 'mean_relative_benchmark', 'mean_relative_portfolio',
             'beat_portfolio_share', 'reentered_share')), '',
        f'波动落池外组卖后20/40/60日涨幅中位数分别为{summary["vol_post20_median"]:.2%}、'
        f'{summary["vol_post40_median"]:.2%}、{summary["vol_post60_median"]:.2%}；'
        f'排名留存线外组卖后20日中位数{summary["rank_post20_median"]:.2%}，'
        f'胜过实际策略占比{summary["rank_beat_portfolio_share_post20"]:.1%}。'
        '各窗口样本数不同，平均可被少数后续大涨拉高；不能据此统一放宽退出。'
        '重叠窗口未做独立样本显著性检验。', '',
        f'卖后20日相对策略净值跑赢超过10%的共{summary["rally_over10_count"]}轮，'
        '以下为最极端8例（事后名单，不能用于重设规则；部分在窗口内已实际重新开仓）：', '',
        markdown_table(rally,
            {'name': '股票', 'exit_date': '卖出日', 'group_label': '退出分组',
             'approximate_exit_return': '原持有期价格涨跌', 'post20_stock_return': '卖后20日涨幅',
             'post20_relative_portfolio': '相对策略收益', 'post20_reentered': '20日内重新开仓'},
            ('approximate_exit_return', 'post20_stock_return', 'post20_relative_portfolio')), '',
        '## 盈利回吐', '',
        f'{summary["price_path_periods"]}轮可观察路径中{summary["large_giveback_periods"]}轮'
        '曾有至少15%收盘浮盈、实际退出时相对首次买价不超过5%。详见'
        '[large_profit_givebacks.csv](price_paths_v4_1_2026-10-10/large_profit_givebacks.csv)，'
        '与减仓实现的部分利润需区分。', '',
        '## 方法边界', '',
        '价格缓存由`pool_curve_v1.py`生成（新浪日线，仅收盘）；按当天复权/原始收盘比把实际成交价'
        '换尺度，属单份额近似，忽略盘中、增减仓与资金复利。缺测行情不填零，卖出后目标日缺行'
        '不以复牌价替代；缺失缓存样本不能默认表现良好。源CSV与缓存SHA256见summary.json。', '',
        '## 复现', '',
        '`python research/studies/quality_momentum/internal/analyze_price_paths_v4_1.py`（项目 .conda 环境）', '',
        '对照材料：[v1版同名报告](../../roe_quality/reports/pool_rotation_v1_price_paths_2026-10-05.md)、'
        '[4.1/4.1.1止损诊断](stop_loss_2026-10-10/report.md)。', '',
    ]
    REPORT.write_text('\n'.join(lines), encoding='utf-8')
    print('报告：', REPORT)


if __name__ == '__main__':
    main()
