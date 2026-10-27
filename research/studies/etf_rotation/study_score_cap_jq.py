# -*- coding: utf-8 -*-
"""聚宽双账户：原15只，基线 vs 仅取消旧持仓的分数上限。

2016-01-01~2026-10-01，初始200000（两账户各100000），频率每天。
10:40决策；万二、最低5元、零滑点；新买入两组均要求0<score<5。
实验组仅允许已有持仓在score>=5时继续参与排名；非正分、跳水、排名退出不变。
导出score_cap_nav.csv、score_cap_signals.csv、score_cap_decisions.csv。
平台主图是两组合计，以分账户导出为准；须从头运行，不支持热更新拼接。
"""
from jqdata import *
import csv
import io
import math
import numpy as np

POOL = (
    '510300.XSHG', '159915.XSHE', '515180.XSHG', '512100.XSHG', '588000.XSHG',
    '513500.XSHG', '513100.XSHG', '159920.XSHE', '513520.XSHG', '513030.XSHG',
    '518880.XSHG', '159980.XSHE', '159985.XSHE', '511090.XSHG', '511010.XSHG',
)


def score_of(history, last_price):
    prices = np.append(np.asarray(history, dtype=float), float(last_price))
    if len(prices) != 26 or not np.isfinite(prices).all() or np.any(prices <= 0):
        return float('nan')
    y = np.log(prices)
    x, w = np.arange(len(y)), np.linspace(1, 2, len(y))
    slope, intercept = np.polyfit(x, y, 1, w=w)
    effective_w = w ** 2
    center = float(np.average(y, weights=effective_w))
    ss_res = float(np.sum(effective_w * (y - (slope * x + intercept)) ** 2))
    ss_tot = float(np.sum(effective_w * (y - center) ** 2))
    r2 = float(np.clip(1 - ss_res / ss_tot, 0., 1.)) if ss_tot > 0 else 0.
    score = (math.exp(slope * 250) - 1) * r2
    if prices[-1] / prices[-2] < .95 or prices[-2] / prices[-3] < .95 or prices[-3] / prices[-4] < .95:
        return 0.
    return score


def choose_target(scores, holding, hold_cap=False):
    eligible = [code for code in POOL if math.isfinite(scores.get(code, float('nan')))
                and 0 < scores[code] < 5]
    if hold_cap and holding in POOL:
        value = scores.get(holding, float('nan'))
        if math.isfinite(value) and value >= 5:
            eligible.append(holding)
    ranked = sorted(eligible, key=lambda c: (-scores[c], POOL.index(c)))
    return ranked[0] if ranked else None


def write_rows(filename, rows, append=True):
    out = io.StringIO()
    csv.writer(out, lineterminator='\n').writerows(rows)
    write_file(filename, out.getvalue(), append=append)


def initialize(context):
    if not math.isclose(float(context.portfolio.starting_cash), 200000., abs_tol=.01):
        raise ValueError('初始资金须为200000元，两账户各100000元')
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=.0002,
                             close_commission=.0002, close_today_commission=0,
                             min_commission=5), type='fund')
    set_slippage(FixedSlippage(0))
    set_subportfolios([SubPortfolioConfig(cash=100000., type='stock') for _ in range(2)])
    g.last_trade, g.last_close = None, None
    g.years = [{}, {}]
    for filename, header in [
        ('score_cap_nav.csv', ['date', 'account', 'nav', 'cash', 'holdings']),
        ('score_cap_signals.csv', ['date', 'code', 'decision_price', 'score', 'eligible', 'paused']),
        ('score_cap_decisions.csv', ['date', 'account', 'holding_before', 'target', 'old_score', 'reason']),
    ]:
        write_rows(filename, [header], append=False)
    run_daily(trade_handler, time='10:40', reference_security='000300.XSHG')
    run_daily(after_market_close, time='15:01')


def holdings(portfolio):
    return [code for code, p in portfolio.long_positions.items() if p.total_amount > 0]


def order_value(code, value, index, current):
    if current[code].paused:
        return
    result = order_target_value(code, float(value), pindex=index)
    if value > 0 and not result:
        price = float(current[code].last_price)
        if math.isfinite(price) and price > 0:
            qty = int(value / price / 100) * 100
            if qty > 0:
                order_target(code, qty, pindex=index)


def trade_handler(context):
    today = str(context.current_dt.date())
    if g.last_trade == today:
        return
    current, scores = get_current_data(), {}
    signals = []
    for code in POOL:
        hist = attribute_history(code, 25, '1d', ['close'], skip_paused=False, fq='pre')
        price = float(current[code].last_price)
        score = score_of(hist['close'].values, price) if hist is not None and len(hist) == 25 else float('nan')
        scores[code] = score
        signals.append([today, code, price, score, int(math.isfinite(score) and 0 < score < 5), int(current[code].paused)])
    decisions = []
    for index, label in enumerate(('baseline', 'hold_cap')):
        portfolio = context.subportfolios[index]
        held = holdings(portfolio)
        if len(held) > 1:
            raise ValueError('非单仓状态，拒绝掩盖执行异常')
        old = held[0] if held else None
        target = choose_target(scores, old, hold_cap=(index == 1))
        reason = 'keep' if target == old else 'rank_exit'
        if target == old and old and scores[old] >= 5:
            reason = 'hold_above_cap'
        if old != target:
            if old:
                order_value(old, 0., index, current)
            # 未成交/停牌卖出不得为新买单提供虚构回款。
            if target and not holdings(portfolio):
                order_value(target, portfolio.available_cash, index, current)
        decisions.append([today, label, old or '', target or '', scores.get(old), reason])
    write_rows('score_cap_signals.csv', signals)
    write_rows('score_cap_decisions.csv', decisions)
    g.last_trade = today


def after_market_close(context):
    today = str(context.current_dt.date())
    if g.last_close == today:
        return
    rows, chart = [], {}
    for index, label in enumerate(('baseline', 'hold_cap')):
        portfolio = context.subportfolios[index]
        value = float(portfolio.total_value)
        rows.append([today, label, value, float(portfolio.available_cash), '|'.join(holdings(portfolio))])
        chart[label] = (value / 100000. - 1) * 100
    write_rows('score_cap_nav.csv', rows)
    record(**chart)
    g.last_close = today
