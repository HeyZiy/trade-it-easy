# -*- coding: utf-8 -*-
"""行业动态池三仓：自身退出 vs 自身退出叠加排名退出。

聚宽策略编辑器：2024-01-01 至 2026-09-30，初始100000元，日频。
用同一文件运行两次，仅改 RANK_EXIT_PCT：None 为基线，0.40 为排名对照。
两组固定0.85流动性优先去重、20日建池、三仓、14:55、万一无最低佣金、零滑点。
排名组仅增加“跌出当日有效全池前40%”退出，保留带至少覆盖前3名。
原三日确认/限亏/保本损保留。
无排名的出池或数据缺失持仓只按自身规则评估，不把缺测当成排名垫底。
下载 im_rank_exit_base_* 或 im_rank_exit_rank40_* 的 nav/signals/orders 三个CSV。
绩效取平台完整账户净值；两次运行须使用相同区间、资金和频率。
"""
import builtins as py
import csv
import io
import math

import numpy as np

from jqdata import *

RANK_EXIT_PCT = None
TOPN = 3
STOP_COST_PCT = 0.08                 # 限亏：跌破成本 -8% 清仓
BREAKEVEN_TRIGGER = 0.12             # 浮盈曾达 +12% 后保本线抬到成本 +1%
BREAKEVEN_STOP = 0.01                # 保本微利线（成本×1.01）；布尔置位只升不降，清仓重置
SCORE_EXIT_CONFIRM = 3               # score≤0 连续 N 日才清仓（churn 缓冲，限亏不延迟）
CROWD_LOOKBACK, CROWD_MIN_OBS, CROWD_MAX = 250, 60, 101.0   # 量价热度准入阈值沿用 101 分（闸常开），热度仍计算入日志
MIN_BARS = 250
LIQ_AMT20_MIN = 50_000_000.0
REBUILD_EVERY = 20
CORR_DEDUPE = 0.85
MATURE_DAYS = 365
SCORE_DAYS = 25                      # 25 根已完成收盘 + 当日现价

# 名称剔除词：宽基/规模/风格/债券/货币/商品/跨境——行业与主题保留
EXCLUDE_KW = ('沪深300', '中证500', '中证1000', '中证800', '中证全指', '2000', '200', '中证A500', 'A500', 'A50', 'A100',
 '上证50', '上证180', '上证380', '上证580', '上证指数', '科创50', '科创100', '科创综', '创业板50', '创业板综', '创业板指',
 '创业板', '双创', '北证', '深证100', '基本面50', '红利', '股息', '国债', '政金', '信用', '债券', '转债', '货币', '现金',
 '黄金', '白银', '原油', '豆粕', '商品', '纳斯达克', '纳指', '标普', '道琼', '日经', '德国', '法国', '亚太', '东南亚', '恒生',
 '香港', 'H股', '港股', '央企', '国企', '龙头', 'ESG', '养老', 'FOF', '联动', '增强', '价值', '成长', '质量', '低波',
 '动量', '多因子', '自由现金流', 'HK', '225', '东证', '中韩', '美国', '恒指', '油气', '债', '上海金', '短融', '日利',
 '添益', '快线')

# 代码段硬剔除，不依赖命名：513xxx 沪市跨境 ETF / 518xxx 上金所黄金现货 ETF
EXCLUDE_CODE_PREFIXES = ('513', '518')


def initialize(context):
    if not math.isclose(float(context.portfolio.starting_cash), 100000.0, abs_tol=.01):
        raise ValueError('请将初始资金设为100000元，两组使用相同日期与日频设置')
    if RANK_EXIT_PCT not in (None, .40):
        raise ValueError('本实验仅比较 None 与 0.40，不扫描排名阈值')
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
    g.neg_run = {}                        # score≤0 连续计数（确认期用），卖出时清除
    g.be = {}                             # 保本置位：code -> True（浮盈曾达 +12%），卖出时清除
    g.study_prefix = 'im_rank_exit_%s_' % ('base' if RANK_EXIT_PCT is None else 'rank40')
    g.study_peak = float(context.portfolio.starting_cash)
    g.study_drawdown = 0.0
    for suffix, columns in (('nav', NAV_COLUMNS), ('signals', SIGNAL_COLUMNS),
                            ('orders', ORDER_COLUMNS)):
        _write_study(suffix, columns, [], append=False)
    log.info('[排名退出对照] mode=%s corr=%.2f topn=%d confirm=%d 14:55 万一无最低佣金'
             % (RANK_EXIT_PCT, CORR_DEDUPE, TOPN, SCORE_EXIT_CONFIRM))
    run_daily(run_rotation, time='14:55')


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


