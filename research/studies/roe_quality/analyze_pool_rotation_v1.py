r"""离线分析聚宽日频导出；不改交易规则，不获取行情，不执行参数优化。

仓库根目录：.\.conda\python.exe -m research.studies.roe_quality.analyze_pool_rotation_v1
原始CSV保持不变；报告及中间表写入本研究的reports/pool_rotation_v1_analysis/。
持有期依成交推断，送转增量只用于校验股票存在，不重建持仓成本或逐股日收益。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.tools.market_regimes import RegimeConfig, analyze_returns, read_index
from research.tools.market_regimes.data import read_csv
from research.tools.market_regimes.report import write_report

HERE = Path(__file__).resolve().parent
OUT = HERE / 'reports' / 'pool_rotation_v1_analysis'
INITIAL = 1_000_000.0
ANNUAL = 250
EQUITY_COLUMNS = ['date', 'benchmark_pct', 'strategy_pct', 'profit', 'loss',
                  'buy_amount', 'sell_amount', 'relative_pct', 'cash_pct',
                  'holdings', 'pending_exits', 'turnover_pct']
TRADE_COLUMNS = ['date', 'time', 'asset', 'name', 'side', 'order_type', 'quantity',
                 'price', 'amount', 'order_quantity', 'limit_price', 'realized_pnl',
                 'fee', 'status', 'updated']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_inputs():
    e = read_csv(HERE / 'pool_rotation_v1_equity.csv')
    t = read_csv(HERE / 'pool_rotation_v1_trades.csv')
    require(e.columns.tolist() == ['时间', '基准收益', '策略收益', '当日盈利', '当日亏损',
            '当日买入', '当日卖出', '超额收益(%)', 'cash_pct', 'holdings',
            'pending_exits', 'turnover_pct'], '收益表结构与本次导出不符')
    require(t.columns.tolist() == ['日期', '委托时间', '品种', '标的', '交易类型',
            '下单类型', '成交数量', '成交价', '成交额', '委托数量', '委托价格',
            '平仓盈亏', '手续费', '状态', '最后更新时间'], '成交表结构与本次导出不符')
    e.columns, t.columns = EQUITY_COLUMNS, TRADE_COLUMNS
    e['date'] = pd.to_datetime(e.date).dt.normalize().astype('datetime64[ns]')
    t['date'] = pd.to_datetime(t.date).dt.normalize().astype('datetime64[ns]')
    require(e.date.is_unique and e.date.is_monotonic_increasing, '收益日期重复或未排序')
    require(t.date.is_monotonic_increasing and not t.duplicated().any(), '成交未排序或存在重复行')
    require(np.isfinite(e.drop(columns='date').to_numpy(dtype=float)).all(), '收益表数值缺失')
    require((e.profit >= 0).all() and (e.loss <= 0).all(), '每日盈亏符号错误')
    require(t.status.eq('全部成交').all() and t.order_type.eq('市价单').all(), '本分析需要完整市价成交')
    t['code'] = t.name.str.extract(r'\((\d{6}\.(?:XSHG|XSHE))\)')
    require(t.code.notna().all(), '无法识别股票代码')
    t['qty'] = t.quantity.str.extract(r'^(-?\d+)股$').astype(int)
    ordered = t.order_quantity.str.extract(r'^(-?\d+)股$').astype(int)[0]
    require((t.qty == ordered).all(), '成交数量与委托数量不一致')
    require(((t.side.eq('买') & t.qty.gt(0)) | (t.side.eq('卖') & t.qty.lt(0))).all(), '买卖数量符号错误')
    require(np.allclose(t.qty * t.price, t.amount, atol=1e-6, rtol=0), '价量无法复原成交额')
    t['amount_abs'] = t.amount.abs()
    e['equity'] = INITIAL + (e.profit + e.loss).cumsum()
    e['strategy_nav'] = e.equity / INITIAL
    e['benchmark_nav'] = 1 + e.benchmark_pct / 100
    require(np.max(np.abs(e.strategy_nav - (1 + e.strategy_pct / 100))) <= 0.0000501,
            '初始资金或每日盈亏与累计收益不一致')
    e['relative_nav'] = e.strategy_nav / e.benchmark_nav
    require(np.max(np.abs((e.relative_nav - 1) * 100 - e.relative_pct)) <= 0.015,
            '平台累计超额与相对净值不一致')
    for prefix in ('strategy', 'benchmark', 'relative'):
        e[f'{prefix}_return'] = e[f'{prefix}_nav'] / e[f'{prefix}_nav'].shift(fill_value=1) - 1
        e[f'{prefix}_drawdown'] = e[f'{prefix}_nav'] / e[f'{prefix}_nav'].cummax().clip(lower=1) - 1
    e['year'] = e.date.dt.year
    day_index = dict(zip(e.date, e.index))
    t['day_index'] = t.date.map(day_index)
    require(t.day_index.notna().all(), '成交日不存在于收益表')
    t['year'] = t.date.dt.year
    t['off_cycle'] = t.day_index.mod(20).ne(1)
    # 最低佣金只约束佣金；印花税在此基础上另加。使用导出中的真实费用校验默认配置。
    tax = np.where(t.date >= pd.Timestamp('2023-08-28'), 0.0005, 0.001)
    expected_fee = np.maximum(t.amount_abs * 0.00025, 5) + np.where(t.side.eq('卖'), t.amount_abs * tax, 0)
    require(np.max(np.abs(expected_fee - t.fee)) <= 0.011, '成交费用与默认配置不一致')
    grouped = t.groupby('date')
    e['fee'] = e.date.map(grouped.fee.sum()).fillna(0)
    e['trade_notional'] = e.date.map(grouped.amount_abs.sum()).fillna(0)
    require(np.allclose(e.trade_notional / e.equity, e.turnover_pct / 100, atol=1e-7, rtol=0),
            '成交额与日换手无法对齐')
    for side, column in [('买', 'buy_amount'), ('卖', 'sell_amount')]:
        actual = e.date.map(t[t.side.eq(side)].groupby('date').amount.sum()).fillna(0)
        require(np.max(np.abs(actual - e[column])) <= 0.51, '每日买卖金额不一致')
    return e, t


def infer_holding_episodes(e, t):
    """数量非正时判断清仓；缺失送转数量只在清仓时记录推断，不制造行情或成本。"""
    quantities, active, closed, actions, labels, counts = {}, {}, [], [], [], []
    for row in t.itertuples():
        before = quantities.get(row.code, 0)
        after = before + row.qty
        if row.side == '买' and before == 0:
            kind = 'entry'
            active[row.code] = dict(code=row.code, name=row.name, entry=row.date,
                entry_index=int(row.day_index), buys=row.amount_abs, sells=0.0,
                realized_pnl=0.0, fee=row.fee, inferred_share_adjustment=False)
        elif row.side == '买':
            kind = 'topup'
            active[row.code]['buys'] += row.amount_abs
            active[row.code]['fee'] += row.fee
        else:
            require(before > 0 and row.code in active, '存在无法归属的卖单')
            kind = 'exit' if after <= 0 else 'trim'
            episode = active[row.code]
            episode['sells'] += row.amount_abs
            episode['realized_pnl'] += row.realized_pnl
            episode['fee'] += row.fee
            if after < 0:
                episode['inferred_share_adjustment'] = True
                actions.append(dict(date=row.date, code=row.code, inferred_extra_shares=-after))
            if after <= 0:
                episode.update(exit=row.date, exit_index=int(row.day_index),
                    trading_days=int(row.day_index) - episode['entry_index'],
                    calendar_days=(row.date - episode['entry']).days)
                closed.append(episode)
                del active[row.code]
                after = 0
        quantities[row.code] = after
        labels.append(kind)
        counts.append(sum(q > 0 for q in quantities.values()))
    t = t.copy()
    t['kind'], t['inferred_holdings'] = labels, counts
    observed = t.groupby('date').inferred_holdings.last().reindex(e.date).ffill().fillna(0).to_numpy()
    require(np.array_equal(observed, e.holdings.to_numpy()), '推断持仓数与平台持仓数不符')
    c = pd.DataFrame(closed)
    c['net_realized_pnl'] = c.realized_pnl - c.fee
    o = pd.DataFrame(active.values())
    require(np.isclose(c.realized_pnl.sum() + o.realized_pnl.sum(), t.realized_pnl.sum()), '持有期盈亏未闭合')
    require(np.isclose(c.fee.sum() + o.fee.sum(), t.fee.sum()), '持有期费用未闭合')
    return t, c, o, pd.DataFrame(actions)


def drawdown_episodes(e, prefix):
    peak, peak_date, peak_index, current, rows = 1.0, pd.Timestamp('2015-12-31'), -1, None, []
    for i, row in e.iterrows():
        value = row[f'{prefix}_nav']
        if value >= peak:
            if current is not None:
                current.update(recovery=row.date, recovery_trading_days=i - peak_index,
                    recovery_calendar_days=(row.date - peak_date).days)
                rows.append(current)
                current = None
            peak, peak_date, peak_index = value, row.date, i
        else:
            dd = value / peak - 1
            if current is None:
                current = dict(kind=prefix, peak=peak_date, start=row.date, trough=row.date,
                    max_drawdown=dd, peak_nav=peak, trough_nav=value,
                    recovery=pd.NaT, recovery_trading_days=np.nan, recovery_calendar_days=np.nan)
            elif dd < current['max_drawdown']:
                current.update(trough=row.date, max_drawdown=dd, trough_nav=value)
    if current is not None:
        rows.append(current)
    return pd.DataFrame(rows)


def make_tables(e, t, closed, regimes):
    years = []
    for year, f in e.groupby('year'):
        ts = t[t.year.eq(year)]
        local_nav = np.r_[1, np.cumprod(1 + f.strategy_return)]
        years.append(dict(year=year, days=len(f), strategy_return=np.prod(1 + f.strategy_return) - 1,
            benchmark_return=np.prod(1 + f.benchmark_return) - 1,
            relative_return=np.prod(1 + f.relative_return) - 1,
            within_year_drawdown=np.min(local_nav / np.maximum.accumulate(local_nav) - 1),
            average_cash_pct=f.cash_pct.mean(), average_holdings=f.holdings.mean(),
            turnover_two_sided=f.turnover_pct.sum() / 100, fee=ts.fee.sum(),
            strategy_log_contribution=np.log1p(f.strategy_return).sum(),
            relative_log_contribution=np.log1p(f.relative_return).sum()))
    annual = pd.DataFrame(years)
    types = t.groupby('kind').agg(orders=('kind', 'size'), notional=('amount_abs', 'sum'),
        fee=('fee', 'sum'), realized_pnl=('realized_pnl', 'sum')).reset_index()
    duration = closed.assign(bucket=pd.cut(closed.trading_days, [0, 20, 40, 80, 10000],
        labels=['20日以内', '21～40日', '41～80日', '80日以上'], include_lowest=True))
    duration = duration.groupby('bucket', observed=True).agg(episodes=('code', 'size'),
        gross_realized_pnl=('realized_pnl', 'sum'), fee=('fee', 'sum'),
        net_realized_pnl=('net_realized_pnl', 'sum'),
        win_rate=('net_realized_pnl', lambda x: (x > 0).mean())).reset_index()
    stocks = t.groupby('code').agg(name=('name', 'last'), orders=('code', 'size'),
        realized_pnl=('realized_pnl', 'sum'), fee=('fee', 'sum'), notional=('amount_abs', 'sum')).reset_index()
    prev = closed.groupby('code').exit_index.apply(list).to_dict()
    reentries = []
    for row in t[t.kind.eq('entry')].itertuples():
        previous = [i for i in prev.get(row.code, []) if i < row.day_index]
        if previous:
            reentries.append(dict(date=row.date, code=row.code, gap_trading_days=int(row.day_index - max(previous))))
    blocks = e.pending_exits.gt(0).ne(e.pending_exits.gt(0).shift(fill_value=False)).cumsum()
    pending = []
    for _, f in e[e.pending_exits.gt(0)].groupby(blocks):
        later = t[t.date.gt(f.date.iloc[-1])]
        next_day = e[e.date.gt(f.date.iloc[-1])].date.iloc[0]
        orders = later[later.date.eq(next_day) & later.side.eq('卖') & later.off_cycle]
        pending.append(dict(start=f.date.iloc[0], end=f.date.iloc[-1], trading_days=len(f),
            off_cycle_exit_next_day=';'.join(orders.name), average_cash_pct=f.cash_pct.mean()))
    weak = []
    for label, mask in [('全期', e.index >= 0), ('剔除2018', e.year.ne(2018)),
                        ('2016～2020', e.year.le(2020)), ('2021～2026', e.year.ge(2021))]:
        f = regimes.daily.loc[mask & regimes.daily.state.eq('weak_weak')]
        weak.append(dict(sample=label, days=len(f), conditional_annualized=np.expm1(
            np.log1p(f.strategy_return).mean() * ANNUAL)))
    return dict(annual=annual, trade_types=types, duration=duration, stock_realized_pnl=stocks,
        reentries=pd.DataFrame(reentries), pending_exit_blocks=pd.DataFrame(pending),
        weak_state_robustness=pd.DataFrame(weak), drawdowns=pd.concat([
            drawdown_episodes(e, 'strategy'), drawdown_episodes(e, 'relative')], ignore_index=True))


def markdown_table(frame, labels, percents=(), money=()):
    lines = ['| ' + ' | '.join(labels.values()) + ' |', '| ' + ' | '.join(['---'] * len(labels)) + ' |']
    for _, row in frame.iterrows():
        values = []
        for col in labels:
            v = row[col]
            if pd.isna(v):
                value = '—'
            elif col in percents:
                value = f'{v:.2%}'
            elif col in money:
                value = f'{v:,.2f}'
            elif isinstance(v, pd.Timestamp):
                value = v.strftime('%Y-%m-%d')
            elif col in ('year', 'days', 'episodes', 'orders', 'trading_days'):
                value = str(int(v))
            else:
                value = str(v)
            values.append(value.replace('|', '\\|'))
        lines.append('| ' + ' | '.join(values) + ' |')
    return '\n'.join(lines)


def write_analysis(e, t, closed, opened, adjustments, regimes, tables):
    OUT.mkdir(parents=True, exist_ok=True)
    exports = dict(tables, daily=e, trades=t, closed_holding_episodes=closed,
        open_holding_episodes=opened, inferred_share_adjustments=adjustments,
        regime_summary=regimes.summary, regime_year_states=regimes.year_states,
        regime_episodes=regimes.episodes)
    for name, frame in exports.items():
        frame.to_csv(OUT / f'{name}.csv', index=False, encoding='utf-8-sig')
    years = len(e) / ANNUAL
    absolute = tables['drawdowns'].query("kind == 'strategy'").nsmallest(1, 'max_drawdown').iloc[0]
    relative = tables['drawdowns'].query("kind == 'relative'").nsmallest(1, 'max_drawdown').iloc[0]
    value = e.set_index('date')
    ap, at = value.loc[absolute.peak], value.loc[absolute.trough]
    rp, rt = value.loc[relative.peak], value.loc[relative.trough]
    normalized_fee = (e.fee / e.equity).sum() / years
    turnover = e.turnover_pct.sum() / 100 / years
    residual = e.equity.iloc[-1] * e.cash_pct.iloc[-1] / 100 - (INITIAL - t.amount.sum() - t.fee.sum())
    short = closed[closed.trading_days.le(20)]
    dd_sells = t[t.date.gt(absolute.peak) & t.date.le(absolute.trough) & t.side.eq('卖')]
    dd_stock = dd_sells.groupby('code').realized_pnl.sum().sort_values()
    long_contribution = closed.loc[closed.trading_days.gt(40), 'net_realized_pnl'].sum() / closed.net_realized_pnl.sum()
    summary = dict(recorded='2026-10-05', frequency='day (用户确认)', initial_cash=INITIAL,
        start=str(e.date.iloc[0].date()), end=str(e.date.iloc[-1].date()), days=len(e),
        trade_orders=len(t), distinct_stocks=int(t.code.nunique()), annualization_days=ANNUAL,
        cumulative_return=float(e.strategy_nav.iloc[-1] - 1), benchmark_return=float(e.benchmark_nav.iloc[-1] - 1),
        cumulative_relative_return=float(e.relative_nav.iloc[-1] - 1),
        annual_return=float(e.strategy_nav.iloc[-1] ** (1 / years) - 1),
        annual_relative_return=float(e.relative_nav.iloc[-1] ** (1 / years) - 1),
        absolute_max_drawdown=float(absolute.max_drawdown), relative_max_drawdown=float(relative.max_drawdown),
        absolute_recovery=str(absolute.recovery.date()), average_cash_pct=float(e.cash_pct.mean()),
        average_cash_after_2016_pct=float(e.loc[e.year.gt(2016), 'cash_pct'].mean()),
        turnover_two_sided_per_year=float(turnover), total_fee=float(t.fee.sum()),
        approximate_fee_cost_per_year=float(normalized_fee),
        approximate_slippage_cost_per_year=float(turnover * 0.001),
        closed_episode_count=len(closed), closed_episode_win_rate=float(closed.net_realized_pnl.gt(0).mean()),
        short_episode_share=float(len(short) / len(closed)), short_episode_net_pnl=float(short.net_realized_pnl.sum()),
        non_trade_cash_flow_residual=float(residual),
        sha256={str(path.relative_to(HERE)): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                [HERE/'pool_rotation_v1_equity.csv', HERE/'pool_rotation_v1_trades.csv', HERE/'_cache/sz399317_daily.csv']})
    (OUT/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    annual_table = markdown_table(tables['annual'], {'year':'年份', 'days':'日数', 'strategy_return':'策略',
        'benchmark_return':'基准', 'relative_return':'相对收益', 'within_year_drawdown':'年内回撤'},
        ['strategy_return', 'benchmark_return', 'relative_return', 'within_year_drawdown'])
    duration_table = markdown_table(tables['duration'], {'bucket':'完整持有期', 'episodes':'次数',
        'gross_realized_pnl':'平仓盈亏合计', 'fee':'手续费', 'net_realized_pnl':'扣费后合计', 'win_rate':'扣费胜率'},
        ['win_rate'], ['gross_realized_pnl', 'fee', 'net_realized_pnl'])
    regime_table = markdown_table(regimes.summary, {'state_label':'已知市场状态', 'days':'日数',
        'strategy_annualized':'条件折算年化', 'excluded_years_annualized':'剔除2018后'},
        ['strategy_annualized', 'excluded_years_annualized'])
    pending_table = markdown_table(tables['pending_exit_blocks'], {'start':'待退出开始', 'end':'最后待退出日',
        'trading_days':'持续交易日', 'off_cycle_exit_next_day':'次日轮外卖出'})
    top = tables['stock_realized_pnl'].nlargest(5, 'realized_pnl')
    bottom = tables['stock_realized_pnl'].nsmallest(5, 'realized_pnl')
    stocks_table = markdown_table(pd.concat([top, bottom]), {'name':'股票', 'realized_pnl':'平仓盈亏合计',
        'fee':'该股全部交易手续费'}, money=['realized_pnl', 'fee'])
    ann = tables['annual'].set_index('year')
    concentration = ann.loc[[2016, 2017, 2021], 'relative_log_contribution'].sum() / ann.relative_log_contribution.sum()
    report = f'''# 质量池轮动 v1：CSV归因及改进方向

分析日期：2026-10-05。**建议继续一轮有边界的研究，优先检查频繁整仓替换和组合风险，120日窗口调参放在后面。**
这次CSV揭示了两个独立问题：弱市满仓造成绝对回撤，近期又出现持续的相对落后。
现有数据还不能证明120日排序有增量预测力。

## 数据与复核

- 原始数据：[每日权益](../pool_rotation_v1_equity.csv)、[成交表](../pool_rotation_v1_trades.csv)。
- 实际覆盖{summary['start']}～{summary['end']}，{len(e)}个交易日，{len(t)}笔成交，
  {summary['distinct_stocks']}只不同股票；初始资金100万元与每日金额及累计百分比相符。
- 用户确认回测频率为“天”。全部导出成交是09:31市价单，状态均为全部成交；这不包括
  下单前被策略跳过的候选或未形成委托的卖出阻塞，不能据此推断执行没有障碍。
- 由初始资金加每日盈利/亏损复原的期末权益为{e.equity.iloc[-1]:,.2f}元；与累计收益列
  对应的权益差异不超过50元，符合收益列只保留两位百分数的精度。分析使用金额复原净值。
- 校验通过：日期完整性、成交价×有符号数量=成交额、成交额=逐日买卖金额、默认费用、
  换手对齐、全期推断持仓数=平台持仓数、持有期平仓盈亏与费用分别闭合。
- 基准按原始累计收益还原，保留首日−7.42%；首日策略未持股。若把基准首日重新归一为1，
  会删除首日避跌超额。本报告没有这样做。
- 年化按250交易日/年，能复现平台7.97%；旧脚本245日、裸池20日期末采样另属不同口径。

## 结果及两个回撤问题

策略累计{summary['cumulative_return']:.2%}，基准{summary['benchmark_return']:.2%}，
相对累计{summary['cumulative_relative_return']:.2%}；策略年化{summary['annual_return']:.2%}，
相对年化{summary['annual_relative_return']:.2%}。平台夏普0.221、超额夏普0.165仍按平台原值记录，
不与“日收益差均值/标准差”混用。

**绝对最大回撤：{absolute.peak:%Y-%m-%d}～{absolute.trough:%Y-%m-%d}，{absolute.max_drawdown:.2%}。**
同一峰谷区间基准{at.benchmark_nav/ap.benchmark_nav-1:.2%}；组合平均现金
{e.loc[e.date.gt(absolute.peak)&e.date.le(absolute.trough),'cash_pct'].mean():.2f}%，平均持仓
{e.loc[e.date.gt(absolute.peak)&e.date.le(absolute.trough),'holdings'].mean():.2f}只。
直到{absolute.recovery:%Y-%m-%d}才回到此前高点，历时{int(absolute.recovery_trading_days)}个交易日。
这是基本满仓时与市场同步承压的证据，不能把质量和单股低波过滤视为组合防跌机制。
区间内发生卖出的{len(dd_stock)}只股票中，{int(dd_stock.lt(0).sum())}只平仓盈亏合计为负；
亏损最大的五只只占这组负平仓盈亏的{dd_stock.head(5).sum()/dd_stock[dd_stock.lt(0)].sum():.1%}。
亏损具有分散性；没有每日逐股持仓价格，不能把这张已实现盈亏表当作完整回撤归因。

**相对最大回撤：{relative.peak:%Y-%m-%d}～{relative.trough:%Y-%m-%d}，{relative.max_drawdown:.2%}。**
这段策略{rt.strategy_nav/rp.strategy_nav-1:.2%}，基准{rt.benchmark_nav/rp.benchmark_nav-1:.2%}，
平均现金{e.loc[e.date.between(relative.peak,relative.trough),'cash_pct'].mean():.2f}%。
它主要表现为基本满仓却跟不上基准，而非资金未投入。到样本结束，相对净值仍比历史高点低
{abs(e.relative_drawdown.iloc[-1]):.2%}。单独减少熊市仓位不能回答这一段的选股和风格问题。

## 年度分布

{annual_table}

2026只到9月1日。年度从上一年末净值开始计算，年内回撤含该起点；不代表各年独立起跑回测。
2016、2017、2021合计占全期相对对数收益的{concentration:.1%}，其余年份正负抵消后贡献较小。
2019明显落后，2024/2025全年相对收益接近零，2026截至9月1日相对亏损约6.91%。
全期正超额支持保留研究方向，但不能据此确认近期收益来源仍然稳定。

## 交易结构：20日淘汰与跨轮留存

按股票代码的买卖数量推断持有期：首次开仓至完全退出为一轮，期间补仓/减持仍属同一轮。
发现{len(adjustments)}次卖出数量超出成交账本可解释数量，记录为可能的送转数量调整；
不猜测其成本。推断股票存在状态与全部{len(e)}天的持仓数一致，但企业行动及逐股持仓未独立核对。

已结束{len(closed)}轮，另有{len(opened)}轮期末仍在持有。结束轮的持有期中位数
{closed.trading_days.median():.0f}交易日、均值{closed.trading_days.mean():.2f}交易日。

{duration_table}

金额为元，扣费后合计=平台平仓盈亏合计−该轮买卖手续费，含平台持仓成本调整的影响；
未单列分红、未含期末浮盈，不与总权益收益强行相加。

- {len(short)}轮、占{len(short)/len(closed):.1%}，在20个交易日内退出。这组平仓盈亏接近零，
  手续费后合计{short.net_realized_pnl.sum():,.2f}元。短持有期淘汰是明确的效率检查入口。
- 持有超过40交易日的轮次占{closed.trading_days.gt(40).mean():.1%}，贡献已结束轮扣费平仓盈亏
  合计的{long_contribution:.1%}。这里存在结果选择：盈利者更可能留存，不能推导出
  “强制持有40天就会赚钱”，也不能放宽财务失效或交易风险退出。
- 平台979次盈利、748次亏损之和等于1727笔卖单。卖单胜率约56.7%；等权减持卖单
  胜率约88.1%，完整持有期扣费胜率约{closed.net_realized_pnl.gt(0).mean():.1%}。
  这些统计对象不同，完整持有期胜率较低也不否定趋势策略，仍须结合盈亏金额。
- 20交易日内重新买回的股票有{tables['reentries'].gap_trading_days.le(20).sum()}次，40日内
  {tables['reentries'].gap_trading_days.le(40).sum()}次。是否由临界出池、排名波动或其他因素造成，
  需要成员和退出原因记录。

## 换手与费用

- 全期双边成交额{t.amount_abs.sum():,.2f}元；按每日成交额/当日权益累计，双边换手
  折合每年{turnover:.2f}倍。这里每次买和卖都计入，不能当成单边换手。
- 实际手续费{t.fee.sum():,.2f}元。按每日权益归一化费用再除交易年数，成本量级约
  {normalized_fee:.2%}/年。默认单边0.1%滑点对应的换手成本量级约{turnover*.001:.2%}/年，
  合计约{normalized_fee+turnover*.001:.2%}/年；这是摩擦量级估算，不是无成本回测年化差。
  当前净值已含模拟费用及滑点，不应再重复扣除这些估算。
- 新开仓/完整退出占成交额{t.loc[t.kind.isin(['entry','exit']),'amount_abs'].sum()/t.amount_abs.sum():.1%}；
  等权补仓/减持仅占{t.loc[t.kind.isin(['topup','trim']),'amount_abs'].sum()/t.amount_abs.sum():.1%}。
  小额等权调整手续费合计{t.loc[t.kind.isin(['topup','trim']),'fee'].sum():,.2f}元，
  仅占总手续费{t.loc[t.kind.isin(['topup','trim']),'fee'].sum()/t.fee.sum():.1%}。
  因此只过滤100股微调不会解决主要换手问题，应优先调查整仓替换的原因。
- 原股票池的“期均成员换手50%”是集合变化统计，与这里实际账户双边换手不同。

## 待退出风险

{pending_table}

全期{e.pending_exits.gt(0).sum()}个交易日有待退出仓，实际存在7次轮外卖出。
最长连续待退出102个交易日。后续成交状态全部成功并不能消除此前无法退出的风险。
名单中的卖出只是队列解除次日的轮外成交对应关系；缺事件表，未确认停牌、跌停或其他原因。
不能承诺普通止损规则可以及时退出这些股票。

## 市场状态诊断及稳健性

复用仓库已有国证A指10月/20周状态模块，使用上一已结束周期的信息；没有搜索窗口或
执行择时规则。状态指数来自本地缓存，日期与收益表逐日匹配；基准百分比的舍入会造成
很小的日收益差异。条件折算年化用于诊断，不是加上门控后的策略年化。

{regime_table}

月弱/周弱状态全期条件年化约−4.50%，剔除2018却转为约+7.59%；2016～2020约−8.34%，
2021～2026约−0.85%。2018主导其负贡献，不能据此宣布双弱清仓具有稳定优势。
月弱/周强反弹状态的收益为正，简单按月线弱势持续空仓可能错失反弹。
市场条件可以作为组合风险假设，尚需执行回测、过渡时点及多个时期验证。

## 个股平仓盈亏观察

{stocks_table}

这是全期已实现盈亏观察，包含不同资金规模、不同持有轮次和平台成本调整；不含完整
期末持仓收益，不能作为行业贡献或选股因子的证据。未经验证，不据此建立个股黑名单。

## 数据能回答和仍缺的部分

成交与手续费不能独立复原期末现金，存在{residual:,.2f}元非交易现金流残差，可能来自
分红或其他企业行动现金处理；来源尚未核对，不能直接写成分红收益贡献。
以此提醒后续保持分红、送转和持仓账本，而非从卖单盈亏重建整个账户收益。

首轮分析时未获得策略导出的`nav/signals/members/events/orders`五张过程表，也没有逐日逐股
持仓市值和行业。CSV可判断净值、成交结构和退出队列，无法直接分解以下因果问题：
短持有期退出是出池还是掉排名、入选股票是否胜过未入选者、行业集中度、是否出现
整轮数据暂停，以及日频与分钟撮合的实际差异。120日排序增益仍待同口径对照。

后续新增的决策与持有期CSV已用于核验退出原因及数据暂停，见
[退出原因补充报告](pool_rotation_v1_diagnostics_2026-10-05.md)。以下保留首轮数据下的
研究顺序，具体候选以补充报告为准；不将短持仓全部亏损直接当成应延长持仓的证据。

## 改进方向及验证顺序

| 优先级 | 方向 | 这次数据给出的理由 | 下一项具体验证 |
|---|---|---|---|
| 1 | 检查20日整仓淘汰，增加必要的留存稳定性 | 56.7%完整持有期只持续20日，扣费后亏损；98.6%成交额来自整仓替换 | 先用成员和事件表区分出池、跌出30名、数据问题；对主要非财务风险原因单独试留存缓冲 |
| 2 | 为本策略设计组合风险预算 | 2018峰谷跌幅与基准接近，期间现金不足1% | 固定选股规则，单独检验仓位调整的收益/回撤权衡；核对反弹参与和实际换手 |
| 3 | 验证近期选股效率及120日排序贡献 | 2024-07～2026-06满仓但远落后，前三个贡献年份占全期相对对数收益95.3% | 保持池、历史要求、20/30留存、费用及现金规则一致，只替换排序分数并做多次对照；按年份和连续区间比较 |
| 4 | 完成执行与记录核验 | 待退出最长102日，只有日频成交 | 补齐原因、持仓权重和企业行动台账；对同起点同配置区间核对分钟订单 |

第1项的缓冲应与财务失效、ST和已知重大交易风险分开处理；只保护仍合格或处于可容忍
技术边界的持仓，不因长持有组赚钱就延迟全部退出。均线入场、止损、缩短动量窗口、
延长检查周期等都还只是候选，不能从这些CSV直接选出最优参数。

先保持本版作为基准，再一次改变一个机制。接受改进需看到成本后收益/回撤的实际
改善，并检查不同年份和反弹区间；只救2018或只抬高全期终值不够。全段历史已经看过，
后续历史切片可检验稳定性，不能重新称为未见样本；新数据及前向跟踪另行验证。
多次尝试后挑历史最优参数会产生选择偏差，参见[回测过拟合研究](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。
日频市价采用当日开盘价加滑点撮合，参见[聚宽官方手册](https://cdn.joinquant.com/help/img/JoinQuantAPI.pdf)。

## 复现及产物

```powershell
& '.\\.conda\\python.exe' -m research.studies.roe_quality.analyze_pool_rotation_v1
```

[交互净值/回撤/市场状态图](pool_rotation_v1_analysis/report.html)可按日期放大查看。
中间表、原始文件SHA256和汇总位于`pool_rotation_v1_analysis/`，分析脚本不修改原始CSV或交易代码。
'''
    record = HERE/'reports'/'pool_rotation_v1_daily_2026-10-05.md'
    record.write_text(report, encoding='utf-8')
    meta = dict(index_name='国证A指', benchmark_name='平台中证800', exclude_years=[2018],
        trading_days_per_year=ANNUAL, month_window=10, week_window=20,
        source='_cache/sz399317_daily.csv（新浪，2026-10-05 取），无网络刷新')
    write_report([dict(name='质量池轮动 v1（日频成交）', daily=regimes.daily,
        summary=regimes.summary, episodes=regimes.episodes)], OUT, meta)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('报告：', record)
    print('图表：', OUT/'report.html')


def main():
    e, t = load_inputs()
    t, closed, opened, adjustments = infer_holding_episodes(e, t)
    returns = e[['date', 'strategy_return', 'benchmark_return']]
    regimes = analyze_returns(returns, read_index(HERE/'_cache/sz399317_daily.csv'),
        RegimeConfig(trading_days_per_year=ANNUAL, exclude_years=(2018,)))
    tables = make_tables(e, t, closed, regimes)
    write_analysis(e, t, closed, opened, adjustments, regimes, tables)


if __name__ == '__main__':
    main()
