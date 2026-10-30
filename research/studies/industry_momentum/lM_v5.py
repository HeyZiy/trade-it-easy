# -*- coding: utf-8 -*-
# lM_v5 —— 行业动量轮动：最简合理版 + 权益事件隔离
#
# 相对 v4 只加一件事：持仓数量非本策略成交而变化（拆合/份额折算）时，
# 峰值价按 1/k 折算到新刻度，当天不做价格类判定。v4 在折算登记日
# 会用“新成本 ÷ 旧价格”把折算倍数写进峰值，次日触发假 trail/crash。
#
# 逻辑链只有一条：
#   买最强（20 日涨幅前 3，且 ≥5%）→ 跌破峰值 4×ATR 走 →
#   单日崩盘当日撤 → 趋势死了撤 → 凑不满就持币
#
# 六个可调项（其余全是数据口径，不是可调参数）：
#   MIN_RET    = 0.05   20 日涨幅门槛，不够就空仓
#   TRAIL_ATR  = 4.0    峰值下方 4×ATR20 跟踪
#   CRASH_DAY  = -0.05  单日跌幅触发当日离场（入场侧同样过滤）
#   DEATH_DAYS = 3      20 日涨幅 < 0 连续 3 日离场
#   MAX_CORR   = 0.75   新仓与已持有的相关性上限（120 对真实行业 ETF 的
#                       相关性中位 0.26，超过 0.75 的只有 3%——酒/消费、医药/医疗）
#   MAX_WEIGHT = 0.40   单票权重上限，重建日削回
#
# 刻意不要的东西（都是针对某次回测疼痛的补丁，叠起来会互相打架）：
#   成本锚止损、+12%/+1% 保本棘轮、池内相关性去重、量价热度闸、
#   评分内嵌跳水否决、R² 加权回归、指数择时。
#
# 跑法：粘贴整个文件；2024-01-01 ~ 2026-10-01，初始 10 万，天频。

import numpy as np
from jqdata import *

# ---------------- 六个可调项 ----------------
MIN_RET = 0.05
TRAIL_ATR = 4.0
CRASH_DAY = -0.05
DEATH_DAYS = 3
MAX_CORR = 0.75
MAX_WEIGHT = 0.40

# ---------------- 数据口径 ----------------
TOPN = 3
LOOKBACK = 20
MIN_DAYS = 365
MIN_AMT20 = 50_000_000.0
REBUILD_EVERY = 20
CORR_DAYS = 120
EXCLUDE_PREFIXES = ('513', '518', '520')     # 沪市跨境 / 上金所黄金 / 跨境新段

# 名称负筛：宽基、风格、债券货币、商品跨境。新增词只堵漏，不改信号。
EXCLUDE_KW = (
    # 宽基
    '沪深300', '中证500', '中证1000', '中证800', '中证全指', '中证A500', 'A500',
    '上证50', '上证180', '上证380', '科创50', '科创100', '科创综', '创业板',
    '双创', '北证', '深证100', '2000', 'A50', 'A100', '中证100',
    # 风格 / 因子
    '红利', '股息', '价值', '成长', '质量', '低波', '动量', '多因子', '基本面',
    'ESG', '自由现金流', '增强', '龙头', '央企', '国企', '养老',
    # 债券 / 货币
    '国债', '政金', '信用债', '债券', '转债', '货币', '现金', '短融', '日利',
    '添益', '快线', '债',
    # 商品 / 跨境
    '黄金', '白银', '原油', '豆粕', '商品', '油气', '上海金',
    '纳斯达克', '纳指', '标普', '道琼', '日经', '德国', '法国', '亚太',
    '东南亚', '恒生', '恒指', '港股', 'H股', '香港', '中概', '美国', '东证',
    '225', 'HK', '中韩',
)


