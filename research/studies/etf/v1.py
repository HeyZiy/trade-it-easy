# -*- coding: utf-8 -*-
"""全 ETF 动量轮动基线；可直接复制到聚宽回测。

- 池：全 ETF，20日均额 >= 3000万，上市满 250 根日线，每 20 日重建；持仓始终保留在池内。
- 评分：25 根已完成日线 + 当日最新价，对数价格加权线性回归，score = 年化斜率 × R²。
- 买入：14:55 按分数排名补满 TOPN=3 个名额，等权；score<=0 不买。
- 卖出：排名跌出 TOPN×2 或 score<=0，连续 3 次评估确认后退出；数据未知时冻结计数。
"""

import builtins as _b
import math

import numpy as np

from jqdata import *

STRATEGY_VERSION = 'etf_v1_baseline'
TOPN = 3
EXIT_RANK_MULT = 2                   # 旧仓留存带：前 TOPN×2
SCORE_EXIT_CONFIRM = 3               # 连续有效评估不合格次数；分数/排名共用计数
MIN_BARS = 250
LIQ_AMT20_MIN = 30_000_000.0
REBUILD_EVERY = 20
SCORE_DAYS = 25                      # 25 根已完成日线 + 当日最新价
COMMISSION_RATE = 0.0001


def initialize(context):
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark("000300.XSHG")
    set_slippage(FixedSlippage(0))                  # 无滑点（冻结口径）
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=COMMISSION_RATE,
                             close_commission=COMMISSION_RATE, min_commission=0),
                   type="fund")                     # 万一单边、无最低佣金
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.pool = []
    g.pool_valid = False              # 区分正常空池和重建失败，失败次日重试
    g.day = 0
    g.trades = []                         # 卖出台账：{date, code, name, pnl}
    g.neg_run = {}                        # 掉队或 score<=0 连续计数，卖出时清除
    run_daily(run_rotation, time='14:55')
    log.info('策略 %s 参数 %s' % (STRATEGY_VERSION, {
        'topn': TOPN, 'exit_rank': TOPN * EXIT_RANK_MULT,
        'confirm': SCORE_EXIT_CONFIRM,
        'min_bars': MIN_BARS, 'liquidity': LIQ_AMT20_MIN,
        'rebuild_every': REBUILD_EVERY, 'score_days': SCORE_DAYS,
        'commission': COMMISSION_RATE, 'slippage': 0, 'time': '14:55'}))


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


def read_closes(code, count, min_count=None):
    """保留日期索引；要求最后 min_count 根收盘全部有效，不跨缺失日拼接。"""
    required = count if min_count is None else min_count
    h = attribute_history(code, count, '1d', ['close'], skip_paused=True)
    if h is None or len(h) < required:
        return None
    closes = h['close'].iloc[-count:].astype(float)
    invalid = np.flatnonzero(~np.isfinite(closes.values) | (closes.values <= 0))
    if invalid.size:
        closes = closes.iloc[invalid[-1] + 1:]
    return closes if len(closes) >= required else None


def rebuild_pool(dt, held):
    secs = get_all_securities(['etf'], date=dt.strftime('%Y-%m-%d'))
    if not len(secs):
        raise ValueError('ETF历史名单为空，无法确认候选范围')
    cands = list(secs.index)
    money = history(20, '1d', 'money', security_list=cands)
    if money is None or len(money) < 20:
        raise ValueError('建池成交额数据不完整')
    liquid = set()
    for code in cands:
        if code not in money:
            continue
        values = np.asarray(money[code].iloc[-20:], dtype=float)
        if (np.all(np.isfinite(values)) and np.all(values >= 0)
                and float(values.mean()) >= LIQ_AMT20_MIN):
            liquid.add(code)
    if not liquid:
        raise ValueError('流动性过滤后候选为空')
    pool = set(held)
    for code in sorted(liquid - held):
        if read_closes(code, MIN_BARS - 1) is not None:
            pool.add(code)
    log.info('池重建 全表 %d | 流动 %d | 池 %d | 持仓直入 %s'
             % (len(cands), len(liquid), len(pool), sorted(held)))
    return sorted(pool)


