# -*- coding: utf-8 -*-
"""聚宽单文件：15/37只池 × 等权/合格持有/排名轮动，一次回测六个独立账户。

粘贴整个文件；2016-01-01至2026-10-01，初始资金600000，频率每天。
每账户100000，不划转资金。频率变更须六组一起变更，不能混用历史日频结果。
平台主图是六账户的合计；比较看六条累计收益辅助线和分账户年度/全程日志。
费用、复权、整手、停牌与实际成交均由平台处理，不是虚拟净值估算。
下载 pool_control_nav.csv / pool_control_signals.csv / pool_control_decisions.csv。
实际平台结果及核算口径见同目录README；本地测试不能替代平台回测。
"""

from jqdata import *
import builtins as py
import csv
import io
import math
import numpy as np


ORIGINAL15 = (
    '510300.XSHG', '159915.XSHE', '515180.XSHG', '512100.XSHG', '588000.XSHG',
    '513500.XSHG', '513100.XSHG', '159920.XSHE', '513520.XSHG', '513030.XSHG',
    '518880.XSHG', '159980.XSHE', '159985.XSHE', '511090.XSHG', '511010.XSHG',
)
INDUSTRY22 = (
    '515170.XSHG', '512660.XSHG', '512690.XSHG', '512400.XSHG', '159928.XSHE',
    '515220.XSHG', '512010.XSHG', '515210.XSHG', '512170.XSHG', '159870.XSHE',
    '512800.XSHG', '159996.XSHE', '512000.XSHG', '512200.XSHG', '512480.XSHG',
    '159611.XSHE', '515880.XSHG', '516110.XSHG', '512720.XSHG', '512980.XSHG',
    '515030.XSHG', '515790.XSHG',
)
MIXED37 = ORIGINAL15 + INDUSTRY22
HISTORY_DAYS = 25
COMMISSION = 0.0002
MIN_COMMISSION = 5.0
ROUND_LOT = 100
NAV_COLUMNS = ('date', 'account', 'nav', 'cash', 'invested', 'return_pct',
               'drawdown_pct', 'holdings')
SIGNAL_COLUMNS = ('date', 'code', 'decision_price', 'score', 'trend_score',
                  'annual_slope', 'r_squared', 'status', 'paused', 'rank15', 'rank37')
DECISION_COLUMNS = ('date', 'account', 'holdings_before', 'target', 'reason',
                    'old_score', 'target_score')


def write_rows(filename, columns, rows, append=True):
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerows(rows if append else [columns])
    write_file(filename, output.getvalue(), append=append)


def initialize(context):
    starting = float(context.portfolio.starting_cash)
    if not math.isclose(starting, 600000.0, abs_tol=0.01):
        raise ValueError('请将初始资金设为600000元，六个账户各100000元，以匹配既有实验')
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=COMMISSION,
                            close_commission=COMMISSION, close_today_commission=0,
                            min_commission=MIN_COMMISSION), type='fund')
    set_slippage(FixedSlippage(0))
    set_subportfolios([SubPortfolioConfig(cash=100000.0, type='stock')
                       for _ in range(6)])
    g.accounts = []
    for size, pool in ((15, ORIGINAL15), (37, MIXED37)):
        for mode, label in (('equal', '等权'), ('hold', '合格持有'), ('rank', '排名')):
            g.accounts.append(dict(index=len(g.accounts), label='%d%s' % (size, label),
                                   pool=pool, mode=mode, eligible=set(), month=None,
                                   initial=100000.0, previous=100000.0, peak=100000.0,
                                   max_drawdown=0.0, years={}))
    g.last_close = None
    g.last_trade = None
    for filename, columns in (('pool_control_nav.csv', NAV_COLUMNS),
                              ('pool_control_signals.csv', SIGNAL_COLUMNS),
                              ('pool_control_decisions.csv', DECISION_COLUMNS)):
        write_rows(filename, columns, [], append=False)
    run_daily(trade_handler, time='10:40', reference_security='000300.XSHG')
    run_daily(after_market_close, time='15:01')
    log.info('[六组对照] 每账户10万元；六条辅助线均为实际分账户累计收益%。'
             '平台主图是六组合计，不是某一个策略；以分账户曲线及日志作比较。')