def initialize(context):
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark('000300.XSHG')
    set_slippage(FixedSlippage(0))
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=0.0001,
                             close_commission=0.0001, min_commission=0),
                   type='fund')
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.pool = []        # 当前池
    g.corr = None      # 池 + 持仓的相关矩阵（重建时算一次）
    g.day = 0
    g.peak_ratio = {}  # code -> 入场以来最高价/持仓成本（比例口径，成本与价格同步折算时不变）
    g.dead = {}        # code -> 20 日涨幅 < 0 的连续天数
    g.amt = {}         # code -> 本策略成交后的持仓数量基准，用于识别拆合/份额折算
    g.event_today = set()  # 当日发生权益事件的持仓，跳过价格类判定
    g.sold_today = set() # 当日已卖出，禁止同日买回
    g.pending_buy = {}  # code -> {order_id, age}，避免延迟成交时重复买
    g.trades = []
    g.cash_sum = 0.0   # 现金占比累计（汇报用）
    g.cash_n = 0
    run_daily(rotate, time='14:55')


def sec_name(code):
    try:
        return get_security_info(code).display_name
    except Exception:
        return code


def rebuild_pool(context, dt):
    """全表 → 代码段/名称/成熟度/流动性。不做池内相关去重，去重放在选股那一刻。"""
    today = dt.date()
    secs = get_all_securities(['etf'], date=dt.strftime('%Y-%m-%d'))
    n_all = len(secs)
    cands, n_px, n_kw, n_age = [], 0, 0, 0
    for code, row in secs.iterrows():
        if code.startswith(EXCLUDE_PREFIXES):
            n_px += 1
            continue
        hits = [k for k in EXCLUDE_KW if k in row['display_name']]   # 不用裸 any()
        if hits:
            n_kw += 1
            continue
        start = row['start_date']
        if hasattr(start, 'date'):
            start = start.date()
        if (today - start).days < MIN_DAYS:
            n_age += 1
            continue
        cands.append(code)

    liq = []
    if cands:
        amt = history(20, '1d', 'money', security_list=cands).mean(axis=0)
        liq = [c for c in cands
               if amt.get(c, 0) == amt.get(c, 0) and amt[c] >= MIN_AMT20]

    held = [s for s, p in context.portfolio.positions.items() if p.total_amount > 0]
    uni = sorted(set(liq) | set(held))
    corr = None
    if len(uni) > 1:
        rets = history(CORR_DAYS, '1d', 'close', security_list=uni).pct_change()
        corr = rets.corr(min_periods=60)

    g.pool, g.corr = liq, corr
    log.info('池 %s: 全表%d 代码段剔%d 名称剔%d 未满1年%d 入池%d'
             % (dt.strftime('%Y-%m-%d'), n_all, n_px, n_kw, n_age, len(liq)))
    if liq:
        log.info('池成员: %s' % ' '.join(c[:6] for c in liq))


def pool_returns(pool, cd, dt):
    """一次取完整个池的 20 日涨幅；今天若被引擎算进 history，就丢掉末行。"""
    h = history(LOOKBACK + 2, '1d', 'close', security_list=pool)
    if len(h) and str(h.index[-1])[:10] == dt.strftime('%Y-%m-%d'):
        h = h.iloc[:-1]
    out = {}
    for code in pool:
        if code not in h.columns or cd[code].paused:
            continue
        v = h[code].values.astype(float)
        v = v[~np.isnan(v)]
        px = float(cd[code].last_price)
        if px <= 0 or len(v) < LOOKBACK + 1:
            continue
        if px / v[-1] - 1.0 <= CRASH_DAY:        # 当天正在崩的不买
            continue
        out[code] = px / v[-LOOKBACK] - 1.0      # 末根是昨收，往前第 20 根即 20 个交易日
    return out


