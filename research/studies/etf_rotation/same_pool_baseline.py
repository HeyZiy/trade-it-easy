# -*- coding: utf-8 -*-
"""同池被动基线：历史可用成员等权，每月首个交易日再平衡。

聚宽新建策略，粘贴整个文件：2016-01-04 至 2026-09-30，初始资金100000。
频率选分钟；同池排名轮动对照版须用相同日期、资金、频率重新运行后比较。
本文件没有本地回测结果，实际收益以聚宽完整回测为准。

规则固定，不按历史收益挑选成员或权重：
  * 使用下方 ETF_POOL 的8只ETF；当时已上市、至少有25日完整历史才纳入。
  * 每月首个交易日10:40，将可用成员恢复等权；无动量、止损或排名。
  * 新ETF在满足历史要求后的下次月度再平衡纳入，不回填上市前行情。
  * 已纳入的暂停交易成员保留目标份额，暂时不能成交的部分留在原持仓/现金。
  * 佣金万二、最低5元，默认零滑点，与原策略相同；现金不另计利息。
  * 年度收益按账户净值计算；回撤含期初资金，只使用每日收盘净值。
  * 费用压力测试：SLIPPAGE_PER_SIDE=0.0005 / 0.001，原策略同步改为
    set_slippage(PriceRelatedSlippage(2 * SLIPPAGE_PER_SIDE)) 后重新对比。

这是同池机会成本对照；其分散持仓与原策略单只持仓的风险不同，不能将
两者收益之差完全归因为择时能力。应同时比较收益、回撤和仓位。
"""

from jqdata import *
import builtins as py
import math


ETF_POOL = (
    '510300.XSHG',  # 沪深300
    '159915.XSHE',  # 创业板
    '515180.XSHG',  # 中证红利（100红利）
    '513500.XSHG',  # 标普500
    '513100.XSHG',  # 纳斯达克100
    '518880.XSHG',  # 黄金
    '511090.XSHG',  # 30年国债
    '511010.XSHG',  # 5年期国债，有久期风险，不是现金
)
HISTORY_DAYS = 25
TRADE_TIME = '10:40'
COMMISSION = 0.0002
MIN_COMMISSION = 5.0
SLIPPAGE_PER_SIDE = 0.0
ROUND_LOT = 100


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_order_cost(OrderCost(
        open_tax=0, close_tax=0,
        open_commission=COMMISSION, close_commission=COMMISSION,
        close_today_commission=0, min_commission=MIN_COMMISSION,
    ), type='fund')
    set_slippage(PriceRelatedSlippage(2 * SLIPPAGE_PER_SIDE))
    g.rebalance_month = None
    g.eligible = set()
    g.year_stats = {}
    g.previous_value = float(context.portfolio.starting_cash)
    g.total_peak = g.previous_value
    g.total_drawdown = 0.0
    g.last_stats_date = None
    run_daily(rebalance_task, time=TRADE_TIME, reference_security='000300.XSHG')
    run_daily(after_market_close, time='15:01')
    log.info('[同池基线] 历史可用成员等权，月初%s再平衡；单边滑点%.3f%%'
             % (TRADE_TIME, SLIPPAGE_PER_SIDE * 100))


def eligible_members(context):
    """历史不足不入池；已满足条件的成员不因短期数据缺失被重新分配权重。"""
    members = []
    for code in ETF_POOL:
        info = get_security_info(code, date=context.previous_date)
        if info is None or info.start_date > context.previous_date:
            continue
        if info.end_date < context.current_dt.date():
            continue
        if code not in g.eligible:
            hist = attribute_history(code, HISTORY_DAYS, '1d', ['close'],
                                     skip_paused=False, fq='pre')
            if hist is None or len(hist) < HISTORY_DAYS:
                continue
            if not py.all(math.isfinite(float(px)) and float(px) > 0
                          for px in hist['close']):
                continue
            g.eligible.add(code)
            log.info('[纳入基线] %s %s，历史已满%d日'
                     % (context.current_dt.date(), code, HISTORY_DAYS))
        members.append(code)
    return members