def signal_snapshot(context):
    """一次计算37只的真实决策分数，两池按各自原始顺序稳定排序。"""
    current = get_current_data()
    observations = {}
    for code in MIXED37:
        row = dict(price=None, score=None, trend_score=None, annual=None, r2=None,
                   status='missing_history', paused=None)
        try:
            row['paused'] = int(current[code].paused)
            row['price'] = float(current[code].last_price)
            hist = attribute_history(code, HISTORY_DAYS, '1d', ['close'],
                                     skip_paused=False, fq='pre')
            if hist is None or hist.empty or len(hist['close']) < HISTORY_DAYS:
                observations[code] = row
                continue
            prices = np.append(hist['close'].values, row['price'])
            if not np.isfinite(prices).all() or np.any(prices <= 0):
                row['status'] = 'invalid_price'
                observations[code] = row
                continue
            y = np.log(prices)
            x = np.arange(len(y))
            w = np.linspace(1, 2, len(y))
            slope, intercept = np.polyfit(x, y, 1, w=w)
            annual = math.exp(slope * 250) - 1.0
            effective_w = w ** 2
            mean_y = float(np.average(y, weights=effective_w))
            residual = y - (slope * x + intercept)
            ss_res = float(np.sum(effective_w * residual * residual))
            ss_tot = float(np.sum(effective_w * (y - mean_y) ** 2))
            r_squared = float(np.clip(1 - ss_res / ss_tot, 0.0, 1.0)) if ss_tot > 0 else 0.0
            trend_score = annual * r_squared
            drop = py.any(prices[-i] / prices[-i-1] < 0.95 for i in (1, 2, 3))
            score = 0.0 if drop else trend_score
            status = ('drop_filter' if drop else 'nonpositive' if score <= 0 else
                      'score_upper_limit' if score >= 5 else 'eligible')
            row.update(score=score, trend_score=trend_score, annual=annual,
                       r2=r_squared, status=status)
        except Exception as error:
            row['status'] = 'data_error'
            log.error('[信号缺失] %s %s: %s' % (context.current_dt.date(), code, error))
        observations[code] = row
    return current, observations


def ranked_members(pool, observations):
    # 与原策略一致：暂停不新增为评分过滤条件，是否成交交给平台。
    members = [code for code in pool if observations[code]['status'] == 'eligible']
    return py.sorted(members, key=lambda code: observations[code]['score'], reverse=True)


def positions_of(context, account):
    return context.subportfolios[account['index']].long_positions


def holding_codes(context, account):
    return [code for code, position in positions_of(context, account).items()
            if code in account['pool'] and position.total_amount > 0]


def rotation_order(context, account, code, value, current):
    """保留原金额委托及失败后整手回退，只增加独立账户路由。"""
    index = account['index']
    try:
        if value <= 0:
            order_target_value(code, 0, pindex=index)
        else:
            result = order_target_value(code, float(value), pindex=index)
            if not result:
                price = current[code].last_price
                if price and price > 0:
                    amount = int(value / price / ROUND_LOT) * ROUND_LOT
                    if amount > 0:
                        order_target(code, amount, pindex=index)
    except Exception as error:
        log.error('[%s委托失败] %s: %s' % (account['label'], code, error))