def pos_metrics(code, px):
    """持仓用的三项：20 日涨幅、ATR20、昨收。"""
    try:
        h = attribute_history(code, LOOKBACK + 1, '1d',
                              ['close', 'high', 'low'], skip_paused=True)
    except Exception:
        return None
    if h is None or len(h) < LOOKBACK + 1:
        return None
    c = h['close'].values.astype(float)
    hi = h['high'].values.astype(float)
    lo = h['low'].values.astype(float)
    trs = np.maximum(hi[1:] - lo[1:],
                     np.maximum(np.abs(hi[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])))
    atr = float(np.mean(trs[-LOOKBACK:]))
    if not atr > 0:
        atr = px * 0.02
    return px / c[-LOOKBACK] - 1.0, atr, float(c[-1])


def sync_position_state(context):
    """同步延迟买单状态；记录本策略成交后的数量基准，并识别权益事件。"""
    current = {s for s, p in context.portfolio.positions.items() if p.total_amount > 0}
    active_ids = set()
    try:
        active_ids = {str(o.order_id) for o in get_orders().values()}
    except Exception:
        pass
    for sec, pending in list(g.pending_buy.items()):
        if sec in current:
            g.pending_buy.pop(sec, None)
            g.amt[sec] = int(context.portfolio.positions[sec].total_amount)
        elif str(pending['order_id']) not in active_ids:
            pending['age'] += 1
            if pending['age'] >= 2:
                log.info('买单已结束且无持仓，释放挂单状态 %s order=%s'
                         % (sec, pending['order_id']))
                g.pending_buy.pop(sec, None)

    # 权益事件：数量的变化不是本策略成交造成的（拆合/份额折算）。
    # 账户成本会先按折算调整，行情价次日才切；那一天 px/cost 差一个折算倍数，
    # 峰值价按 1/k 折算到新刻度后，当天不参与任何价格类判定。
    for sec, pos in context.portfolio.positions.items():
        if pos.total_amount <= 0:
            continue
        n = int(pos.total_amount)
        base = g.amt.get(sec)
        if base is None:
            g.amt[sec] = n
            continue
        if n == base:
            continue
        k = n / max(base, 1)
        cost = float(pos.avg_cost)
        peak_px = g.peak_ratio.get(sec, 1.0) * cost * k   # 折算前刻度的峰值价
        g.peak_ratio[sec] = (peak_px / k) / max(cost, 1e-12)
        g.dead[sec] = 0
        g.amt[sec] = n
        g.event_today.add(sec)
        log.info('权益事件 %s 数量%d→%d（×%.4g）成本%.3f 峰值%.3f按比例折算'
                 % (sec, base, n, k, cost, peak_px / k))



def get_security_info_safe(code):
    try:
        return get_security_info(code).display_name
    except Exception:
        return code


def corr_blocked(code, picks):
    if g.corr is None:
        return False
    for k in picks:
        if k == code or code not in g.corr.columns or k not in g.corr.index:
            continue
        r = g.corr.at[code, k]
        if r == r and r >= MAX_CORR:
            return True
    return False


