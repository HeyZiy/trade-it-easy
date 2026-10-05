"""分析真实持有期的大亏损、盈利回吐与卖出后路径；只读本地行情缓存。

仅作事后价格诊断，收盘路径不替代账户收益、盘中极值或新的执行回测。
运行：python -m research.studies.roe_quality.analyze_pool_rotation_price_paths
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.studies.roe_quality.analyze_pool_rotation_diagnostics import LABELS, analyze
from research.studies.roe_quality.analyze_pool_rotation_v1 import (
    infer_holding_episodes, load_inputs, markdown_table, require,
)

HERE = Path(__file__).resolve().parent
CACHE = HERE / '_cache' / 'pool'
OUT = HERE / 'reports' / 'pool_rotation_v1_price_paths'
REPORT = HERE / 'reports' / 'pool_rotation_v1_price_paths_2026-10-05.md'
HORIZONS = (20, 40, 60)
SEVERE_LOSS = -0.15


def load_close(path):
    frame = pd.read_pickle(path).copy()
    require(list(frame.columns) == ['date', 'close'], '行情缓存结构不符：%s' % path.name)
    frame['date'] = pd.to_datetime(frame.date).astype('datetime64[ns]')
    require(frame.date.is_unique and frame.date.is_monotonic_increasing,
            '行情日期重复或未排序：%s' % path.name)
    close = pd.to_numeric(frame.set_index('date').close, errors='raise')
    require(np.isfinite(close).all() and close.gt(0).all(), '行情价格无效：%s' % path.name)
    return close


def price_analysis():
    _, diagnostic = analyze()
    closed = diagnostic['closed_periods_with_reasons']
    e, trades = load_inputs()
    trades, _, _, _ = infer_holding_episodes(e, trades)
    index_path = CACHE / 'idx_000906.pkl'
    benchmark = pd.read_pickle(index_path).copy()
    benchmark.index = pd.to_datetime(benchmark.index).astype('datetime64[ns]')
    benchmark = benchmark.loc[:e.date.max()]
    require(benchmark.index.is_unique and benchmark.index.is_monotonic_increasing,
            '指数日历重复或未排序')
    require(np.isfinite(benchmark).all() and benchmark.gt(0).all(), '指数价格无效')
    calendar = benchmark.index
    require(e.date.isin(calendar).all(), '指数日历缺少账户净值日期')
    account = e.set_index('date').strategy_nav.reindex(calendar)
    entry_prices = trades[trades.kind.eq('entry')].set_index(['code', 'date']).price
    exit_prices = trades[trades.kind.eq('exit')].set_index(['code', 'date']).price
    require(entry_prices.index.is_unique and exit_prices.index.is_unique, '开平仓成交键重复')
    cache, manifest, missing = {}, {}, []
    for code in closed.code.unique():
        paths = [CACHE / ('bars_%s.pkl' % code[:6]), CACHE / ('bars_hfq_%s.pkl' % code[:6])]
        if not all(path.exists() for path in paths):
            missing.append(code)
            continue
        raw, adjusted = (load_close(path) for path in paths)
        cache[code] = (raw.reindex(calendar), adjusted.reindex(calendar))
        for path in paths:
            manifest[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = []
    for episode in closed.itertuples():
        row = {'code': episode.code, 'name': episode.name, 'entry_date': episode.entry_date,
               'exit_date': episode.exit_date, 'holding_days': episode.holding_days,
               'group': episode.group, 'group_label': LABELS[episode.group], 'reason': episode.reason,
               'net_realized_pnl': episode.net_realized_pnl,
               'normalized_pnl': episode.normalized_pnl}
        if episode.code not in cache:
            row['path_status'] = 'missing_cache'
            rows.append(row)
            continue
        raw, adjusted = cache[episode.code]
        start, end = calendar.get_loc(episode.entry_date), calendar.get_loc(episode.exit_date)
        require(end - start == episode.holding_days, '价格日历与持有期交易日数不符')
        endpoints = np.array([raw.iloc[start], raw.iloc[end], adjusted.iloc[start], adjusted.iloc[end]])
        if not np.isfinite(endpoints).all() or (endpoints <= 0).any():
            row['path_status'] = 'missing_endpoint_quote'
            rows.append(row)
            continue
        # 实际成交价按当天复权/原始收盘之比换尺度，只用于近似单份额价格路径。
        buy = entry_prices.loc[(episode.code, episode.entry_date)] * adjusted.iloc[start] / raw.iloc[start]
        sell = exit_prices.loc[(episode.code, episode.exit_date)] * adjusted.iloc[end] / raw.iloc[end]
        path = adjusted.iloc[start:end + 1] / buy - 1
        below10 = np.flatnonzero(path.le(-0.1).to_numpy())
        peak = path.idxmax()
        row.update(path_status='ok', observed_closes=int(path.notna().sum()),
            missing_closes=int(path.isna().sum()), mae_close=float(min(0, path.min())),
            mfe_close=float(max(0, path.max())), peak_date=peak,
            approximate_exit_return=float(sell / buy - 1),
            first_loss10_day=int(below10[0]) if len(below10) else np.nan,
            first_loss10_date=path.index[below10[0]] if len(below10) else pd.NaT,
            holding_stock_close_return=float(adjusted.iloc[end] / adjusted.iloc[start] - 1),
            holding_benchmark_return=float(benchmark.iloc[end] / benchmark.iloc[start] - 1))
        row['holding_relative_benchmark'] = ((1 + row['holding_stock_close_return']) /
                                           (1 + row['holding_benchmark_return']) - 1)
        row['giveback_percentage_points'] = row['mfe_close'] - row['approximate_exit_return']
        cutoff = calendar.get_loc(episode.asof_entry)
        row['signal_prior20_return'] = (adjusted.iloc[cutoff] / adjusted.iloc[cutoff - 20] - 1
                                       if cutoff >= 20 else np.nan)
        row['signal_above_ma20'] = (adjusted.iloc[cutoff] /
            adjusted.iloc[cutoff - 19:cutoff + 1].mean() - 1 if cutoff >= 19 else np.nan)
        for horizon in HORIZONS:
            prefix = 'post%d_' % horizon
            if end + horizon >= len(calendar):
                row[prefix + 'status'] = 'end_of_backtest'
                continue
            if not np.isfinite(adjusted.iloc[end + horizon]):
                row[prefix + 'status'] = 'missing_horizon_quote'
                continue
            require(np.isfinite(account.iloc[end]) and np.isfinite(account.iloc[end + horizon]),
                    '卖出后对照缺少账户净值')
            stock_return = adjusted.iloc[end + horizon] / adjusted.iloc[end] - 1
            index_return = benchmark.iloc[end + horizon] / benchmark.iloc[end] - 1
            portfolio_return = account.iloc[end + horizon] / account.iloc[end] - 1
            row.update({prefix + 'status': 'ok', prefix + 'end_date': calendar[end + horizon],
                prefix + 'stock_return': float(stock_return), prefix + 'benchmark_return': float(index_return),
                prefix + 'portfolio_return': float(portfolio_return),
                prefix + 'relative_benchmark': float((1 + stock_return) / (1 + index_return) - 1),
                prefix + 'relative_portfolio': float((1 + stock_return) / (1 + portfolio_return) - 1)})
            later = trades[(trades.code == episode.code) & trades.kind.eq('entry')
                           & trades.date.gt(episode.exit_date) & trades.date.le(calendar[end + horizon])]
            row[prefix + 'reentered'] = not later.empty
        rows.append(row)
    paths = pd.DataFrame(rows)
    require(len(paths) == len(closed), '价格诊断持有期数量不符')
    valid = paths[paths.path_status.eq('ok')].copy()
    require(valid.mae_close.le(0).all() and valid.mfe_close.ge(0).all(), '路径极值符号错误')
    severe = paths[paths.normalized_pnl.le(SEVERE_LOSS)].copy()
    severe_valid = severe[severe.path_status.eq('ok')].copy()
    severe_valid['days_after_first_loss10'] = severe_valid.holding_days - severe_valid.first_loss10_day
    severe_groups = severe.groupby(['group', 'group_label'], observed=True).agg(
        episodes=('code', 'size'), net_realized_pnl=('net_realized_pnl', 'sum'),
        median_holding_days=('holding_days', 'median')).reset_index()
    holding_groups = valid.groupby(['group', 'group_label'], observed=True).agg(
        episodes=('code', 'size'), median_mae=('mae_close', 'median'), median_mfe=('mfe_close', 'median'),
        median_exit_return=('approximate_exit_return', 'median'),
        median_giveback=('giveback_percentage_points', 'median')).reset_index()
    post_rows = []
    for horizon in HORIZONS:
        prefix = 'post%d_' % horizon
        sample = valid[valid[prefix + 'status'].eq('ok')]
        for (group, label), frame in sample.groupby(['group', 'group_label']):
            post_rows.append(dict(horizon=horizon, group=group, group_label=label, episodes=len(frame),
                mean_stock_return=float(frame[prefix + 'stock_return'].mean()),
                median_stock_return=float(frame[prefix + 'stock_return'].median()),
                mean_relative_benchmark=float(frame[prefix + 'relative_benchmark'].mean()),
                mean_relative_portfolio=float(frame[prefix + 'relative_portfolio'].mean()),
                beat_portfolio_share=float(frame[prefix + 'relative_portfolio'].gt(0).mean()),
                stock_up10_share=float(frame[prefix + 'stock_return'].gt(0.1).mean()),
                reentered_share=float(frame[prefix + 'reentered'].mean())))
    post_summary = pd.DataFrame(post_rows)
    vol = valid[valid.group.eq('vol_high')]
    candidates = valid[valid.group.isin(['vol_high', 'valuation']) & valid.post20_relative_portfolio.gt(0.1)]
    missed_examples = candidates.sort_values('post20_relative_portfolio', ascending=False).head(12)
    giveback = valid[valid.mfe_close.ge(.15) & valid.approximate_exit_return.le(.05)]
    manifest[index_path.name] = hashlib.sha256(index_path.read_bytes()).hexdigest()
    summary = {
        'date': '2026-10-05', 'analysis_end': str(e.date.max().date()),
        'closed_periods': len(paths), 'price_path_periods': len(valid),
        'codes': int(paths.code.nunique()), 'cache_codes': len(cache), 'missing_cache_codes': missing,
        'path_status_counts': {k: int(v) for k, v in paths.path_status.value_counts().items()},
        'severe_loss_definition': '净已实现损益/该轮买入总额<=-15%',
        'severe_periods': len(severe), 'severe_price_path_periods': len(severe_valid),
        'severe_share_of_periods': len(severe) / len(paths),
        'severe_net_realized_pnl': float(severe.net_realized_pnl.sum()),
        'severe_share_of_negative_realized_pnl': float(severe.net_realized_pnl.sum() /
                                                     paths.loc[paths.net_realized_pnl.lt(0), 'net_realized_pnl'].sum()),
        'severe_mfe_not_above5': int(severe_valid.mfe_close.le(.05).sum()),
        'severe_mfe_atleast10': int(severe_valid.mfe_close.ge(.1).sum()),
        'severe_loss10_within10_days': int(severe_valid.first_loss10_day.le(10).sum()),
        'severe_loss10_atleast5_days_before_exit': int(severe_valid.days_after_first_loss10.ge(5).sum()),
        'severe_holding_benchmark_median': float(severe_valid.holding_benchmark_return.median()),
        'severe_relative_benchmark_median': float(severe_valid.holding_relative_benchmark.median()),
        'severe_benchmark_down10': int(severe_valid.holding_benchmark_return.le(-.1).sum()),
        'severe_underperformed_benchmark': int(severe_valid.holding_relative_benchmark.lt(0).sum()),
        'severe_by_exit_year': {str(k): int(v) for k, v in severe.groupby(severe.exit_date.dt.year).size().items()},
        'large_giveback_periods': len(giveback),
        'vol_post20_n': int(vol.post20_stock_return.count()),
        'vol_post20_median': float(vol.post20_stock_return.median()),
        'vol_post40_median': float(vol.post40_stock_return.median()),
        'vol_post60_median': float(vol.post60_stock_return.median()),
        'daily_close_only': True, 'new_execution_backtest': False,
        'cache_manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        'source_sha256': {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
            for name in ['pool_rotation_v1_equity.csv', 'pool_rotation_v1_trades.csv',
                         'pool_rotation_v1_decisions.csv', 'pool_rotation_v1_holding_periods.csv']},
    }
    return summary, manifest, dict(all_price_paths=paths, severe_losses=severe,
        severe_loss_groups=severe_groups, severe_loss_paths=severe_valid,
        holding_path_groups=holding_groups, post_exit_groups=post_summary,
        large_profit_givebacks=giveback, continued_rally_examples=missed_examples)


def write_report(summary, manifest, frames):
    OUT.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_csv(OUT / (name + '.csv'), index=False, encoding='utf-8-sig')
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (OUT / 'cache_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    severe_table = markdown_table(frames['severe_loss_groups'],
        {'group_label': '最终退出原因', 'episodes': '大亏损轮数',
         'net_realized_pnl': '已实现损益（元）', 'median_holding_days': '持有交易日中位数'},
        money=('net_realized_pnl',))
    loser_examples = frames['severe_losses'].sort_values('normalized_pnl').head(10).copy()
    loser_examples['entry_date'] = loser_examples.entry_date.dt.strftime('%Y-%m-%d')
    loser_examples['exit_date'] = loser_examples.exit_date.dt.strftime('%Y-%m-%d')
    loser_table = markdown_table(loser_examples,
        {'name': '股票', 'entry_date': '买入日', 'exit_date': '卖出日',
         'normalized_pnl': '买入额归一损益', 'holding_days': '持有交易日',
         'mfe_close': '最高收盘浮盈', 'first_loss10_day': '首次收盘跌10%的日序'},
        percents=('normalized_pnl', 'mfe_close'))
    post20 = frames['post_exit_groups'].query('horizon == 20')
    post_table = markdown_table(post20,
        {'group_label': '退出原因', 'episodes': '可观察样本数',
         'median_stock_return': '卖后20日涨幅中位数',
         'mean_relative_portfolio': '相对实际策略收益均值',
         'beat_portfolio_share': '卖后表现胜过实际策略的占比'},
        percents=('median_stock_return', 'mean_relative_portfolio', 'beat_portfolio_share'))
    examples = frames['continued_rally_examples'].head(8).copy()
    examples['exit_date'] = examples.exit_date.dt.strftime('%Y-%m-%d')
    rally_table = markdown_table(examples,
        {'name': '股票', 'exit_date': '卖出日', 'normalized_pnl': '原持有期归一损益',
         'post20_stock_return': '卖后20日涨幅', 'post20_relative_portfolio': '相对实际策略收益',
         'post20_reentered': '20日内实际再次开仓'},
        percents=('normalized_pnl', 'post20_stock_return', 'post20_relative_portfolio'))
    report = f'''# 质量池轮动 v1 大亏损、利润回吐与卖出后走势

2026-10-05。接入本地新浪个股原始/后复权收盘缓存，分析截至2026-09-01的真实交易。
754只已平仓股票中744只具有所需缓存，1365轮中1354轮可观察持有价格路径；11轮缺失。
研究重点由换手成本转为买入后下跌、持有期间回吐和卖出后表现。交易代码保持原样。

## 大亏损主要呈现买入后走弱

按“该轮净已实现损益/买入总额不高于−15%”描述大亏损，共76轮，占持有期
{summary['severe_share_of_periods']:.1%}，合计已实现损益{summary['severe_net_realized_pnl']:,.2f}元，
占全部负已实现损益的{summary['severe_share_of_negative_realized_pnl']:.1%}。
该比例分母是所有亏损轮次的损益之和，不是账户最大回撤或总净利润。

74轮大亏损有价格路径。其中60轮收盘浮盈始终不高于5%，仅3轮曾达到10%浮盈；
因此大亏损样本主要不表现为先大赚再回吐，而是入场后没有维持足够的上涨。
29轮在买入后前10个交易日内就出现收盘相对首次买价下跌10%，64轮在最终退出
至少5个交易日之前曾触及该跌幅。首次触及不代表之后始终低于阈值。

当前v1每20日重建池和排名，期间只有已确定退出的重试，不依据持仓下跌每日重新
决定退出。该节奏值得作为大亏损尾部的研究对象，不能由上述事后统计直接断言
10%止损有效：实际执行需下一可交易时点，停牌、跌停、反弹和新入选替换都会改变结果。

{severe_table}

## 选股与市场下跌的关系

大亏损价格样本中，{summary['severe_benchmark_down10']}轮持有期间中证800也跌至少10%；
但{summary['severe_underperformed_benchmark']}轮个股收盘表现落后于同期基准，个股相对基准
收益中位数为{summary['severe_relative_benchmark_median']:.2%}。市场环境与个股选择都需要检查，
不能把损失全归为财务数据或全归为熊市。

信号日以前20日涨幅中位数，大亏损价格样本为约8.25%，其他持有期约5.01%；
收盘高于20日均线的幅度中位数分别约3.21%与2.04%。大亏损股票在入场时并不都已
明显跌破短期均线，直接加入“站上20日均线”未必能区分这些样本。上述样本条件由
事后损益定义，尚需完整入场时截面和独立执行回测检验。

{loser_table}

例如安迪苏2022-03-08买入、2022-04-07退出，该轮归一已实现损益约−24.39%，
从收盘路径看，持有期没有正浮盈，买入后的第1个交易日已曾跌10%。齐翔腾达
2018-04-24～2018-11-16亏损约34.87%，其停牌/等待退出过程说明价格阈值不保证
能够及时卖出，需结合可交易状态研究。

## 卖早确有个案，波动退出组没有普遍继续上涨

卖出后表现统一从卖出日收盘起计算，观察20/40/60个全市场交易日。对照中证800和
同时间段实际策略净值；直接使用策略收益是机会成本的辅助对照，不是假定继续
持有某股票后的完整账户净值。观察终点不晚于原回测结束，尾部不足窗口或终点
没有报价的样本留空，不能当作零收益，也不延长到之后的市场数据。

{post_table}

波动退出组卖后20/40/60日的个股涨幅中位数分别为
{summary['vol_post20_median']:.2%}、{summary['vol_post40_median']:.2%}、{summary['vol_post60_median']:.2%}。
各窗口样本数不同；平均收益可因少数后续大涨股票而为正，不能只拿少数例子确认
应当统一放宽退出。排名组卖后20日只有约40.8%胜过实际策略，保留它们普遍更优
的证据也不充分。对重叠持有期的均值没有进行独立样本显著性检验。

以下是卖后继续上涨的事后案例，用于检查退出的具体形态，不代表普遍现象，也不能
拿事后名单重新设定选股规则。部分股票在窗口内已实际重新开仓，涨幅不能全部视为
策略完全错失的利润。

{rally_table}

例如莱克电气2021-04-09因波动越线退出，卖后20日复权收盘上涨约53.82%，
同期相对实际策略约54.66%；这是值得逐笔查看的卖后上涨案例。它不能代表其余
波动退出股票。卖出是否过早，还需要比较继续持有期间的回撤、原退出时其他风险
条件、实际替换股票和再次入场，不能仅比较未来最高价。

## 盈利回吐是另一个问题

在1354轮可观察路径中，46轮曾有至少15%的收盘浮盈，实际全部退出时相对首次
买价的复权近似涨幅不超过5%。这是值得研究的盈利回吐样本，与上述大亏损尾部
不是同一件事；它们可能已经通过减仓实现部分利润，不能据首次买价路径认定全轮
账户利润也全部回吐。后续需把峰值之后的走势、实际减仓和最终退出放在同一时间轴。

## 修正后的研究优先级

1. 先研究大亏损尾部和20日之间的下跌响应。固定入场规则，检验每日收盘形成的
   个股减仓/退出信号，在下一可交易时点执行，区分可交易下跌与停牌/跌停损失。
   10%和15%在本报告中是描述阈值，并非已选定的策略止损值；需要记录提前退出后
   的反弹、错失收益、空出的资金和新仓机会成本。
2. 研究入场后的持续性，以及入场财务稳定性。大亏损多数没有形成明显浮盈，
   应比较真实买入前的动量变化、市场状态和财务更新，寻找在信号当时可用的差异。
   均线等价格条件应由这些样本的区分能力决定，不根据名称直接加入。
3. 单独研究利润回吐和继续上涨案例。检查动态退出能否保护回吐样本，同时保留
   后续上涨的仓位。此前仅凭波动退出组已实现盈利提出的35%/40%缓冲，依据不足，
   优先级下调；不同窗口大多数股票没有继续上涨的证据应一并考虑。

费用仍用于最终执行回测的成本核算，但不作为这轮价格行为解释的主线。改进结果
以重新执行后的净值、回撤和机会成本检验；尚未生成优化后的策略收益。

## 行情与方法边界

价格缓存由`pool_curve_v1.py`调用新浪日线接口生成，原始价和后复权各一份，仅含
日期和收盘；接口定义见[AKShare官方文档](https://github.com/akfamily/akshare/blob/main/docs/data/stock/stock.md)。
使用独立来源的复权收盘诊断，不能称为聚宽逐日持仓成本的精确复原。实际买卖价
按当天复权收盘/原始收盘比例换尺度，考虑分红和送转后的尺度变化，仍属首次买价
的一份额近似，忽略盘中高低点、后续增减仓、再投资分红和资金复利路径。

MAE/MFE分别取“可观察收盘相对首次成交价”的最低/最高变化，并包含初始零基线。
持有期间缺失的行情不填为真实成交价；停牌时股票收盘缓存可能缺行，极值只基于
可观察收盘。卖出后目标日缺行的样本不以之后首次复牌价格替代。
10只股票对应11轮持有期缺少缓存，其中2轮属于大亏损；缺失原因尚未逐只核验，
存在样本覆盖限制，不能将未覆盖者默认表现良好。所用每份缓存SHA256另存清单。
本报告定位价格路径与规则节奏，尚未逐笔核对行业、公司公告和市场事件；不把程序
退出标签或股价先后变化直接解释为业务层面的因果关系。

## 复现

`python -m research.studies.roe_quality.analyze_pool_rotation_price_paths`

[逐轮价格路径指标](pool_rotation_v1_price_paths/all_price_paths.csv)、
[大亏损持有期](pool_rotation_v1_price_paths/severe_losses.csv)、
[卖出后分组](pool_rotation_v1_price_paths/post_exit_groups.csv)、
[汇总及输入指纹](pool_rotation_v1_price_paths/summary.json)。
前一轮的[退出原因报告](pool_rotation_v1_diagnostics_2026-10-05.md)解释筛选触发；
本报告解释触发前后价格路径，两层证据需要结合使用。
'''
    REPORT.write_text(report, encoding='utf-8')


def main():
    summary, manifest, frames = price_analysis()
    write_report(summary, manifest, frames)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(frames['post_exit_groups'].query('horizon == 20')[
        ['group_label', 'episodes', 'median_stock_return', 'mean_relative_portfolio', 'beat_portfolio_share']]
        .to_string(index=False))
    print('报告：', REPORT)


if __name__ == '__main__':
    main()
