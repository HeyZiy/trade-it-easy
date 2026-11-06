# -*- coding: utf-8 -*-
"""全 ETF 趋势轮动研究基线；可直接复制到聚宽回测。

策略收益
91.74%
策略年化收益
27.68%
超额收益
50.97%
基准收益
27.00%
阿尔法
0.187
贝塔
0.932
夏普比率
0.734
胜率
0.441
盈亏比
1.529
最大回撤 
29.98%
索提诺比率
0.963
日均超额收益
0.08%
超额收益最大回撤
38.75%
超额收益夏普比率
0.467
日胜率
0.542
盈利次数
75
亏损次数
95
信息比率
0.671
策略波动率
0.323
基准波动率
0.186
最大回撤区间
2024/03/18,2025/06/19
"""

import builtins as _b
import math
import uuid

import numpy as np
import pandas as pd

from jqdata import *

STRATEGY_VERSION = 'etf_v1_holdings_first'
TOPN = 3
ENTRY_RANK = 3                       # 量能筛选前的排名上限，不能向后递补
EXIT_RANK_MULT = 2                   # 旧仓留存带：前 TOPN×2
STOP_COST_PCT = 0.08                 # 每日评估成本 -8% 即提交退出，不保证成交价
SCORE_EXIT_CONFIRM = 3               # 连续有效评估不合格次数；分数/排名共用计数
MIN_BARS = 250
LIQ_AMT20_MIN = 50_000_000.0
REBUILD_EVERY = 20
CORR_DEDUPE = 0.90
CORR_MIN_OBS = 120
SCORE_DAYS = 25                      # 25 根已完成日线 + 当日最新价
VOL_RECENT_DAYS = 20
VOL_BASE_DAYS = 40
VOL_CONFIRM_MIN = 0.8                # 近20日均额 / 此前40日均额，仅过滤新仓
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
    g.pool_candidates = []
    g.pool_conflicts = {}
    g.pool_valid = False              # 区分正常空池和重建失败，失败次日重试
    g.day = 0
    g.trades = []                         # 卖出台账：{date, code, name, pnl}
    g.neg_run = {}                        # rank 掉队或 score≤0 连续计数（确认期用），卖出时清除
    g.buy_orders = {}                 # 只存序列化元数据；名额以持仓及平台挂单为准
    g.detail_log = []
    g.detail_path = 'etf_v1_details_%s_%s.txt' % (
        context.current_dt.strftime('%Y%m%d'), uuid.uuid4().hex[:12])
    run_daily(run_rotation, time='14:55')
    log.info('策略 %s 参数 %s' % (STRATEGY_VERSION, {
        'topn': TOPN, 'entry_rank': ENTRY_RANK, 'exit_rank': TOPN * EXIT_RANK_MULT,
        'confirm': SCORE_EXIT_CONFIRM, 'stop_cost': STOP_COST_PCT,
        'min_bars': MIN_BARS, 'liquidity': LIQ_AMT20_MIN,
        'rebuild_every': REBUILD_EVERY, 'corr_dedupe': CORR_DEDUPE,
        'score_days': SCORE_DAYS, 'volume_recent': VOL_RECENT_DAYS,
        'volume_base': VOL_BASE_DAYS, 'volume_min': VOL_CONFIRM_MIN,
        'commission': COMMISSION_RATE, 'slippage': 0, 'time': '14:55',
        'detail_file': g.detail_path}))


