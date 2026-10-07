# -*- coding: utf-8 -*-
# lM_v3_2_1 —— v3_2 + 可卖域交还给池（去重贪心以"已持仓"为种子）（2026-10-06，基于 lM_v3_2 复制）
# 成交台账已修正：按实际成交记账；分批卖出合并，未清仓不重置退出状态。
# R²已批量修正为 w² 加权口径；缓存评分、候选与持仓样本需重建。
# 旧结果及裁决已从工作脚本移除；修正后平台结果待重跑。
# 旧收益、缓存 score/rank 与版本优劣不能作为修正评分的结论。

import math

import numpy as np

from jqdata import *

TOPN, EXIT_PCT = 3, 0.40
CROWD_LOOKBACK, CROWD_MIN_OBS, CROWD_MAX = 250, 60, 90.0
MIN_BARS = 250
LIQ_AMT20_MIN = 50_000_000.0
REBUILD_EVERY = 20
CORR_DEDUPE = 0.90
MATURE_DAYS = 365
SCORE_DAYS = 25                      # new.py m_days：25 根收盘 + 当日价
TRAIL_DD = 0.20                      # 本版唯一新变量：浮盈最高点回撤 20% 清仓
REENTRY_BAN = 20                     # 跟踪出局后禁回购的交易日数（=REBUILD_EVERY）

# 名称剔除词：宽基/规模/风格/债券/货币/商品/跨境——行业与主题保留
# v2_1 补充（末尾 7 词）：堵 v2 实测漏网的跨境变体命名
EXCLUDE_KW = ('沪深300', '中证500', '中证1000', '中证800', '中证全指', '2000', '200',
              '中证A500', 'A500', 'A50', 'A100', '上证50', '上证180', '上证380', '科创50',
              '科创100', '科创综', '创业板50', '创业板综', '创业板指', '创业板',
              '双创', '北证', '深证100', '基本面50', '红利', '股息', '国债', '政金',
              '信用', '债券', '转债', '货币', '现金', '黄金', '白银', '原油', '豆粕',
              '商品', '纳斯达克', '纳指', '标普', '道琼', '日经', '德国', '法国',
              '亚太', '东南亚', '恒生', '香港', 'H股', '港股', '央企', '国企',
              '龙头', 'ESG', '养老', 'FOF', '联动', '增强', '价值', '成长',
              '质量', '低波', '动量', '多因子', '自由现金流',
              'HK', '225', '东证', '中韩', '美国', '恒指', '油气',
              '债', '上海金',
              # v3_1_1 补：货币/短融类命名漏网（511360/511880/511990/159003）
              '短融', '日利', '添益', '快线')

# 代码段硬剔除，不依赖命名：513xxx 沪市跨境 ETF / 518xxx 上金所黄金现货 ETF
EXCLUDE_CODE_PREFIXES = ('513', '518')


def initialize(context):
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark("000300.XSHG")
    set_slippage(FixedSlippage(0))                  # 无滑点（冻结口径）
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=0.0001,
                             close_commission=0.0001, min_commission=0),
                   type="fund")                     # 万一单边、无最低佣金
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.pool = []
    g.day = 0
    g.trades = []                         # 卖出台账：{date, code, name, pnl}
    g.peak = {}                           # 持仓期最高 last_price（跟踪止盈基准）
    g.ban = {}                            # 跟踪出局 code -> 解禁交易日序号
    run_daily(run_rotation, time='14:55')


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