def current_amount(context, code):
    position = context.portfolio.positions.get(code)
    return int(position.total_amount) if position is not None else 0


def submit_target(code, target):
    """每个方向只提交一次；记录实际成交，避免失败后重复提交委托。"""
    try:
        result = order_target(code, int(target))
    except Exception as error:
        log.error('[委托失败] %s 目标%d份: %s' % (code, target, error))
        return
    if result is None:
        log.info('[委托未成交] %s 目标%d份' % (code, target))
    else:
        log.info('[委托] %s 目标%d份，已成交%s份'
                 % (code, target, result.filled))


def rebalance_task(context):
    today = context.current_dt.date()
    month = (today.year, today.month)
    if g.rebalance_month == month:
        return
    members = eligible_members(context)
    g.rebalance_month = month
    if not members:
        log.info('[月度再平衡] %s 无可用成员，保持现金' % today)
        return

    total = float(context.portfolio.total_value)
    target_value = total / len(members)
    data = get_current_data()
    targets = {}
    prices = {}
    for code in members:
        snapshot = data[code]
        price = float(snapshot.last_price)
        if snapshot.paused or not math.isfinite(price) or price <= 0:
            log.info('[跳过交易] %s 暂停交易或缺少有效价格，保留其目标预算' % code)
            continue
        prices[code] = price
        targets[code] = int(target_value / price / ROUND_LOT) * ROUND_LOT

    # 先卖后买；部分成交/卖出失败后，以实际剩余持仓和可用资金继续计算。
    for code, target in targets.items():
        held = current_amount(context, code)
        if held <= target:
            continue
        position = context.portfolio.positions[code]
        closeable = int(position.closeable_amount)
        sell = py.min(held - target, closeable)
        if sell > 0:
            submit_target(code, held - sell)

    # 各成员按可用买入资金同比例缩减，避免资金不足时由代码顺序决定配置。
    deficits = {}
    required = 0.0
    for code, target in targets.items():
        deficit = py.max(0, target - current_amount(context, code))
        if deficit >= ROUND_LOT:
            deficits[code] = deficit
            cost = deficit * prices[code] * (1 + SLIPPAGE_PER_SIDE)
            required += cost + py.max(MIN_COMMISSION, cost * COMMISSION)
    if required > 0:
        scale = py.min(1.0, float(context.portfolio.available_cash) / required)
        for code, deficit in deficits.items():
            buy = int(deficit * scale / ROUND_LOT) * ROUND_LOT
            price = prices[code] * (1 + SLIPPAGE_PER_SIDE)
            cash = float(context.portfolio.available_cash)
            affordable = int(py.max(0.0, cash - MIN_COMMISSION)
                             / (price * (1 + COMMISSION)) / ROUND_LOT) * ROUND_LOT
            buy = py.min(buy, affordable)
            if buy >= ROUND_LOT:
                submit_target(code, current_amount(context, code) + buy)

    log.info('[月度再平衡] %s 可用成员%d只，每只目标%.2f%%，实际现金%.2f元'
             % (today, len(members), 100.0 / len(members),
                context.portfolio.available_cash))


def record_execution_metrics(context, previous_value):
    """单边换手=(当日买入+卖出成交额)/(2×前次收盘净值)，不含费用。"""
    turnover = float('nan')
    exposure = float('nan')
    try:
        previous_value = float(previous_value)
        if not math.isfinite(previous_value) or previous_value <= 0:
            raise ValueError('前次收盘净值无效')
        traded = 0.0
        for trade in get_trades().values():
            price = float(trade.price)
            amount = abs(float(trade.amount))
            if not math.isfinite(price) or price <= 0 or not math.isfinite(amount):
                raise ValueError('实际成交价格或数量无效')
            traded += price * amount
        if not math.isfinite(traded):
            raise ValueError('实际成交额无效')
        turnover = traded / (2.0 * previous_value) * 100.0
    except Exception as error:
        log.error('[副图换手缺失] %s' % error)
    try:
        total = float(context.portfolio.total_value)
        if not math.isfinite(total) or total <= 0:
            raise ValueError('账户净值无效')
        invested = 0.0
        for position in context.portfolio.positions.values():
            if position.total_amount <= 0:
                continue
            value = float(position.value)
            if not math.isfinite(value) or value < 0:
                raise ValueError('实际持仓市值无效')
            invested += value
        if not math.isfinite(invested):
            raise ValueError('实际持仓总市值无效')
        exposure = invested / total * 100.0
    except Exception as error:
        log.error('[副图仓位缺失] %s' % error)
    record(**{'单边换手%': turnover, '实际仓位%': exposure})