def _record_detail(dt, message):
    g.detail_log.append('%s - %s' % (dt.strftime('%Y-%m-%d %H:%M:%S'), message))


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
    """冻结当期新买候选的优先级及相关关系，持仓直接作为去重起点。"""
    secs = get_all_securities(['etf'], date=dt.strftime('%Y-%m-%d'))
    n_all = len(secs)
    if not n_all:
        raise ValueError('ETF历史名单为空，无法确认候选范围')
    cands = list(secs.index)
    money = history(20, '1d', 'money', security_list=cands)
    if money is None or len(money) < 20 or set(cands) - set(money.columns) - held:
        raise ValueError('建池成交额数据不完整')
    valid_money = {}
    for code in cands:
        if code not in money:
            continue
        values = np.asarray(money[code].iloc[-20:], dtype=float)
        if np.all(np.isfinite(values)) and np.all(values >= 0):
            valid_money[code] = float(values.mean())
    if not valid_money:
        raise ValueError('建池成交额全部无效')
    old_candidates = (set(g.pool) & set(g.pool_candidates)) - held
    missing_old = (set(cands) - set(valid_money)) & old_candidates
    if missing_old:
        raise ValueError('旧池成交额缺失: %s' % sorted(missing_old))
    liquid = {c for c in cands if valid_money.get(c, 0) >= LIQ_AMT20_MIN}
    histories = {}
    score_of = {}
    for c in sorted(liquid | held):
        try:
            required = CORR_MIN_OBS + 1 if c in held else MIN_BARS - 1
            closes = read_closes(c, MIN_BARS, min_count=required)
        except Exception as exc:
            if c not in held:
                raise
            _record_detail(dt, '持仓相关性历史异常 %s: %s' % (c, exc))
            continue
        if closes is None:
            if c in old_candidates:
                raise ValueError('旧池收盘历史不足或无效: %s' % c)
            continue
        histories[c] = closes
        if c in liquid and len(closes) >= MIN_BARS - 1:
            vals = closes.values
            score_of[c] = momentum_score(list(vals[:-1]), float(vals[-1]))
    corr = pd.DataFrame(histories).pct_change(fill_method=None).corr(min_periods=CORR_MIN_OBS)
    candidates = sorted(score_of, key=lambda c: (-score_of[c], -valid_money[c], c))
    conflicts = {c: set(corr.index[corr[c] >= CORR_DEDUPE]) for c in candidates}
    _record_detail(dt, '池重建 全表 %d | 流动 %d | 新买候选 %d | 持仓直入 %s'
                   % (n_all, len(liquid), len(candidates), sorted(held)))
    return candidates, conflicts


def select_pool(candidates, conflicts, held):
    """先保留实际持仓，再补入与已选成员无已知高相关的候选。"""
    kept = sorted(held)
    selected = set(held)
    for c in candidates:
        if c not in selected and selected.isdisjoint(conflicts[c]):
            kept.append(c)
            selected.add(c)
    return kept


def momentum_score(close_tail, price):
    return momentum_details(close_tail, price)[0]


def momentum_details(close_tail, price):
    """动量评分器：保留原回归，修正R²有效权重与中心均值。

    close_tail 末 SCORE_DAYS 根收盘 + 当日 last_price 共 26 点；
    对数价格加权回归（w=linspace(1,2)），score=年化×w²加权R²；
    返回最终分、未取整原始分、跳水标记。无效输入按零分处理，诊断值未知。
    """
    try:
        prices = np.append(np.asarray(close_tail[-SCORE_DAYS:], dtype=float),
                           float(price))
        if (len(prices) < SCORE_DAYS + 1 or not np.all(np.isfinite(prices))
                or np.any(prices <= 0)):
            return 0.0, None, None
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
        raw_score = float(ann * r2)
        sharp_drop = bool(_b.min(prices[-1] / prices[-2], prices[-2] / prices[-3],
                                 prices[-3] / prices[-4]) < 0.95)
        score = 0.0 if sharp_drop else round(raw_score, 4)
        return score, raw_score, sharp_drop
    except Exception:
        return 0.0, None, None


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
            score, raw_score, sharp_drop = momentum_details(list(closes.values), price)
            if not np.isfinite(score):
                raise ValueError('趋势分无效')
        except Exception as exc:
            complete = False
            _record_detail(dt, '排名数据异常 %s: %s' % (code, exc))
            continue
        rows.append({'code': code, 'name': get_security_name(code),
                     'price': price, 'score': score, 'raw_score': raw_score,
                     'sharp_drop': sharp_drop})
    rows.sort(key=lambda r: (-r['score'], r['code']))
    return rows, complete