def rotation_trade(context, account, ranked, observations, current):
    held = holding_codes(context, account)
    old = held[0] if held else None
    target = ranked[0] if ranked else None
    if account['mode'] == 'hold' and old in ranked:
        target = old
    if old == target:
        reason = 'keep' if old else 'no_candidate'
    elif old:
        reason = ('rank_replaced' if observations[old]['status'] == 'eligible'
                  else observations[old]['status'])
    else:
        reason = 'entry'
    if old != target:
        if old:
            rotation_order(context, account, old, 0, current)
        if target:
            cash = context.subportfolios[account['index']].available_cash
            rotation_order(context, account, target, cash, current)
        log.info('[%s] %s → %s；原因=%s' % (account['label'], old, target, reason))
    return (str(context.current_dt.date()), account['label'], '|'.join(held),
            target or '', reason, observations[old]['score'] if old else None,
            observations[target]['score'] if target else None)


def equal_members(context, account):
    members = []
    for code in account['pool']:
        info = get_security_info(code, date=context.previous_date)
        if info is None or info.start_date > context.previous_date:
            continue
        if info.end_date < context.current_dt.date():
            continue
        if code not in account['eligible']:
            hist = attribute_history(code, HISTORY_DAYS, '1d', ['close'],
                                     skip_paused=False, fq='pre')
            if hist is None or len(hist) < HISTORY_DAYS:
                continue
            if not py.all(math.isfinite(float(px)) and float(px) > 0 for px in hist['close']):
                continue
            account['eligible'].add(code)
        members.append(code)
    return members


def equal_order(account, code, target):
    try:
        order_target(code, int(target), pindex=account['index'])
    except Exception as error:
        log.error('[%s委托失败] %s: %s' % (account['label'], code, error))


def equal_trade(context, account, current):
    today = context.current_dt.date()
    month = (today.year, today.month)
    if account['month'] == month:
        return None
    members = equal_members(context, account)
    account['month'] = month
    before = '|'.join(holding_codes(context, account))
    if not members:
        return (str(today), account['label'], before, '', 'no_eligible_history', None, None)
    portfolio = context.subportfolios[account['index']]
    budget = float(portfolio.total_value) / len(members)
    targets, prices = {}, {}
    for code in members:
        price = float(current[code].last_price)
        if current[code].paused or not math.isfinite(price) or price <= 0:
            continue  # 暂停成员仍占一个目标预算，不能将其份额分给其他成员。
        prices[code] = price
        targets[code] = int(budget / price / ROUND_LOT) * ROUND_LOT

    def amount(code):
        position = positions_of(context, account).get(code)
        return int(position.total_amount) if position is not None else 0

    for code, target in targets.items():
        held = amount(code)
        if held > target:
            position = positions_of(context, account)[code]
            sell = py.min(held - target, int(position.closeable_amount))
            if sell > 0:
                equal_order(account, code, held - sell)
    deficits, required = {}, 0.0
    for code, target in targets.items():
        deficit = py.max(0, target - amount(code))
        if deficit >= ROUND_LOT:
            deficits[code] = deficit
            cost = deficit * prices[code]
            required += cost + py.max(MIN_COMMISSION, cost * COMMISSION)
    if required > 0:
        scale = py.min(1.0, float(portfolio.available_cash) / required)
        for code, deficit in deficits.items():
            buy = int(deficit * scale / ROUND_LOT) * ROUND_LOT
            cash = float(portfolio.available_cash)
            affordable = int(py.max(0.0, cash - MIN_COMMISSION)
                             / (prices[code] * (1 + COMMISSION)) / ROUND_LOT) * ROUND_LOT
            buy = py.min(buy, affordable)
            if buy >= ROUND_LOT:
                equal_order(account, code, amount(code) + buy)
    log.info('[%s月度等权] %s，历史可用%d只，单只目标%.2f%%'
             % (account['label'], today, len(members), 100.0 / len(members)))
    return (str(today), account['label'], before, '|'.join(members), 'monthly_equal', None, None)