def rebuild_pool(dt):
    """规则动态池：时点候选 → 跨境/名称/成熟度/流动性/相关性去重（全用当时数据）。"""
    secs = get_all_securities(['etf'], date=dt.strftime('%Y-%m-%d'))
    n_all = len(secs)
    today = dt.date()
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
        cands.append(code)
    n_mature = len(cands)
    liq = []
    if cands:
        amt20 = history(20, '1d', 'money', security_list=cands).mean(axis=0)
        liq = [c for c in cands if amt20.get(c, 0) == amt20.get(c, 0)
               and amt20[c] >= LIQ_AMT20_MIN]
    if len(liq) <= TOPN:
        log.info("池重建 %s: 全表 %d | 跨境剔 %d | 名称过 %d | 成熟 %d | 流动 %d（去重跳过）"
                 % (dt.strftime('%Y-%m-%d'), n_all, n_xb, n_name, n_mature, len(liq)))
        return liq
    rets = history(250, '1d', 'close', security_list=liq).pct_change()
    corr = rets.corr(min_periods=120)
    kept = []
    for c in sorted(liq, key=lambda x: -amt20[x]):
        ok = True
        for k in kept:
            r = corr.at[c, k]
            if r == r and r >= CORR_DEDUPE:     # NaN 视为不可比 → 保留
                ok = False
                break
        if ok:
            kept.append(c)
    log.info("池重建 %s: 全表 %d | 跨境剔 %d | 名称过 %d | 成熟 %d | 流动 %d | 去重后 %d"
             % (dt.strftime('%Y-%m-%d'), n_all, n_xb, n_name, n_mature, len(liq),
                len(kept)))
    return kept


def pct_in_window(vals, lookback):
    """自身近 N 日窗口内分位（0-100），不足 60 观测返回 None（镜像 v1）。"""
    w = vals[-lookback:]
    if len(w) < CROWD_MIN_OBS:
        return None
    last = w[-1]
    return round(sum(1 for v in w if v < last) / float(len(w)) * 100, 1)