def evaluate_exits(context, cd, rows, rank_valid):
    """自身成本/趋势出口独立执行；排名未知时仅暂停排名判断。"""
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
            _record_detail(context.current_dt, '持仓价格异常 %s: %s' % (sec, exc))
            continue
        below_stop = px <= pos.avg_cost * (1.0 - STOP_COST_PCT)
        row = row_of.get(sec, {})
        score_now = row.get('score')
        raw_score = row.get('raw_score')
        sharp_drop = row.get('sharp_drop')
        if score_now is None and not below_stop:
            try:
                closes = read_closes(sec, SCORE_DAYS)
                if closes is not None:
                    score_now, raw_score, sharp_drop = momentum_details(list(closes.values), px)
            except Exception as exc:
                _record_detail(context.current_dt, '持仓趋势数据异常 %s: %s' % (sec, exc))
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
        should_sell = below_stop or ((nonpositive or out_of_rank) and neg >= SCORE_EXIT_CONFIRM)
        if neg == previous_neg and not should_sell:
            continue
        detail = 'score=%s rank=%s 排名有效=%s 本次非正分=%s 本次排名掉队=%s 现价%.3f' % (
            '未知' if score_now is None else '%.4f' % score_now,
            rank, rank_valid, nonpositive, out_of_rank, px)
        _record_detail(context.current_dt, '持仓评估 %s 计数%d->%d %s 原始分=%s 跳水=%s %s' % (
            sec, previous_neg, neg,
            '触发卖出' if should_sell else '继续持有',
            '未知' if raw_score is None else '%+.6g' % raw_score,
            '未知' if sharp_drop is None else sharp_drop, detail))
        if not should_sell:
            continue
        if below_stop:
            reason = "跌破成本-8%"
        else:
            reason = '连续%d次评估不合格' % neg
        _submit_sell(context, sec, reason, detail)
        sold_today.add(sec)
    return sold_today


def volume_ratio(code):
    """近20日均额/此前40日均额；缺失、负数、无穷和零分母均不可买。"""
    count = VOL_RECENT_DAYS + VOL_BASE_DAYS
    h = history(count, '1d', 'money', security_list=[code])
    if h is None or code not in h or len(h) < count:
        return None
    values = np.asarray(h[code].iloc[-count:], dtype=float)
    if not np.all(np.isfinite(values)) or np.any(values < 0):
        return None
    base = float(values[:VOL_BASE_DAYS].mean())
    if base <= 0:
        return None
    ratio = float(values[-VOL_RECENT_DAYS:].mean() / base)
    return ratio if np.isfinite(ratio) else None


def held_codes(context):
    return {s for s, p in context.portfolio.positions.items() if p.total_amount > 0}


def _record_buy_fill(dt, result, meta):
    filled = int(result.filled)
    delta = filled - meta['filled']
    if delta > 0:
        _record_detail(dt, '买入成交 %s 本次%d份 累计%d份 成交均价%.4f' % (
            meta['code'], delta, filled, float(result.price)))
        meta['filled'] = filled


def _sync_buy_orders(dt):
    for result in get_orders().values():
        meta = g.buy_orders.get(str(result.order_id))
        if meta is not None:
            _record_buy_fill(dt, result, meta)