def trade_handler(context):
    today = context.current_dt.date()
    if g.last_trade == today:
        return
    current, observations = signal_snapshot(context)
    rankings = {15: ranked_members(ORIGINAL15, observations),
                37: ranked_members(MIXED37, observations)}
    ranks = {size: {code: i + 1 for i, code in enumerate(ranked)}
             for size, ranked in rankings.items()}
    rows = []
    for code, row in observations.items():
        rows.append((str(today), code, row['price'], row['score'], row['trend_score'],
                     row['annual'], row['r2'], row['status'], row['paused'],
                     ranks[15].get(code), ranks[37].get(code)))
    write_rows('pool_control_signals.csv', SIGNAL_COLUMNS, rows)
    decisions = []
    for account in g.accounts:
        if account['mode'] == 'equal':
            decision = equal_trade(context, account, current)
        else:
            decision = rotation_trade(context, account, rankings[len(account['pool'])],
                                      observations, current)
        if decision is not None:
            decisions.append(decision)
    write_rows('pool_control_decisions.csv', DECISION_COLUMNS, decisions)
    g.last_trade = today


def update_stats(account, today, total):
    if not math.isfinite(total) or total <= 0:
        raise ValueError('%s账户净值无效，停止统计' % account['label'])
    year = today.year
    if year not in account['years']:
        if account['years']:
            print_year(account, py.max(account['years']))
        account['years'][year] = dict(start=account['previous'], end=total,
                                     first=str(today), last=str(today),
                                     peak=account['previous'], drawdown=0.0)
    stats = account['years'][year]
    stats['end'], stats['last'] = total, str(today)
    stats['peak'] = py.max(stats['peak'], total)
    stats['drawdown'] = py.min(stats['drawdown'], total / stats['peak'] - 1.0)
    account['peak'] = py.max(account['peak'], total)
    drawdown = total / account['peak'] - 1.0
    account['max_drawdown'] = py.min(account['max_drawdown'], drawdown)
    account['previous'] = total
    return drawdown


def print_year(account, year):
    stats = account['years'][year]
    log.info('[%s %d年 %s至%s] 收益%+.2f%%，收益额%+.2f元，期初%.2f元，期末%.2f元，'
             '年内最大回撤%.2f%%' %
             (account['label'], year, stats['first'], stats['last'],
              (stats['end'] / stats['start'] - 1) * 100,
              stats['end'] - stats['start'], stats['start'], stats['end'],
              -stats['drawdown'] * 100))


def after_market_close(context):
    today = context.current_dt.date()
    if g.last_close == today:
        return
    records, rows = {}, []
    for account in g.accounts:
        portfolio = context.subportfolios[account['index']]
        total = float(portfolio.total_value)
        drawdown = update_stats(account, today, total)
        cumulative = (total / account['initial'] - 1) * 100
        invested = py.sum(float(pos.value) for pos in portfolio.long_positions.values()
                          if pos.total_amount > 0)
        records[account['label'] + '%'] = cumulative
        rows.append((str(today), account['label'], total, float(portfolio.available_cash),
                     invested, cumulative, drawdown * 100,
                     '|'.join(holding_codes(context, account))))
    record(**records)
    write_rows('pool_control_nav.csv', NAV_COLUMNS, rows)
    g.last_close = today


def on_strategy_end(context):
    log.info('[六组最终报告] 以下为独立账户实际净值；平台合计曲线不用于策略比较。')
    for account in g.accounts:
        for year in py.sorted(account['years']):
            print_year(account, year)
        log.info('[%s全程] 累计收益%+.2f%%，期初%.2f元，期末%.2f元，最大回撤%.2f%%' %
                 (account['label'], (account['previous'] / account['initial'] - 1) * 100,
                  account['initial'], account['previous'], -account['max_drawdown'] * 100))
    log.info('[导出] 下载pool_control_nav.csv、pool_control_signals.csv、'
             'pool_control_decisions.csv；nav含每日六账户，signals含两池全候选评分，'
             'decisions中的target是目标，实际仓位以nav和平台持仓/成交为准。')