def after_market_close(context):
    today = context.current_dt.date()
    if g.last_stats_date == today:
        return
    total = float(context.portfolio.total_value)
    if not math.isfinite(total) or total <= 0:
        raise ValueError('账户净值无效，停止统计，不能将缺失净值按零计算')
    year = today.year
    if year not in g.year_stats:
        if g.year_stats:
            print_year_stats(py.sorted(g.year_stats)[-1])
        g.year_stats[year] = {
            'start': g.previous_value, 'start_date': today,
            'peak': g.previous_value, 'drawdown': 0.0, 'days': 0,
            'holding_days': {}, 'weight_sum': {}, 'cash_weight_sum': 0.0,
        }
    stats = g.year_stats[year]
    stats['end'] = total
    stats['end_date'] = today
    stats['peak'] = py.max(stats['peak'], total)
    stats['drawdown'] = py.min(stats['drawdown'], total / stats['peak'] - 1)
    stats['days'] += 1
    invested = 0.0
    for code, position in context.portfolio.positions.items():
        if position.total_amount <= 0:
            continue
        value = float(position.value)
        if not math.isfinite(value) or value < 0:
            raise ValueError('%s 持仓市值无效' % code)
        invested += value
        stats['holding_days'][code] = stats['holding_days'].get(code, 0) + 1
        stats['weight_sum'][code] = stats['weight_sum'].get(code, 0.0) + value / total
    stats['cash_weight_sum'] += (total - invested) / total
    g.total_peak = py.max(g.total_peak, total)
    g.total_drawdown = py.min(g.total_drawdown, total / g.total_peak - 1)
    record_execution_metrics(context, g.previous_value)
    g.previous_value = total
    g.last_stats_date = today


def print_year_stats(year):
    stats = g.year_stats[year]
    result = (stats['end'] / stats['start'] - 1) * 100
    log.info('=' * 65)
    log.info('%d年同池基线（%s 至 %s）' % (year, stats['start_date'], stats['end_date']))
    log.info('年度收益率: %+.2f%% | 收益额: %+.2f元 | 年内最大回撤: %.2f%%'
             % (result, stats['end'] - stats['start'], -stats['drawdown'] * 100))
    log.info('期初资产: %.2f元 | 期末资产: %.2f元 | 交易日: %d天'
             % (stats['start'], stats['end'], stats['days']))
    log.info('实际持仓（占比为每日收盘仓位的年度均值）：')
    for code, weight_sum in py.sorted(stats['weight_sum'].items(),
                                     key=lambda item: item[1], reverse=True):
        info = get_security_info(code)
        name = info.display_name if info is not None else code
        log.info('  %s(%s): %d天，平均仓位%.2f%%'
                 % (name, code, stats['holding_days'][code],
                    weight_sum / stats['days'] * 100))
    log.info('  平均现金仓位: %.2f%%' % (stats['cash_weight_sum'] / stats['days'] * 100))


def on_strategy_end(context):
    if not g.year_stats:
        return
    log.info('最终同池基线报告（首尾不足整年按实际区间计算；回撤按收盘净值）')
    result = (g.previous_value / context.portfolio.starting_cash - 1) * 100
    log.info('累计收益: %+.2f%% | 全程最大回撤: %.2f%%'
             % (result, -g.total_drawdown * 100))
    for year in py.sorted(g.year_stats):
        print_year_stats(year)