def momentum_score(close_tail, price):
    """动量评分器：保留原回归，修正R²有效权重与中心均值。

    close_tail 末 SCORE_DAYS 根收盘 + 当日 last_price 共 26 点；
    对数价格加权回归（w=linspace(1,2)），score=年化×w²加权R²；
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
        # polyfit(w=w)的残差平方权重为w²，R²与拟合使用同一权重。
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
        g.pool = rebuild_pool(dt)
    cd = get_current_data()
    today = dt.strftime('%Y-%m-%d')

    closes = history(CROWD_LOOKBACK, '1d', 'close', security_list=g.pool) if g.pool else None
    money = history(CROWD_LOOKBACK, '1d', 'money', security_list=g.pool) if g.pool else None
    rowsum = money.sum(axis=1) if money is not None else None

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

    # 先检查自身趋势、成本限亏和保本损，再检查可选的排名退出。
    held_before = {s for s, p in context.portfolio.positions.items() if p.total_amount > 0}
    export_rows = list(rows)
    score_of = {r['code']: r['score'] for r in rows}
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0:
            continue
        if cd[sec].paused:
            continue                                # 停牌不评估，仍持有
        px = float(cd[sec].last_price)
        if px <= 0:
            continue
        if px >= pos.avg_cost * (1.0 + BREAKEVEN_TRIGGER):
            g.be[sec] = True
        latched = g.be.get(sec, False)
        stop_ratio = (1.0 + BREAKEVEN_STOP) if latched else (1.0 - STOP_COST_PCT)
        below_stop = px <= pos.avg_cost * stop_ratio
        score = score_of.get(sec)
        if score is None:                           # 出池持仓：直接取 25 根评分
            h = attribute_history(sec, SCORE_DAYS, '1d', ['close'],
                                  skip_paused=True)
            if h is not None and len(h) >= SCORE_DAYS:
                score = momentum_score(list(h['close'].values), px)
            export_rows.append(dict(code=sec, price=px, score=score, crowd=None))
        neg = g.neg_run.get(sec, 0)
        if score is not None:
            neg = 0 if score > 0 else neg + 1
            g.neg_run[sec] = neg
        rank_exit = (RANK_EXIT_PCT is not None and sec in rank
                     and rank[sec] > py.max(TOPN, math.ceil(RANK_EXIT_PCT * n)))
        trend_exit = score is not None and neg >= SCORE_EXIT_CONFIRM
        if not (below_stop or trend_exit or rank_exit):
            continue
        if below_stop:
            reason = "保本损" if latched else "跌破成本-8%"
        elif trend_exit:
            reason = "score转负x%d日" % neg
        else:
            reason = "排名跌出前40%"
        score_text = '%.4f' % score if score is not None else 'missing'
        _submit_sell(context, sec, reason, "score=%s 现价%.3f rank=%s/%d"
                     % (score_text, px, rank.get(sec), n))

    # 后买：score>0 且量价热度闸常开（本版 CROWD_MAX=101）的前空槽数等权
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
               or (r['crowd'] is not None and r['crowd'] >= CROWD_MAX):
                continue
            px = r['price']
            amount = min(int(per / (px * 1.0001) // 100) * 100,
                         int(cash / (px * 1.0001) // 100) * 100)
            if amount < 100:
                continue
            order(code, amount)
            log.info("买入 %s %s %d份 @%.3f score=%.4f 量价热度=%s分 rank %d/%d"
                     % (code, r['name'], amount, px, r['score'],
                        r['crowd'], rank[code], n))
            bought += 1
    if getattr(g, 'study_prefix', None):
        _write_study('signals', SIGNAL_COLUMNS, [
            (today, r['code'], r['price'], r['score'], rank.get(r['code']), n,
             int(r['code'] in held_before), '|'.join(g.pool)) for r in export_rows])


NAV_COLUMNS = ('date', 'equity', 'cash', 'invested', 'return_pct',
               'drawdown_pct', 'holdings', 'pool')
SIGNAL_COLUMNS = ('date', 'code', 'price', 'score', 'rank', 'n', 'held_before', 'pool')
ORDER_COLUMNS = ('date', 'order_id', 'code', 'side', 'requested', 'filled',
                 'price', 'commission', 'reason')


def _write_study(suffix, columns, rows, append=True):
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerows(rows if append else [columns])
    write_file(g.study_prefix + suffix + '.csv', output.getvalue(), append=append)


def _export_close(context):
    if not getattr(g, 'study_prefix', None):
        return
    today = context.current_dt.strftime('%Y-%m-%d')
    equity = float(context.portfolio.total_value)
    g.study_peak = py.max(g.study_peak, equity)
    drawdown = (equity / g.study_peak - 1) * 100
    g.study_drawdown = py.min(g.study_drawdown, drawdown)
    positions = {c: p for c, p in context.portfolio.positions.items() if p.total_amount > 0}
    holdings = '|'.join('%s:%d' % (c, p.total_amount) for c, p in py.sorted(positions.items()))
    invested = float(py.sum(p.value for p in positions.values()))
    _write_study('nav', NAV_COLUMNS, [(today, equity, float(context.portfolio.available_cash),
        invested, (equity / context.portfolio.starting_cash - 1) * 100,
        drawdown, holdings, '|'.join(g.pool))])
    records = []
    for order_id, item in get_orders().items():
        meta = g.sell_orders.get(str(order_id), {})
        records.append((today, str(order_id), meta.get('code', getattr(item, 'security', '')),
            'sell' if meta else 'buy', getattr(item, 'amount', None), item.filled,
            item.price, getattr(item, 'commission', None), meta.get('reason', 'entry')))
    _write_study('orders', ORDER_COLUMNS, records)


# BEGIN SHARED PLATFORM LEDGER
# Actual-fill accounting kept identical to lM_v3_3_2.py; no local imports on JoinQuant.


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
    _export_close(context)


def on_strategy_end(context):
    """Report confirmed closed trades, partial realized P&L and reconciliation."""
    # JoinQuant globals can shadow Python's sum with numpy.sum.
    import builtins as python_builtins

    _sync_sell_orders(context)
    if getattr(g, 'study_prefix', None):
        log.info('[排名退出对照结束] mode=%s 收盘最大回撤%.2f%%；比较两组独立运行的完整账户导出'
                 % (RANK_EXIT_PCT, g.study_drawdown))
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