def momentum_details(close_tail, price):
    """对数价格加权回归趋势分；数据无效时返回 None。"""
    try:
        prices = np.append(np.asarray(close_tail[-SCORE_DAYS:], dtype=float),
                           float(price))
        if (len(prices) < SCORE_DAYS + 1 or not np.all(np.isfinite(prices))
                or np.any(prices <= 0)):
            return None
        logp = np.log(prices)
        x = np.arange(len(logp))
        w = np.linspace(1, 2, len(logp))
        slope, intercept = np.polyfit(x, logp, 1, w=w)
        ann = math.exp(slope * 250) - 1.0
        # polyfit(w=w)的残差平方权重为w²，R²与拟合使用同一权重。
        effective_w = w ** 2
        mean_logp = float(np.average(logp, weights=effective_w))
        ss_res = float(np.sum(effective_w * (logp - (slope * x + intercept)) ** 2))
        ss_tot = float(np.sum(effective_w * (logp - mean_logp) ** 2))
        r2 = float(np.clip(1.0 - ss_res / ss_tot, 0.0, 1.0)) if ss_tot > 0 else 0.0
        score = float(ann * r2)
        return score if np.isfinite(score) else None
    except Exception:
        return None


def rank_pool(cd, dt, held):
    """返回完整排名和有效性；单个成熟成员缺数据也不能制造虚假掉队。"""
    rows = []
    complete = True
    for code in g.pool:
        try:
            if cd[code].paused:
                continue
            price = float(cd[code].last_price)
            closes = read_closes(code, SCORE_DAYS if code in held else MIN_BARS - 1)
            if not np.isfinite(price) or price <= 0 or closes is None:
                raise ValueError('最新价或历史收盘无效')
            score = momentum_details(closes.values, price)
            if score is None:
                raise ValueError('趋势分无效')
        except Exception as exc:
            complete = False
            log.info('排名数据异常 %s: %s' % (code, exc))
            continue
        rows.append({'code': code, 'name': get_security_name(code),
                     'price': price, 'score': score})
    rows.sort(key=lambda r: (-r['score'], r['code']))
    return rows, complete


def evaluate_exits(context, cd, rows, rank_valid):
    """排名未知时仅暂停排名判断，自身分数出口独立执行。"""
    rank_of = {r['code']: i + 1 for i, r in enumerate(rows)}
    row_of = {r['code']: r for r in rows}
    sold_today = set()
    open_codes = {o.security for o in get_open_orders().values()}
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0 or sec in open_codes:
            continue
        try:
            if cd[sec].paused:
                continue
            px = float(cd[sec].last_price)
            if not np.isfinite(px) or px <= 0:
                raise ValueError('最新价无效')
        except Exception as exc:
            log.info('持仓价格异常 %s: %s' % (sec, exc))
            continue
        score_now = row_of.get(sec, {}).get('score')
        if score_now is None:
            try:
                closes = read_closes(sec, SCORE_DAYS)
                if closes is not None:
                    score_now = momentum_details(closes.values, px)
            except Exception as exc:
                log.info('持仓趋势数据异常 %s: %s' % (sec, exc))
        rank = rank_of.get(sec)
        out_of_rank = rank_valid and rank is not None and rank > TOPN * EXIT_RANK_MULT
        nonpositive = score_now is not None and score_now <= 0
        previous_neg = g.neg_run.get(sec, 0)
        neg = previous_neg
        if nonpositive or out_of_rank:
            neg += 1
        elif rank_valid and rank is not None and score_now is not None:
            neg = 0
        # 两个条件都未知时冻结；已经由有效数据确认的不合格才累计。
        g.neg_run[sec] = neg
        should_sell = (nonpositive or out_of_rank) and neg >= SCORE_EXIT_CONFIRM
        if neg == previous_neg and not should_sell:
            continue
        detail = 'score=%s rank=%s 排名有效=%s 本次非正分=%s 本次排名掉队=%s 现价%.3f' % (
            '未知' if score_now is None else '%+.6g' % score_now,
            rank, rank_valid, nonpositive, out_of_rank, px)
        log.info('持仓评估 %s 计数%d->%d %s %s' % (
            sec, previous_neg, neg,
            '触发卖出' if should_sell else '继续持有', detail))
        if not should_sell:
            continue
        _submit_sell(context, sec, '连续%d次评估不合格' % neg, detail)
        sold_today.add(sec)
    return sold_today


def held_codes(context):
    return {s for s, p in context.portfolio.positions.items() if p.total_amount > 0}