def rebuild_pool(dt, held):
    """规则动态池：时点候选 → 跨境/名称/成熟度/流动性/相关性去重（全用当时数据）。

    held（已持仓 code）作去重**种子**：不参与成熟/流动/去重三关，且挤掉与它
    相关 ≥0.90 的候选。池在本版起同时是选股域和可卖域——持仓一旦出池，卖侧
    `if r is None: continue` 会让它对排名闸和峰回闸双双免疫（v3_2 实测 43%
    持仓日如此）。种子只保证可卖：它已在 held，买侧自然跳过，不额外开买入口子。
    """
    secs = get_all_securities(['etf'], date=dt.strftime('%Y-%m-%d'))
    n_all = len(secs)
    today = dt.date()
    seeds = sorted(held)
    cands = []
    n_xb, n_name = 0, 0
    for code, row in secs.iterrows():
        if code.startswith(EXCLUDE_CODE_PREFIXES):    # 跨境/黄金现货代码段硬剔除
            n_xb += 1
            continue
        nm = row['display_name']
        # 不用裸 any()：引擎 any 被 numpy 覆盖对生成器恒真（v2 空池事故真因）
        hits = [k for k in EXCLUDE_KW if k in nm]
        if hits:
            continue
        n_name += 1
        start = row['start_date']
        if hasattr(start, 'date'):               # datetime/Timestamp → date
            start = start.date()
        if (today - start).days < MATURE_DAYS:   # date 减 date，类型稳
            continue
        if code in seeds:                        # 已是种子，不再作候选
            continue
        cands.append(code)
    n_mature = len(cands)
    liq = []
    if cands:
        amt20 = history(20, '1d', 'money', security_list=cands).mean(axis=0)
        liq = [c for c in cands if amt20.get(c, 0) == amt20.get(c, 0)
               and amt20[c] >= LIQ_AMT20_MIN]
    if len(liq) <= TOPN:
        log.info("池重建 %s: 全表 %d | 跨境剔 %d | 名称过 %d | 成熟 %d | 流动 %d"
                 " | 种子 %d（去重跳过）"
                 % (dt.strftime('%Y-%m-%d'), n_all, n_xb, n_name, n_mature, len(liq),
                    len(seeds)))
        return seeds + liq
    rets = history(250, '1d', 'close',
                   security_list=sorted(set(liq) | set(seeds))).pct_change()
    corr = rets.corr(min_periods=120)
    kept = list(seeds)                            # 贪心起点=种子：占位并顶掉自己的孪生票
    for c in sorted(liq, key=lambda x: -amt20[x]):
        ok = True
        for k in kept:
            r = corr.at[c, k]
            if r == r and r >= CORR_DEDUPE:     # NaN 视为不可比 → 保留
                ok = False
                break
        if ok:
            kept.append(c)
    log.info("池重建 %s: 全表 %d | 跨境剔 %d | 名称过 %d | 成熟 %d | 流动 %d"
             " | 去重后 %d（含种子 %d）"
             % (dt.strftime('%Y-%m-%d'), n_all, n_xb, n_name, n_mature, len(liq),
                len(kept), len(seeds)))
    log.info("池成员 %s: %s" % (dt.strftime('%Y-%m-%d'), ' '.join(kept)))
    return kept


def pct_in_window(vals, lookback):
    """自身近 N 日窗口内分位（0-100），不足 60 观测返回 None（镜像 v1）。"""
    w = vals[-lookback:]
    if len(w) < CROWD_MIN_OBS:
        return None
    last = w[-1]
    return round(sum(1 for v in w if v < last) / float(len(w)) * 100, 1)


def momentum_score(close_tail, price):
    """new.py 动量评分器镜像（EtfRotation.filter_moment_rank 逐条一致）。

    close_tail 末 SCORE_DAYS 根收盘 + 当日 last_price 共 26 点；
    对数价格加权回归（w=linspace(1,2)），score=年化×加权R²；
    近 3 个日环比 min<0.95 清零（跳水否决）。失败返回 0（镜像原 except 分支）。
    """
    try:
        prices = np.append(np.asarray(close_tail[-SCORE_DAYS:], dtype=float),
                           float(price))
        if len(prices) < SCORE_DAYS + 1 or np.any(prices <= 0):
            return 0.0
        logp = np.log(prices)
        x = np.arange(len(logp))
        w = np.linspace(1, 2, len(logp))
        slope, intercept = np.polyfit(x, logp, 1, w=w)
        ann = math.exp(slope * 250) - 1.0
        effective_w = w ** 2
        mean_logp = float(np.average(logp, weights=effective_w))
        ss_res = float(np.sum(effective_w * (logp - (slope * x + intercept)) ** 2))
        ss_tot = float(np.sum(effective_w * (logp - mean_logp) ** 2))
        r2 = float(np.clip(1.0 - ss_res / ss_tot, 0.0, 1.0)) if ss_tot > 0 else 0.0
        score = ann * r2
        if min(prices[-1] / prices[-2], prices[-2] / prices[-3],
               prices[-3] / prices[-4]) < 0.95:
            score = 0.0
        return round(float(score), 4)
    except Exception:
        return 0.0