def execute_buys(context, rows, sold_today):
    """只在入场排名域内补空槽；持仓与有效买单共同占用名额。"""
    open_orders = get_open_orders()
    open_codes = {o.security for o in open_orders.values()}
    reserved = held_codes(context) | {o.security for o in open_orders.values() if o.is_buy}
    slots = TOPN - len(reserved)
    if slots <= 0:
        return
    cash_left = float(context.portfolio.available_cash)
    per = _b.min(cash_left / slots, context.portfolio.total_value / TOPN)
    for rank, row in enumerate(rows[:ENTRY_RANK], 1):
        if len(reserved) >= TOPN:
            break
        code = row['code']
        if code in reserved or code in open_codes or code in sold_today or row['score'] <= 0:
            continue
        try:
            ratio = volume_ratio(code)
        except Exception as exc:
            _record_detail(context.current_dt, '量能数据异常 %s: %s' % (code, exc))
            continue
        if ratio is None or ratio < VOL_CONFIRM_MIN:
            continue
        px = row['price']
        budget = _b.min(per, cash_left, context.portfolio.available_cash)
        amount = int(budget / (px * (1 + COMMISSION_RATE)) // 100) * 100
        if amount < 100:
            continue
        result = order(code, amount)
        if result is None:
            _record_detail(context.current_dt, '买入委托失败 %s rank=%d' % (code, rank))
            continue
        _record_detail(context.current_dt, '提交买入委托 %s %d份 参考价%.3f score=%.4f rank=%d' % (
            code, amount, px, row['score'], rank))
        meta = {'code': code, 'filled': 0}
        g.buy_orders[str(result.order_id)] = meta
        _record_buy_fill(context.current_dt, result, meta)
        # 平台挂单表判断有效委托，成交量判断实际持仓，均不以尝试次数替代。
        live = get_open_orders()
        pending = {o.security for o in live.values() if o.is_buy}
        if int(result.filled) > 0 or code in pending:
            reserved.add(code)
            committed = amount * px * (1 + COMMISSION_RATE) if code in pending else (
                int(result.filled) * float(result.price) * (1 + COMMISSION_RATE))
            cash_left = _b.max(0., cash_left - committed)
        else:
            _record_detail(context.current_dt, '买入委托结束且零成交 %s，未占用名额' % code)
        reserved |= held_codes(context) | pending
        open_codes = {o.security for o in live.values()}


def run_rotation(context):
    """编排建池、排名、持仓退出与新仓执行；正常空池仍管理持仓。"""
    g.day += 1
    _sync_sell_orders(context)
    _sync_buy_orders(context.current_dt)
    held = held_codes(context)
    if not g.pool_valid or (g.day - 1) % REBUILD_EVERY == 0:
        try:
            g.pool_candidates, g.pool_conflicts = rebuild_pool(context.current_dt, held)
            g.pool_valid = True
        except Exception as exc:
            g.pool_valid = False
            _record_detail(context.current_dt, '池重建失败，暂停新买与排名判断，下次重试: %s' % exc)
    cd = get_current_data()
    if g.pool_valid:
        g.pool = select_pool(g.pool_candidates, g.pool_conflicts, held)
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
    _record_detail(context.current_dt,
                   '卖出成交 %s %s 触发[%s] %s | 本次%d份 累计%d份 剩余%d份 '
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
        for name in ('neg_run',):
            state = getattr(g, name, None)
            if state is not None:
                state.pop(code, None)
        _record_detail(context.current_dt, '完成平仓 %s %s 合并成交盈亏 %+.2f'
                       % (code, meta['name'], total))
    return delta


def _submit_sell(context, code, reason, detail=''):
    """Snapshot cost before order_target mutates the platform position object."""
    _ensure_sell_ledger()
    cost = float(context.portfolio.positions[code].avg_cost)
    order = order_target(code, 0)
    if order is None:
        _record_detail(context.current_dt, '卖出未成交 %s 触发[%s]，保留剩余持仓与退出状态'
                       % (code, reason))
        return 0
    meta = {'code': code, 'name': get_security_name(code), 'cost': cost,
            'reason': reason, 'detail': detail, 'filled': 0, 'notional': 0.0,
            'fee': 0.0, 'closed_index': None}
    # Store only serializable metadata in g, not platform Order objects.
    g.sell_orders[str(order.order_id)] = meta
    delta = _record_sell_order(context, order, meta)
    if delta == 0:
        _record_detail(context.current_dt, '卖出未成交 %s 触发[%s]，保留剩余持仓与退出状态'
                       % (code, reason))
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
    _sync_buy_orders(context.current_dt)


def on_strategy_end(context):
    """Report confirmed closed trades, partial realized P&L and reconciliation."""
    # JoinQuant globals can shadow Python's sum with numpy.sum.
    import builtins as python_builtins

    _sync_sell_orders(context)
    _sync_buy_orders(context.current_dt)
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
    try:
        write_file(g.detail_path, '\n'.join(g.detail_log) + '\n')
    except Exception as exc:
        log.info('【明细导出失败】%s: %s' % (g.detail_path, exc))
    else:
        log.info('【明细导出】在聚宽投资研究文件列表下载 %s（%d 条）'
                 % (g.detail_path, len(g.detail_log)))
# END SHARED PLATFORM LEDGER