def trim(context):
    """只削不补：单票超过 40% 削回。"""
    cd = get_current_data()
    total = context.portfolio.total_value
    for sec, pos in list(context.portfolio.positions.items()):
        if pos.total_amount <= 0 or cd[sec].paused or sec in g.event_today:
            continue
        excess = pos.value - total * MAX_WEIGHT
        if excess <= 0:
            continue
        px = float(cd[sec].last_price)
        if px <= 0:
            continue
        amt = int(excess / (px * 1.0001) // 100) * 100
        if amt < 100:
            continue
        cost = float(pos.avg_cost)
        o = order(sec, -amt)
        if o is None:
            log.info('削仓未成交 %s 请求%d份' % (sec, amt))
            continue
        filled = int(getattr(o, 'filled', 0) or 0)
        if filled <= 0:
            log.info('削仓待确认 %s 请求%d份' % (sec, amt))
            continue
        g.amt[sec] = int(context.portfolio.positions[sec].total_amount)
        fill_px = float(getattr(o, 'price', 0) or px)
        fee = float(getattr(o, 'commission', 0) or fill_px * filled * 0.0001)
        g.trades.append({'date': context.current_dt.strftime('%Y-%m-%d'),
                         'code': sec, 'name': sec_name(sec), 'cost': cost,
                         'peak': g.peak_ratio.get(sec, 1.0) * cost, 'px': fill_px, 'amount': filled,
                         'pnl': (fill_px - cost) * filled - fee,
                         'reason': 'trim'})
        log.info('削仓成交 %s %d/%d份（权重超 %.0f%%）'
                 % (sec, filled, amt, MAX_WEIGHT * 100))


def do_sell(context, sec, reason, px):
    pos = context.portfolio.positions[sec]
    requested = int(pos.total_amount)
    cost = float(pos.avg_cost)
    peak = g.peak_ratio.get(sec, 1.0) * cost
    o = order_target(sec, 0)
    if o is None:
        log.info('卖出未成交 %s [%s]' % (sec, reason))
        return False
    filled = int(getattr(o, 'filled', 0) or 0)
    if filled <= 0:
        log.info('卖出待成交 %s [%s] 请求%d份 order=%s'
                 % (sec, reason, requested, getattr(o, 'order_id', '?')))
        return False
    fill_px = float(getattr(o, 'price', 0) or px)
    fee = float(getattr(o, 'commission', 0) or fill_px * filled * 0.0001)
    pnl = (fill_px - cost) * filled - fee
    remaining = context.portfolio.positions.get(sec)
    is_closed = remaining is None or remaining.total_amount <= 0
    g.trades.append({'date': context.current_dt.strftime('%Y-%m-%d'),
                     'code': sec, 'name': sec_name(sec), 'cost': cost,
                     'peak': peak, 'px': fill_px, 'amount': filled,
                     'pnl': pnl, 'reason': reason if is_closed else reason + '_partial'})
    if is_closed:
        g.sold_today.add(sec)
        g.peak_ratio.pop(sec, None)
        g.dead.pop(sec, None)
        g.amt.pop(sec, None)
    else:
        g.amt[sec] = int(remaining.total_amount)
    log.info('卖出成交 %s %s [%s] %d/%d份 成本%.3f 成交%.3f 峰值%.3f 盈亏%+.2f 剩余%d'
             % (sec, sec_name(sec), reason, filled, requested, cost, fill_px, peak, pnl,
                int(remaining.total_amount) if not is_closed else 0))
    return is_closed


def rotate(context):
    dt = context.current_dt
    g.day += 1
    g.sold_today = set()
    g.event_today = set()
    cd = get_current_data()
    sync_position_state(context)
    if (g.day - 1) % REBUILD_EVERY == 0:
        rebuild_pool(context, dt)
        trim(context)
    tv = max(context.portfolio.total_value, 1.0)
    g.cash_sum += min(1.0, max(0.0, (tv - context.portfolio.available_cash) / tv))
    g.cash_n += 1

    # ---- 1) 卖出 ----
    for sec in [s for s, p in context.portfolio.positions.items() if p.total_amount > 0]:
        if cd[sec].paused or sec in g.event_today:
            continue
        px = float(cd[sec].last_price)
        if px <= 0:
            continue
        m = pos_metrics(sec, px)
        if m is None:
            continue
        ret, atr, prev = m
        cost = float(context.portfolio.positions[sec].avg_cost)
        peak_ratio = max(g.peak_ratio.get(sec, 1.0), px / max(cost, 1e-12))
        g.peak_ratio[sec] = peak_ratio
        crash = prev > 0 and px / prev - 1.0 <= CRASH_DAY
        g.dead[sec] = (g.dead.get(sec, 0) + 1) if ret < 0 else 0
        if crash:
            do_sell(context, sec, 'crash', px)
        elif px / max(cost, 1e-12) <= peak_ratio - TRAIL_ATR * atr / max(cost, 1e-12):
            do_sell(context, sec, 'trail', px)
        elif g.dead[sec] >= DEATH_DAYS:
            do_sell(context, sec, 'dead', px)

    # ---- 2) 买入：排名 + 绝对门槛 + 相关上限 ----
    held = [s for s, p in context.portfolio.positions.items() if p.total_amount > 0]
    held += [s for s in g.pending_buy if s not in held]
    slots = TOPN - len(held)
    if slots <= 0 or not g.pool:
        return
    rets = pool_returns(g.pool, cd, dt)
    cands = sorted([(r, c) for c, r in rets.items()
                    if c not in held and r >= MIN_RET and not cd[c].paused],
                   reverse=True)
    per = context.portfolio.total_value / TOPN
    cash = context.portfolio.available_cash
    picks = list(held)
    for ret, code in cands:
        if slots <= 0:
            break
        px = float(cd[code].last_price)
        if px <= 0:
            continue
        if code in g.sold_today:
            continue
        if corr_blocked(code, picks):
            continue
        amt = int(min(per, cash) / (px * 1.0001) // 100) * 100
        if amt < 100:
            continue
        o = order(code, amt)
        if o is None:
            log.info('买入未下单 %s 请求%d份' % (code, amt))
            continue
        order_id = getattr(o, 'order_id', None)
        filled = int(getattr(o, 'filled', 0) or 0)
        if filled > 0:
            fill_px = float(getattr(o, 'price', 0) or px)
            g.peak_ratio[code] = max(g.peak_ratio.get(code, 1.0), fill_px / max(float(context.portfolio.positions[code].avg_cost), 1e-12))
            g.dead.setdefault(code, 0)
            g.amt[code] = int(context.portfolio.positions[code].total_amount)
            log.info('买入成交 %s %s %d/%d份 @%.3f 20日涨幅%+.1f%%'
                     % (code, sec_name(code), filled, amt, fill_px, ret * 100))
        else:
            g.pending_buy[code] = {'order_id': str(order_id), 'age': 0}
            log.info('买入待成交 %s %s 请求%d份 order=%s'
                     % (code, sec_name(code), amt, order_id))
        cash -= (float(getattr(o, 'price', 0) or px) * filled * 1.0001
                 if filled > 0 else amt * px * 1.0001)
        picks.append(code)
        slots -= 1


def on_strategy_end(context):
    import builtins as pyb
    t = [x for x in g.trades if x['reason'] != 'trim']
    if not t:
        log.info('【汇总】无交易')
        return
    pnl = pyb.sum(x['pnl'] for x in t)
    wins = [x for x in t if x['pnl'] > 0]
    log.info('【汇总】平仓%d笔 胜率%.1f%% 已实现%+.2f'
             % (len(t), 100.0 * len(wins) / len(t), pnl))
    by_reason = {}
    for x in t:
        d = by_reason.setdefault(x['reason'], [0, 0.0])
        d[0] += 1
        d[1] += x['pnl']
    for r, (n, s) in sorted(by_reason.items(), key=lambda kv: -abs(kv[1][1])):
        log.info('【汇总】退出[%s] %d笔 合计%+.2f' % (r, n, s))
    big = [x for x in t if x['cost'] > 0 and x['peak'] / x['cost'] - 1.0 >= 0.10]
    if big:
        cap = [((x['px'] - x['cost']) / (x['peak'] - x['cost'])) for x in big
               if x['peak'] - x['cost'] > 1e-9]
        if cap:
            cap.sort()
            log.info('【汇总】峰值≥10%%的%d笔 捕获率 中位%.2f 均值%.2f'
                     % (len(big), cap[len(cap) // 2], pyb.sum(cap) / len(cap)))
        if g.cash_n:
            log.info('【汇总】平均资金占用率 %.0f%%' % (100.0 * g.cash_sum / g.cash_n))
    log.info('【汇总】期末权益 %.2f（总收益 %.2f%%）'
             % (context.portfolio.total_value,
                (context.portfolio.total_value / context.portfolio.starting_cash - 1) * 100))