def run_rotation(context):
    dt = context.current_dt
    g.day += 1
    if (g.day - 1) % REBUILD_EVERY == 0:
        g.pool = rebuild_pool(dt, {s for s, p in context.portfolio.positions.items()
                                   if p.total_amount > 0})   # 持仓=去重种子
    if not g.pool:
        return
    cd = get_current_data()
    today = dt.strftime('%Y-%m-%d')

    closes = history(CROWD_LOOKBACK, '1d', 'close', security_list=g.pool)
    money = history(CROWD_LOOKBACK, '1d', 'money', security_list=g.pool)
    rowsum = money.sum(axis=1)

    rows = []
    for code in g.pool:
        if cd[code].paused:
            continue                                # 停牌不可计分
        h = attribute_history(code, MIN_BARS - 1, '1d', ['close'],
                              skip_paused=True)
        if h is None or len(h) < MIN_BARS - 1:
            continue                                # 上市未满 250 根
        price = float(cd[code].last_price)
        if price <= 0:
            continue
        score = momentum_score(list(h['close'].values), price)
        comps = []
        if code in money.columns:
            share_vals = [m / s for m, s in zip(money[code].values, rowsum.values)
                          if s > 0 and m == m and m > 0]
            sp = pct_in_window(share_vals, CROWD_LOOKBACK)
            if sp is not None:
                comps.append(sp)
        cp_vals = [v for v in closes[code].values if v == v]
        cp = pct_in_window(cp_vals, CROWD_LOOKBACK)
        if cp is not None:
            comps.append(cp)
        crowd = round(sum(comps) / len(comps), 1) if len(comps) >= 2 else None
        rows.append({'code': code, 'name': get_security_name(code),
                     'price': price, 'score': score, 'crowd': crowd})

    rows.sort(key=lambda r: -r['score'])            # 稳定排序，池序解并列
    n = len(rows)
    rank = {r['code']: i + 1 for i, r in enumerate(rows)}

    # 先卖：持仓跌出前 40% 或 从浮盈最高点回撤 TRAIL_DD
    # 本版起持仓必在池内（种子），故 r is None 只剩停牌 / K线不足两种，仍持有
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0:
            continue
        r = rank.get(sec)
        if r is None:
            continue
        px = float(cd[sec].last_price)
        pk = max(g.peak.get(sec, px), px)
        g.peak[sec] = pk
        trail = px <= pk * (1.0 - TRAIL_DD)
        if not trail and r <= math.ceil(EXIT_PCT * n):
            continue
        _submit_sell(context, sec,
            "峰回%.0f%%" % (TRAIL_DD * 100) if trail else "排名",
            "峰%.3f rank %d/%d" % (pk, r, n),
            ban_until=g.day + REENTRY_BAN if trail else None)

    # 后买：score>0 且量价热度<90（None 放行）的前空槽数等权；跟踪出局者冷静期内跳过
    held = {s for s, p in context.portfolio.positions.items() if p.total_amount > 0}
    slots = TOPN - len(held)
    if slots > 0:
        cash = context.portfolio.available_cash
        sleeve = context.portfolio.total_value
        per = min(cash / slots, sleeve / TOPN)
        bought = 0
        for r in rows:
            if bought >= slots:
                break
            code = r['code']
            if code in held or r['score'] <= 0 \
               or (r['crowd'] is not None and r['crowd'] >= CROWD_MAX) \
               or g.ban.get(code, -1) >= g.day:
                continue
            px = r['price']
            amount = min(int(per / (px * 1.0001) // 100) * 100,
                         int(cash / (px * 1.0001) // 100) * 100)
            if amount < 100:
                continue
            order(code, amount)
            g.peak[code] = px                 # 新批次以买入价为跟踪止盈基准
            log.info("买入 %s %s %d份 @%.3f score=%.4f 量价热度=%s分 rank %d/%d"
                     % (code, r['name'], amount, px, r['score'],
                        r['crowd'], rank[code], n))
            bought += 1


# BEGIN SHARED PLATFORM LEDGER
# Canonical functions mirrored into standalone JoinQuant scripts by
# research/tools/sync_industry_momentum.py. No local import is needed on JoinQuant.


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
    # The standalone scripts set ETF selling commission to 0.0001, no minimum.
    fee = getattr(order, 'commission', None)
    fee = float(fee) if fee is not None else notional * 0.0001
    delta = filled - meta['filled']
    pnl = notional - meta['notional'] - meta['cost'] * delta - (fee - meta['fee'])
    if delta == 0 and abs(pnl) < 1e-9:
        return 0
    meta.update(filled=filled, notional=notional, fee=fee)
    code = meta['code']
    today = context.current_dt.strftime('%Y-%m-%d')
    g.sell_fills.append({'date': today, 'code': code, 'name': meta['name'],
                         'amount': delta, 'pnl': pnl})
    if delta > 0 and meta.get('ban_until') is not None:
        g.ban[code] = meta['ban_until']
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
        for name in ('neg_run', 'be', 'peak', 'peaks'):
            state = getattr(g, name, None)
            if state is not None:
                state.pop(code, None)
        log.info('完成平仓 %s %s 合并成交盈亏 %+.2f' % (code, meta['name'], total))
    return delta


def _submit_sell(context, code, reason, detail='', ban_until=None):
    """Snapshot cost before order_target mutates the platform position object."""
    _ensure_sell_ledger()
    cost = float(context.portfolio.positions[code].avg_cost)
    order = order_target(code, 0)
    if order is None:
        log.info('卖出未成交 %s 触发[%s]，保留剩余持仓与退出状态' % (code, reason))
        return 0
    meta = {'code': code, 'name': get_security_name(code), 'cost': cost,
            'reason': reason, 'detail': detail, 'filled': 0, 'notional': 0.0,
            'fee': 0.0, 'closed_index': None, 'ban_until': ban_until}
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