def execute_buys(context, rows, sold_today):
    """按排名补空槽；持仓与有效买单共同占用名额。"""
    open_orders = get_open_orders()
    open_codes = {o.security for o in open_orders.values()}
    reserved = held_codes(context) | {o.security for o in open_orders.values() if o.is_buy}
    slots = TOPN - len(reserved)
    if slots <= 0:
        return
    cash_left = float(context.portfolio.available_cash)
    per = _b.min(cash_left / slots, context.portfolio.total_value / TOPN)
    for rank, row in enumerate(rows[:TOPN], 1):
        if len(reserved) >= TOPN:
            break
        code = row['code']
        if code in reserved or code in open_codes or code in sold_today or row['score'] <= 0:
            continue
        px = row['price']
        budget = _b.min(per, cash_left, context.portfolio.available_cash)
        amount = int(budget / (px * (1 + COMMISSION_RATE)) // 100) * 100
        if amount < 100:
            continue
        result = order(code, amount)
        if result is None:
            log.info('买入委托失败 %s rank=%d' % (code, rank))
            continue
        log.info('提交买入委托 %s %d份 参考价%.3f score=%+.6g rank=%d' % (
            code, amount, px, row['score'], rank))
        # 平台挂单表判断有效委托，成交量判断实际持仓，均不以尝试次数替代。
        live = get_open_orders()
        pending = {o.security for o in live.values() if o.is_buy}
        if int(result.filled) > 0 or code in pending:
            reserved.add(code)
            committed = amount * px * (1 + COMMISSION_RATE) if code in pending else (
                int(result.filled) * float(result.price) * (1 + COMMISSION_RATE))
            cash_left = _b.max(0., cash_left - committed)
        else:
            log.info('买入委托结束且零成交 %s，未占用名额' % code)
        reserved |= held_codes(context) | pending


def run_rotation(context):
    """编排建池、排名、持仓退出与新仓执行；正常空池仍管理持仓。"""
    g.day += 1
    _sync_sell_orders(context)
    held = held_codes(context)
    if not g.pool_valid or (g.day - 1) % REBUILD_EVERY == 0:
        try:
            g.pool = rebuild_pool(context.current_dt, held)
            g.pool_valid = True
        except Exception as exc:
            g.pool_valid = False
            log.info('池重建失败，暂停新买与排名判断，下次重试: %s' % exc)
    cd = get_current_data()
    if g.pool_valid:
        rows, complete = rank_pool(cd, context.current_dt, held)
    else:
        rows, complete = [], False
    rank_valid = g.pool_valid and complete
    sold_today = evaluate_exits(context, cd, rows, rank_valid)
    if rank_valid:
        execute_buys(context, rows, sold_today)


# BEGIN SHARED PLATFORM LEDGER


def _ensure_sell_ledger():
    for name, initial in (('sell_orders', {}), ('sell_partial', {}), ('sell_fills', [])):
        if not hasattr(g, name):
            setattr(g, name, initial)


def _record_sell_order(context, order, meta):
    """Reconcile cumulative fills; never use requested shares as executed shares."""
    filled = int(order.filled)
    if filled <= 0:
        return 0
    notional = float(order.price) * filled
    # Match the configured selling commission, with no minimum.
    fee = getattr(order, 'commission', None)
    fee = float(fee) if fee is not None else notional * COMMISSION_RATE
    delta = filled - meta['filled']
    pnl = notional - meta['notional'] - meta['cost'] * delta - (fee - meta['fee'])
    if delta == 0 and abs(pnl) < 1e-9:
        return 0
    meta.update(filled=filled, notional=notional, fee=fee)
    code = meta['code']
    today = context.current_dt.strftime('%Y-%m-%d')
    g.sell_fills.append({'date': today, 'code': code, 'name': meta['name'],
                         'amount': delta, 'pnl': pnl})
    if meta['closed_index'] is not None:
        # A final commission adjustment must update the same closed trade.
        g.trades[meta['closed_index']]['pnl'] += pnl
        return delta
    g.sell_partial[code] = g.sell_partial.get(code, 0.0) + pnl
    pos = context.portfolio.positions.get(code)
    remaining = int(pos.total_amount) if pos is not None else 0
    log.info('卖出成交 %s %s 触发[%s] %s | 本次%d份 累计%d份 剩余%d份 '
             '成交均价%.4f 本次成交盈亏 %+.2f'
             % (code, meta['name'], meta['reason'], meta['detail'], delta, filled,
                remaining, float(order.price), pnl))
    if remaining <= 0:
        total = g.sell_partial.pop(code)
        closed_index = len(g.trades)
        for pending in g.sell_orders.values():
            if pending['code'] == code and pending['closed_index'] is None:
                pending['closed_index'] = closed_index
        g.trades.append({'date': today, 'code': code, 'name': meta['name'], 'pnl': total})
        g.neg_run.pop(code, None)
        log.info('完成平仓 %s %s 合并成交盈亏 %+.2f'
                 % (code, meta['name'], total))
    return delta


def _submit_sell(context, code, reason, detail=''):
    """Snapshot cost before order_target mutates the platform position object."""
    _ensure_sell_ledger()
    cost = float(context.portfolio.positions[code].avg_cost)
    order = order_target(code, 0)
    if order is None:
        log.info('卖出未成交 %s 触发[%s]，保留剩余持仓与退出状态' % (code, reason))
        return 0
    meta = {'code': code, 'name': get_security_name(code), 'cost': cost,
            'reason': reason, 'detail': detail, 'filled': 0, 'notional': 0.0,
            'fee': 0.0, 'closed_index': None}
    # Store only serializable metadata in g, not platform Order objects.
    g.sell_orders[str(order.order_id)] = meta
    delta = _record_sell_order(context, order, meta)
    if delta == 0:
        log.info('卖出未成交 %s 触发[%s]，保留剩余持仓与退出状态' % (code, reason))
    return delta


def _sync_sell_orders(context):
    _ensure_sell_ledger()
    for order in get_orders().values():
        meta = g.sell_orders.get(str(order.order_id))
        if meta is not None:
            _record_sell_order(context, order, meta)


def after_trading_end(context):
    """Capture subsequent fills and fees without counting the same fill twice."""
    _sync_sell_orders(context)


def on_strategy_end(context):
    """Report confirmed closed trades, partial realized P&L and reconciliation."""
    # JoinQuant globals can shadow Python's sum with numpy.sum.
    import builtins as python_builtins

    _sync_sell_orders(context)
    trades = g.trades
    closed = float(python_builtins.sum(t['pnl'] for t in trades))
    partial = float(python_builtins.sum(g.sell_partial.values()))
    realized = closed + partial
    wins = len([t for t in trades if t['pnl'] > 0])
    losses = len([t for t in trades if t['pnl'] < 0])
    log.info('【台账】完成平仓 %d 笔（盈 %d 亏 %d 平 %d）| 合并盈亏 %+.2f'
             % (len(trades), wins, losses, len(trades)-wins-losses, closed))
    log.info('【台账】未清仓已实现 %+.2f | 全部成交已实现 %+.2f' % (partial, realized))
    ranked = sorted(trades, key=lambda t: -t['pnl'])
    for i, t in enumerate(ranked[:5]):
        log.info('【台账】TOP%d %s %s %s pnl=%+.2f'
                 % (i+1, t['date'], t['code'], t['name'], t['pnl']))
    top3 = float(python_builtins.sum(t['pnl'] for t in ranked[:3]))
    ratio = top3 / closed * 100 if closed > 0 else float('nan')
    log.info('【台账】TOP3=%+.2f 占完成平仓净利 %.1f%% | 其余 %d 笔合计 %+.2f'
             % (top3, ratio, python_builtins.max(0, len(trades)-3), closed-top3))
    years = {}
    for fill in g.sell_fills:
        year = fill['date'][:4]
        years[year] = years.get(year, 0.0) + fill['pnl']
    for year in sorted(years):
        log.info('【台账】分年成交已实现 %s: %+.2f' % (year, years[year]))
    unrealized = 0.0
    for code, pos in context.portfolio.positions.items():
        if pos.total_amount > 0:
            value = pos.value - pos.total_amount * pos.avg_cost
            unrealized += value
            log.info('【台账】期末持仓 %s %s 浮盈 %+.2f'
                     % (code, get_security_name(code), value))
    equity = context.portfolio.total_value
    initial = context.portfolio.starting_cash
    log.info('【台账】期末未实现 %+.2f | 期末权益 %.2f（总收益 %.2f%%）'
             % (unrealized, equity, (equity/initial-1)*100))
    difference = equity - initial - realized - unrealized
    if abs(difference) < 0.005:
        difference = 0.0
    log.info('【台账核对】权益增量 %+.2f = 成交已实现 %+.2f + 未实现 %+.2f '
             '+ 待核对账户差额 %+.2f（含现金分红等未按成交归因的变动）'
             % (equity-initial, realized, unrealized, difference))
# END SHARED PLATFORM LEDGER
