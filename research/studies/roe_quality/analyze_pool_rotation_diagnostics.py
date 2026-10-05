"""核验质量池轮动的决策/持有期CSV，关联首轮成交的已实现损益。

不改变原始CSV或交易代码，不查询行情，不推算更改规则后的净值。
运行：python -m research.studies.roe_quality.analyze_pool_rotation_diagnostics
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.studies.roe_quality.analyze_pool_rotation_v1 import (
    infer_holding_episodes, load_inputs, markdown_table, require,
)

HERE = Path(__file__).resolve().parent
OUT = HERE / 'reports' / 'pool_rotation_v1_diagnostics'
REPORT = HERE / 'reports' / 'pool_rotation_v1_diagnostics_2026-10-05.md'
LABELS = {
    'vol_high': '波动率达到35%',
    'rank_below_buffer': '跌出排名30',
    'financial': 'ROE或利润增长不过线',
    'valuation': 'PE或PB越线',
    'other': '解禁、停牌或财务行不可用',
}
GROUP_ORDER = list(LABELS)


def group_reason(reason):
    if 'roe_low' in reason or 'profit_growth_low' in reason:
        return 'financial'
    if 'pe_high' in reason or 'pb_high' in reason:
        return 'valuation'
    return reason if reason in ('vol_high', 'rank_below_buffer') else 'other'


def aggregate(frame, keys):
    result = frame.groupby(keys, observed=True).agg(
        episodes=('code', 'size'), gross_realized_pnl=('realized_pnl', 'sum'),
        fee=('fee', 'sum'), net_realized_pnl=('net_realized_pnl', 'sum'),
        win_rate=('net_realized_pnl', lambda s: (s > 0).mean()),
        median_holding_days=('holding_days', 'median'),
        median_normalized_pnl=('normalized_pnl', 'median'),
    ).reset_index()
    result['share'] = result.episodes / len(frame)
    return result


def analyze():
    d = pd.read_csv(HERE / 'pool_rotation_v1_decisions.csv', encoding='utf-8-sig')
    h = pd.read_csv(HERE / 'pool_rotation_v1_holding_periods.csv', encoding='utf-8-sig')
    require(not d.empty and not h.empty, '诊断表为空')
    require(not d.duplicated(['signal_day', 'code']).any(), '同一信号日股票决策重复')
    require(not h.duplicated(['code', 'entry_date', 'exit_date']).any(), '实际持有期重复')
    require(d.action.isin(['entry', 'keep', 'exit', 'data_pause']).all(), '未知决策类型')
    for frame, fields in ((d, ['signal_day', 'asof', 'entry_date']),
                          (h, ['entry_date', 'exit_date', 'signal_day'])):
        for field in fields:
            frame[field] = pd.to_datetime(frame[field]).astype('datetime64[ns]')
    require((d['asof'] < d.signal_day).all(), '决策时点未早于信号日')
    require(h.entry_source.eq('filled_order').all()
            and h.closure_source.eq('filled_order').all(), '存在不能归属实际成交的持有期')
    require((h.exit_date > h.entry_date).all(), '持有期日期顺序错误')
    e, t = load_inputs()
    t, c, opened, _ = infer_holding_episodes(e, t)
    x = h.merge(c.rename(columns={'entry': 'entry_date', 'exit': 'exit_date'}),
                on=['code', 'entry_date', 'exit_date'], how='outer',
                validate='one_to_one', indicator=True)
    require(x._merge.eq('both').all(), '新持有期与首轮成交的开平仓日期不一致，不能关联损益')
    require(x.holding_days.eq(x.trading_days).all(), '新旧持有交易日数不一致')
    x = x.drop(columns='_merge')
    require((h.signal_day < h.exit_date).all(), '退出信号必须早于实际退出日')
    exits = d[d.action.eq('exit')].copy()
    reason_check = h.merge(exits[['code', 'signal_day', 'exit_reason', 'pool_reason']],
        on=['code', 'signal_day'], how='left', suffixes=('', '_decision'), validate='many_to_one')
    require(reason_check.exit_reason.eq(reason_check.exit_reason_decision).all()
            and reason_check.pool_reason.eq(reason_check.pool_reason_decision).all(),
            '实际退出原因未能对应原始退出信号')
    x = x.merge(exits.drop(columns=['entry_date', 'exit_reason', 'pool_reason']),
                on=['code', 'signal_day'], how='left', validate='many_to_one')
    require(x.held_days_at_signal.notna().all(), '退出信号信息缺失')
    require(x.held_days_at_signal.le(x.holding_days).all(), '信号年龄晚于实际退出年龄')
    previous_day = dict(zip(e.date, e.date.shift()))
    next_day = dict(zip(e.date, e.date.shift(-1)))
    x['entry_signal_day'] = x.entry_date.map(previous_day)
    entries = d[d.action.eq('entry')].drop(columns=['entry_date', 'action', 'exit_reason',
                                                   'pool_reason'])
    entries = entries.rename(columns={'signal_day': 'entry_signal_day'})
    x = x.merge(entries, on=['code', 'entry_signal_day'], how='left',
                suffixes=('', '_entry'), validate='many_to_one')
    require(x['rank_entry'].notna().all(), '实际入场未能对应前一交易日的入选决策')
    require(x['rank_entry'].le(20).all(), '新入场排名超过20')
    x['reason'] = x.pool_reason.where(x.exit_reason.eq('out_of_pool'), x.exit_reason)
    x['group'] = x.reason.map(group_reason)
    # 这是买入总额归一的已实现损益，包含再平衡，不能称为含分红总收益率。
    x['normalized_pnl'] = x.net_realized_pnl / x.buys
    require(np.isfinite(x.normalized_pnl).all(), '归一损益无效')
    x['period'] = pd.cut(x.exit_date.dt.year, [2015, 2020, 2023, 2026],
                         labels=['2016～2020', '2021～2023', '2024～2026'])
    require(x.period.notna().all(), '分期未覆盖输入区间')
    short = x[x.holding_days.le(20)].copy()
    require(not short.empty, '没有短持仓样本')
    all_groups, short_groups = aggregate(x, 'group'), aggregate(short, 'group')
    for frame in (all_groups, short_groups):
        frame['group_label'] = frame.group.map(LABELS)
        frame['sort'] = frame.group.map(dict(zip(GROUP_ORDER, range(len(GROUP_ORDER)))))
        frame.sort_values('sort', inplace=True)
        frame.drop(columns='sort', inplace=True)
        require(frame.episodes.sum() == (len(x) if frame is all_groups else len(short)),
                '原因分组数量未闭合')
    require(np.isclose(all_groups.net_realized_pnl.sum(), c.net_realized_pnl.sum()),
            '全部退出原因损益未闭合')
    require(np.isclose(short_groups.net_realized_pnl.sum(), short.net_realized_pnl.sum()),
            '短持仓原因损益未闭合')
    periods = aggregate(short, ['group', 'period'])
    periods['group_label'] = periods.group.map(LABELS)
    detailed = aggregate(x, 'reason').sort_values('episodes', ascending=False)
    detailed_short = aggregate(short, 'reason').sort_values('episodes', ascending=False)

    financial = short[short.group.eq('financial')].copy()
    for field in ('stat_date', 'stat_date_entry', 'pub_date', 'pub_date_entry'):
        financial[field] = pd.to_datetime(financial[field])
    financial['report_changed'] = financial.stat_date.gt(financial.stat_date_entry)
    financial['new_pub_after_entry_asof'] = financial.pub_date.gt(financial.asof_entry)
    financial['pub_before_entry_day'] = financial.pub_date.lt(financial.entry_date)
    financial['pub_on_entry_day'] = financial.pub_date.eq(financial.entry_date)
    rank_short = short[short.group.eq('rank_below_buffer')].copy()
    vol = x[x.group.eq('vol_high')].copy()
    vol['vol_bucket'] = pd.cut(vol.vol60, [35, 37, 40, 45, np.inf], right=False,
                               labels=['35～37', '37～40', '40～45', '45以上'])
    vol_bins = aggregate(vol, 'vol_bucket')
    rank_short['rank_bucket'] = pd.cut(rank_short['rank'], [30, 40, 50, 70, np.inf],
        labels=['31～40', '41～50', '51～70', '71以上'])
    rank_bins = aggregate(rank_short, 'rank_bucket')
    require(rank_bins.episodes.sum() == len(rank_short), '排名退出区间未闭合')
    entry_plans = d[d.action.eq('entry')].copy()
    entry_plans['execution_day'] = entry_plans.signal_day.map(next_day)
    entry_keys = set(zip(t[t.kind.eq('entry')].code, t[t.kind.eq('entry')].date))
    entry_plans['filled_next_day'] = [key in entry_keys for key in
                                    zip(entry_plans.code, entry_plans.execution_day)]
    require(entry_plans.filled_next_day.sum() == len(c) + len(opened), '实际新开仓数量未闭合')
    closed_signals = set(zip(h.code, h.signal_day))
    superseded = exits[[key not in closed_signals for key in zip(exits.code, exits.signal_day)]]
    files = ['pool_rotation_v1_decisions.csv', 'pool_rotation_v1_holding_periods.csv',
             'pool_rotation_v1_equity.csv', 'pool_rotation_v1_trades.csv']
    summary = {
        'date': '2026-10-05', 'frequency': 'day (用户确认)',
        'reference_start': str(e.date.iloc[0].date()), 'reference_end': str(e.date.iloc[-1].date()),
        'decision_rows': len(d), 'signal_dates': int(d.signal_day.nunique()),
        'actions': {key: int(value) for key, value in d.action.value_counts().items()},
        'closed_holding_periods': len(x), 'open_periods_from_reference_trades': len(opened),
        'holding_periods_matched_reference': True, 'holding_day_difference_max': 0,
        'exit_reason_matches_signal': True,
        'short_periods': len(short), 'short_share': len(short) / len(x),
        'short_net_realized_pnl': float(short.net_realized_pnl.sum()),
        'short_financial_report_updates': int(financial.report_changed.sum()),
        'short_financial_new_pub_after_entry_asof': int(financial.new_pub_after_entry_asof.sum()),
        'short_financial_pub_before_entry_day': int(financial.pub_before_entry_day.sum()),
        'short_financial_pub_on_entry_day': int(financial.pub_on_entry_day.sum()),
        'short_rank_entry_median': float(rank_short.rank_entry.median()),
        'short_rank_exit_median': float(rank_short['rank'].median()),
        'short_rank_exit_above50': int(rank_short['rank'].gt(50).sum()),
        'short_rank_entry_momentum_median': float(rank_short.momentum_entry.median()),
        'short_rank_exit_momentum_median': float(rank_short.momentum.median()),
        'vol_exit_near35_under37': int(vol.vol60.lt(37).sum()),
        'vol_exit_median': float(vol.vol60.median()),
        'entry_plans_without_next_day_fill': int((~entry_plans.filled_next_day).sum()),
        'exit_plans_without_separate_closure': len(superseded),
        'global_data_pause_decisions': int(d.action.eq('data_pause').sum()),
        'new_run_daily_nav_independently_verified': False,
        'sha256': {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in files},
    }
    frames = dict(closed_periods_with_reasons=x, all_exit_groups=all_groups,
        short_exit_groups=short_groups, detailed_exit_reasons=detailed,
        detailed_short_exit_reasons=detailed_short, short_groups_by_period=periods,
        short_financial_updates=financial, short_rank_exit_bins=rank_bins,
        volatility_exit_bins=vol_bins, entry_plans_without_fill=entry_plans[~entry_plans.filled_next_day],
        exit_plans_superseded=superseded)
    return summary, frames


def write_report(summary, frames):
    OUT.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_csv(OUT / (name + '.csv'), index=False, encoding='utf-8-sig')
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    labels = {'group_label': '退出原因', 'episodes': '持有期数', 'share': '占比',
              'net_realized_pnl': '已实现损益扣手续费（元）', 'win_rate': '盈利轮次占比'}
    all_table = markdown_table(frames['all_exit_groups'], labels,
                                percents=('share', 'win_rate'), money=('net_realized_pnl',))
    short_table = markdown_table(frames['short_exit_groups'], labels,
                                  percents=('share', 'win_rate'), money=('net_realized_pnl',))
    period_table = markdown_table(frames['short_groups_by_period'],
        {'group_label': '退出原因', 'period': '退出年份', 'episodes': '持有期数',
         'net_realized_pnl': '已实现损益扣手续费（元）',
         'median_normalized_pnl': '买入额归一损益中位数'},
        percents=('median_normalized_pnl',), money=('net_realized_pnl',))
    rank_table = markdown_table(frames['short_rank_exit_bins'],
        {'rank_bucket': '实际退出信号排名', 'episodes': '短持仓数',
         'net_realized_pnl': '已实现损益扣手续费（元）'}, money=('net_realized_pnl',))
    vol_table = markdown_table(frames['volatility_exit_bins'],
        {'vol_bucket': '触发退出的年化波动率区间（%）', 'episodes': '全部持有期数',
         'net_realized_pnl': '已实现损益扣手续费（元）'}, money=('net_realized_pnl',))
    report = f'''# 质量池轮动 v1 退出原因与改进顺序

2026-10-05，日频诊断。决策表3916行、130个信号日，实际闭合持有期1365轮。
与首轮2016-01-04～2026-09-01成交记录逐个核对：股票代码、开仓日、退出日及持有
交易日数全部一致；退出原因均能对到原始信号。原始文件保持不变，SHA256见汇总JSON。

本报告解释程序的筛选触发。后续已补上[价格路径报告](pool_rotation_v1_price_paths_2026-10-05.md)，
研究大亏损、回吐及卖出后表现；其证据修正了下面的候选优先级，尤其将统一波动留存
缓冲的优先级下调。下面保留仅按退出标签提出的研究假设，不作为已确认的改进。

## 结论

20日短持仓亏损不能据此归为“卖得太快”。774轮合计扣手续费亏损88,035.26元，
其中掉排名237轮亏损648,132.11元，财务不过线187轮亏损565,042.89元；波动越线
311轮盈利887,830.14元，估值越线32轮盈利246,300.00元。几类机制的方向相反。

优先核验新入场的财务连续性和财报更新，再分别验证排序与波动退出。当前证据不足以
支持统一延长持有期、放宽排名到50名，或直接加入双均线。原因组的既有损益不能回答
“延迟卖出后会怎样”，改善幅度必须由下一轮实际执行回测检验。

## 全部退出与短持仓

{all_table}

1365轮中，774轮持有不超过20个交易日，实际全部恰为20日，占56.7%。短持仓拆分如下：

{short_table}

损益来自首轮平台成交“平仓盈亏”，关联完整持有期内的减仓卖单，再扣该轮所有买卖
手续费。包含差额再平衡和平台成本处理，未单独计算分红；不等于账户收益贡献或个股
总收益率。不同年份账户资金不同，金额不能独立用于比较机制优劣。归一损益采用
“完整持有期净已实现损益/该轮所有买入成交额”，只用于辅助比较，也不是含分红收益率。

## 财务变化发生在入场后第一轮

187轮财务退出短持仓全部切到更晚一期的财报，且披露日期均晚于入场决策的数据截止日。
入场时ROE年化近似中位数16.48%、利润同比中位数42.69%；退出时分别为9.28%、3.57%。
说明不是简单把入场ROE门槛提高一点就能解释全部变化。这里记录的是筛选条件不过线，
不能将单季指标改变直接等同于公司长期质量恶化。

其中{summary['short_financial_pub_before_entry_day']}轮的新财报披露日期早于实际买入日，
{summary['short_financial_pub_on_entry_day']}轮与买入日相同。T日信号使用T-1快照、T+1才执行，
包含周末和假期造成的信息间隔。买入当天披露的具体盘中时点无法从日期判断，不能
把当天稍晚公布的财报提前用于09:31下单。

可分别检验两个候选：

1. 下单前核验新入场股票是否已有当时可见、更新后的财报；不过线者不新买。只有
   确认API可在该下单时点提供对应信息才执行检查，不能利用之后导出的披露记录回填。
   这主要处理信号到执行的间隔，不能覆盖买入后才披露的168轮。
2. 新入场增加财务连续性要求，例如当前与前一季度都通过既有ROE/增长条件；每期
   必须在信号截止时已披露。保持财务不过线的原有退出，不因稳定性假设而强行拖延。
   这会缩小可买数量、增加现金，也可能排除刚改善的公司，需要同时检查机会成本。

当前单次指标筛选的变化是否来自季节性、业务恶化或报告口径变化，需要额外季度
序列才能区分。TTM或多季度ROE可以单独研究，不能从这两张表直接算出或宣布更优。

## 掉排名的短持仓，不只是30名附近抖动

237轮排名退出短持仓的入场排名中位数12，下一轮退出信号排名中位数47；
{summary['short_rank_exit_above50']}轮已经跌到50名以外。前后120日收益中位数分别为
{summary['short_rank_entry_momentum_median']:.1%}与{summary['short_rank_exit_momentum_median']:.1%}。

{rank_table}

这些亏损在2016～2020、2021～2023、2024～2026三段都出现。扩大留存名额会让部分
已经转弱的股票持有更久，其后走势尚未核验。先固定20/30留存规则，只替换排序分数
做同口径对照；例如作为一个预先登记的候选，120日窗口内跳过最近20日，单独检验
近期上涨在当前排序中的作用。这里只提出候选，没有历史最优窗口或预期提升数值。
后续若确需检验排名缓冲，另开独立对照，避免把排序与留存同时改变。

## 波动越线退出，兼具风险过滤与卖出盈利仓位的作用

479轮波动退出的已实现净损益合计+2,362,114.02元；其中311轮20日退出净损益
+887,830.14元。退出时年化波动率中位数38.07%，{summary['vol_exit_near35_under37']}轮位于35%～37%。

{vol_table}

可单独检验“新入场仍小于35%，已有持仓小于40%可留存”的候选缓冲；40%是待验证
取值。仍需同时满足财务、估值、ST、解禁等其他条件，并遵守排名留存；原筛选在
波动环节失败后没有继续检查解禁，不能把原因标签当成其他条件全部通过的证据。

盈利仓位被退出，不证明退出错误。保持更高波动持仓可能继续盈利，也可能扩大
回撤；现有CSV没有规则改变后的完整持仓与现金路径，不能静态累加之后的股价涨幅。
估值越线47轮也多数盈利，样本较小，本轮不同时放宽PE/PB。

## 跨时期检查

下表按退出年份分段；2026年只有截至9月1日的样本。

{period_table}

排名组各段归一损益中位数均为负；财务组亏损以2021～2023较明显。波动退出组
2016～2020归一损益中位数为负，但合计金额为正，存在盈利分布偏斜，不能只看总利润。

## 过程与比较边界

新入选计划1408次，其中1385次对到次日实际新开仓，23次没有次日开仓。计划退出
1380次、实际闭合1365轮，多出的15条没有对应独立闭合，主要是持仓等待期间的重复
退出计划或被后续计划替代。没有记录整轮数据暂停。尚未收到events表，不能把23条
未开仓全部归为拒单，也不能判断每日阻塞是涨跌停、资金、停牌还是其他原因。

实际退出标签中停牌5轮、解禁14轮、财务行不可用1轮。标签反映最终对应退出信号
中的首个失败环节；例如一只股票可以先因PB越线进入待退队列、后来因停牌再次计划
退出，最终闭合标签成为paused。因此，最终标签不完整描述等待期间所有原因。

本轮仅新增两张过程表，未获得本轮nav/orders：已经验证持有期与首轮一致，但没有
独立验证新回测每天净值、全部成交价格和费用完全一致。上面的损益使用首轮成交
关联，后续改变规则应重新导出权益、实际成交及全部过程表，不能沿用旧净值估计。

## 下一轮实验安排

保留当前v1作为固定对照。建议先分别运行财务连续性入场、下单前财报核验、波动率
留存缓冲、排序分数对照；每次只改一个机制，保留相同区间、资金、费用和日频设置。
对比成本后年化、绝对/相对回撤、换手、平均现金、20日退出原因及各年份表现。
同时检查2018下跌、2020反弹和2024～2026相对回撤区间，避免只提高某一段历史利润。

已有数据支持研究假设，尚无优化后收益结果。全部历史已经参与诊断，分段检查不能
重新标为未见样本；后续前向数据另行跟踪。多次尝试后选择历史最好参数会引入选择
偏差，参见[回测过拟合研究](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。

## 复现与数据

`python -m research.studies.roe_quality.analyze_pool_rotation_diagnostics`

[汇总与文件指纹](pool_rotation_v1_diagnostics/summary.json)、
[逐轮关联表](pool_rotation_v1_diagnostics/closed_periods_with_reasons.csv)、
[短持仓原因表](pool_rotation_v1_diagnostics/short_exit_groups.csv)。
首轮完整收益与回撤分析见[原报告](pool_rotation_v1_daily_2026-10-05.md)。
'''
    REPORT.write_text(report, encoding='utf-8')


def main():
    summary, frames = analyze()
    write_report(summary, frames)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(frames['short_exit_groups'][['group_label', 'episodes', 'net_realized_pnl', 'win_rate']]
          .to_string(index=False))
    print('报告：', REPORT)


if __name__ == '__main__':
    main()
