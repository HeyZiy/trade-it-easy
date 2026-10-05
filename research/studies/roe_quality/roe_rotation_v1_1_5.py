# -*- coding: utf-8 -*-
# roe_rotation_v1_1_5 —— 财务筛选移除对照（单文件，直接粘贴聚宽回测编辑器）
# 默认FINANCIAL_FILTERS=False：不查财务/估值，不筛ROE、净利润同比、PE或PB。
# 实验问题：财务质量/估值筛选整体是否改善原1日涨幅排序策略？
# 保留历史全A主板、ST/停牌/解禁过滤、61根行情要求、60日波动率<35%。
# 保留原1日涨幅降序、TOP_N=10、20交易日轮动及持有、9:31下单和原费率。
# 建议100万元、2016-01-04~2026-09-01，与100万元v1.1.2的+95.7%对照。
# FINANCIAL_FILTERS=True恢复原财务筛选，供基线核对；不要同时改其他参数。
# 本实验测试筛选整体作用，不能单独归因于ROE，仍保留波动率筛选。
# 资金分配、到期清仓后回买、排名归因、14:56净值采样沿用v1.1.2。
# 解禁/ST异常时放行及印花税全程0.1%沿用基线；此轮不顺带修正。
# 关闭财务筛选后，不再因缺失/未披露财报排除股票；上市年龄仍由61根行情约束。
# 候选池变大会增加行情请求量；不增加新的逐股票财务请求。
# 同日回买旧仓与新仓分开归因；成本口径平仓盈亏不含期末浮盈。
# 排名画线是累计平仓贡献/初始资金，不是各排名独立策略收益。

# 策略收益
# -65.14%
# 策略年化收益
# -9.67%
# 超额收益
# -71.79%
# 基准收益
# 23.60%
# 阿尔法
# -0.122
# 贝塔
# 0.733
# 夏普比率
# -0.576
# 胜率
# 0.429
# 盈亏比
# 0.846
# 最大回撤 
# 76.15%
# 索提诺比率
# -0.780
# 日均超额收益
# -0.04%
# 超额收益最大回撤
# 82.90%
# 超额收益夏普比率
# -0.779
# 日胜率
# 0.478
# 盈利次数
# 537
# 亏损次数
# 715
# 信息比率
# -0.590
# 策略波动率
# 0.237
# 基准波动率
# 0.189
# 最大回撤区间
# 2016/11/22,2026/07/24


import hashlib
import math

import pandas as pd

from jqdata import *
import builtins as _python_builtins

TOP_N = 10               # 最大持仓数（东财表单未抄入文档，默认 10）
HOLD_DAYS = 20           # 持有交易日数
ROTATE_EVERY = 20        # 轮动间隔（交易日）
ROE_MIN = 12.0           # ROE 下限（%，最新单季度×4的年化近似）
PE_MAX = 30.0            # PE(TTM) 上限
PB_MAX = 5.0             # PB 上限
NP_YOY_MIN = 10.0        # 净利润同比增长下限（%，最新单季度）
VOL60_MAX = 35.0         # 60 日年化波动率上限（%）
VOL_LOOKBACK = 61        # 61根日线，形成60个收益样本，截止T-1
LIMIT_UP_PAD = 0.001     # 开盘涨停判定容差
FEE_COMMISSION = 0.00025 # 佣金单边
FEE_TAX = 0.001          # 卖出印花税（2023-08 前，全程保守）
BAN_WINDOW = 90          # 未来解禁窗口（自然日）
FINANCIAL_FILTERS = False  # False：本轮移除财务筛选；True：恢复v1.1.2基线
RANK_MODE = 'prev_change'  # 本轮用原排序统计前十；仍支持 random 对照
RANDOM_SEED = 0          # 随机对照种子；建议预先固定 0、1、2，分别运行
PLOT_DIAGNOSTICS = False  # 原候选/仓位曲线开关；默认关闭，避免与排名线混叠
PLOT_RANK_CONTRIBUTIONS = True  # 第01~10名累计平仓贡献（占初始资金的百分点）


def initialize(context):
    if RANK_MODE not in ('random', 'prev_change'):
        raise ValueError('RANK_MODE must be random or prev_change')
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark("000300.XSHG")
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    set_order_cost(OrderCost(open_commission=FEE_COMMISSION,
                             close_commission=FEE_COMMISSION,
                             open_tax=0, close_tax=FEE_TAX,
                             min_commission=0), type='stock')
    g.day = 0
    g.pending = None        # (信号日, 排序后候选名单)
    g.batch_sizes = []      # 每轮候选数
    g.hold_since = {}       # code → 买入日（算持有交易日数）
    g.curve = []
    g.diag = {}
    g.diag_prev_pool = None
    g.diag_prev_top = None
    g.diag_prev_reference_top = None
    g.diag_reference_ranked = []
    g.diag_prev_nav = context.portfolio.starting_cash
    g.rank_stats = {i: _empty_rank_stats() for i in _python_builtins.range(1, _python_builtins.max(10, TOP_N) + 1)}
    g.rank_owned = {}       # 仅在实际买入成交后赋予排名
    g.rank_today_buy = {}   # 本日买入意图；不代表已成交
    g.rank_today_old = {}   # 卖出前的旧仓排名；避免同日回买覆盖
    g.rank_years = {}
    g.rank_processed_date = None
    g.rank_unattributed_sells = 0
    log.info("ROE v1.1.5：财务筛选=%s；排序=%s，种子=%s；日线截止T-1；9:31下单"
             % ('开' if FINANCIAL_FILTERS else '关', RANK_MODE, RANDOM_SEED))
    run_daily(on_open, time='9:31')
    run_daily(on_close, time='14:55')
    run_daily(mark_nav, time='14:56')


def mark_nav(context):
    g.curve.append((context.current_dt, context.portfolio.total_value))


def _rank_candidates(cands, change, asof):
    if RANK_MODE == 'prev_change':
        # 保留 v1.1 的稳定排序及同分时输入顺序。
        return _python_builtins.sorted(cands, key=lambda code: -change[code])
    if RANK_MODE != 'random':
        raise ValueError('RANK_MODE must be random or prev_change')
    prefix = '%s|%s|' % (RANDOM_SEED, pd.Timestamp(asof).strftime('%Y-%m-%d'))
    return _python_builtins.sorted(cands, key=lambda code: (
        hashlib.sha256((prefix + code).encode('utf-8')).hexdigest(), code))


def _update_signal_diagnostics(ranked):
    pool, top = _python_builtins.set(ranked), _python_builtins.set(ranked[:TOP_N])
    reference_top = _python_builtins.set(g.diag_reference_ranked[:TOP_N])
    g.diag['候选数'] = _python_builtins.len(pool)
    for name, current, previous in (
            ('候选留存率_pct', pool, g.diag_prev_pool),
            ('前%d留存率_pct' % TOP_N, top, g.diag_prev_top),
            ('原涨幅前%d留存率_pct' % TOP_N, reference_top, g.diag_prev_reference_top)):
        g.diag.pop(name, None)
        if previous:
            g.diag[name] = 100.0 * _python_builtins.len(current & previous) / _python_builtins.len(previous)
    g.diag_prev_pool = pool
    g.diag_prev_top = top
    g.diag_prev_reference_top = reference_top


def _empty_rank_stats():
    return _python_builtins.dict(selected=0, buys=0, sells=0, wins=0, pnl=0.0, cost=0.0)


def _collect_rank_orders(context, orders):
    date = context.current_dt.date()
    if g.rank_processed_date == date:
        return
    # 当前策略先卖再买。订单字典顺序不可信，强制先归因旧仓，再登记新仓。
    for order in _python_builtins.sorted(orders.values(), key=lambda o: _python_builtins.bool(o.is_buy)):
        if order.filled <= 0:
            continue
        if order.is_buy:
            rank = g.rank_today_buy.get(order.security)
            if rank is not None:
                g.rank_stats[rank]['buys'] += 1
                g.rank_owned[order.security] = rank
            continue
        rank = g.rank_today_old.get(order.security)
        cost = getattr(order, 'avg_cost', None)
        if rank is None or cost is None or not math.isfinite(cost) or cost <= 0:
            g.rank_unattributed_sells += 1
            continue
        pnl = order.filled * (order.price - cost)
        stats = g.rank_stats[rank]
        stats['sells'] += 1
        stats['wins'] += pnl > 0
        stats['pnl'] += pnl
        stats['cost'] += order.filled * cost
        annual = g.rank_years.setdefault(date.year, {})
        annual[rank] = annual.get(rank, 0.0) + pnl
    g.rank_owned = {code: rank for code, rank in g.rank_owned.items()
                    if context.portfolio.positions.get(code) is not None
                    and context.portfolio.positions[code].total_amount > 0}
    g.rank_processed_date = date


def _log_rank_summary(context):
    log.info('逐排名归因：成本口径平仓盈亏，不含期末浮盈；非独立策略收益。')
    log.info('排名 | 入选 | 买入成交 | 未买入 | 卖出成交 | 平仓盈亏元 | 成本收益率 | 卖出胜率 | 未平仓只数')
    for rank, stats in _python_builtins.sorted(g.rank_stats.items()):
        ret = '%+.2f%%' % (100 * stats['pnl'] / stats['cost']) if stats['cost'] else '无样本'
        win = '%.1f%%' % (100 * stats['wins'] / stats['sells']) if stats['sells'] else '无样本'
        opened = _python_builtins.sum(value == rank for value in g.rank_owned.values())
        log.info('%02d | %d | %d | %d | %d | %+.2f | %s | %s | %d' % (
            rank, stats['selected'], stats['buys'], stats['selected'] - stats['buys'],
            stats['sells'], stats['pnl'], ret, win, opened))
    for year, ranks in _python_builtins.sorted(g.rank_years.items()):
        log.info('排名年度平仓贡献 %d（元，按卖出年）：%s' % (
            year, {i: _python_builtins.round(ranks.get(i, 0.0), 2) for i in _python_builtins.sorted(g.rank_stats)}))
    log.info('未归因卖出订单数：%d；非零时需核对排名/成本数据。' % g.rank_unattributed_sells)


def after_trading_end(context):
    """收盘后按实际已成交数量画线；不改变信号、仓位或交易调用。"""
    nav = context.portfolio.total_value
    orders = get_orders()
    _collect_rank_orders(context, orders)
    values = {}
    if PLOT_RANK_CONTRIBUTIONS:
        values.update({'R%02d_平仓贡献_pct' % rank:
                       _python_builtins.round(100.0 * stats['pnl'] / context.portfolio.starting_cash, 2)
                       for rank, stats in g.rank_stats.items()})
    if PLOT_DIAGNOSTICS:
        buy_amount, sell_amount = 0.0, 0.0
        bought, sold = _python_builtins.set(), _python_builtins.set()
        for order in orders.values():
            if order.filled <= 0:
                continue
            amount = order.filled * order.price
            if order.is_buy:
                buy_amount += amount
                bought.add(order.security)
            else:
                sell_amount += amount
                sold.add(order.security)
        values.update({name: _python_builtins.round(value, 2) for name, value in g.diag.items()})
        values.update({
            '持仓数': _python_builtins.sum(p.total_amount > 0 for p in context.portfolio.positions.values()),
            '仓位_pct': 100.0 * (nav - context.portfolio.cash) / nav if nav > 0 else 0.0,
            '同日回买数': _python_builtins.len(bought & sold),
        })
        if g.diag_prev_nav > 0:
            values['每日双边换手_pct'] = 100.0 * (buy_amount + sell_amount) / g.diag_prev_nav
    if values:
        record(**{name: _python_builtins.round(value, 2) for name, value in values.items()})
    g.diag_prev_nav = nav


def _report_date(value):
    """解析真实季度末日期；兼容 YYYYqN，缺失或非法值不猜测。"""
    if pd.isna(value):
        return pd.NaT
    text = _python_builtins.str(value).strip().lower()
    try:
        if (_python_builtins.len(text) == 6 and text[:4].isdigit()
                and text[4] == 'q' and text[5] in '1234'):
            return pd.Period(text, freq='Q').end_time.normalize()
        date = pd.Timestamp(value).normalize()
        return date if date.is_quarter_end else pd.NaT
    except (TypeError, ValueError, OverflowError):
        return pd.NaT


def _prepare_fundamentals(rows, asof):
    """仅保留截止日已披露的有效季度指标，ROE(%)按单季度×4年化近似。"""
    ind = rows.copy().set_index('code')
    cutoff = pd.Timestamp(asof).normalize()
    report_dates = ind['statDate'].apply(_report_date)
    publication_dates = pd.to_datetime(ind['pubDate'], errors='coerce')
    visible = (report_dates.notna() & publication_dates.notna()
               & (report_dates <= cutoff) & (publication_dates <= cutoff))
    ind = ind.loc[visible].copy()
    for field in ('roe', 'inc_net_profit_year_on_year'):
        ind[field] = pd.to_numeric(ind[field], errors='coerce')
    ind['roe_ann'] = ind['roe'] * 4.0
    ind = ind.replace([_python_builtins.float('inf'), _python_builtins.float('-inf')], _python_builtins.float('nan'))
    return ind.dropna(subset=['roe_ann', 'inc_net_profit_year_on_year'])


def _prev_trade_day(dt):
    """T-1 交易日：统一财务、估值和历史日线的可见截止日。"""
    return get_trade_days(end_date=dt.date(), count=2)[0]


def _mainboard_pool(context):
    """全 A 主板（时点）：剔科创板(68x)、创业板(30x)、北交所(4/8/92 前缀)。"""
    secs = get_all_securities(['stock'], date=context.current_dt)
    return [c for c in secs.index
            if not (c[:2] in ('68', '30') or c[0] in ('4', '8') or c[:2] == '92')]


def _st_filter(cands, dt):
    """剔除信号日 ST（含*ST）。"""
    try:
        st = get_extras('is_st', security_list=cands, end_date=dt, count=1)
        return [c for c in cands if not _python_builtins.bool(st[c].iloc[-1])]
    except Exception:
        return cands


def _ban_filter(cands, dt):
    """剔除未来 BAN_WINDOW 自然日内有解禁的标的（数据缺失时放行）。"""
    try:
        rows = get_locked_shares(stock_list=cands, start_date=dt,
                                 end_date=dt + pd.Timedelta(days=BAN_WINDOW))
        banned = _python_builtins.set(rows['code']) if rows is not None and _python_builtins.len(rows) else _python_builtins.set()
        return [c for c in cands if c not in banned]
    except Exception:
        return cands


def build_signal(context):
    """14:55 选股：按开关保留/移除财务筛选，其余规则沿用原排序基线。"""
    g.diag_reference_ranked = []  # 所有空池提前返回也必须清空影子名单
    dt = context.current_dt
    asof = _prev_trade_day(dt)
    pool = _mainboard_pool(context)

    if FINANCIAL_FILTERS:
        ind = get_fundamentals(
            query(indicator.code, indicator.roe,
                  indicator.inc_net_profit_year_on_year, indicator.statDate,
                  indicator.pubDate), date=asof)
        ind = _prepare_fundamentals(ind, asof)
        ind = ind[(ind['roe_ann'] > ROE_MIN)
                  & (ind['inc_net_profit_year_on_year'] > NP_YOY_MIN)]
        cands = [c for c in pool if c in ind.index]
        if not cands:
            return []

    else:
        cands = pool
    if not cands:
        return []

    cands = _st_filter(cands, dt)
    if not cands:
        return []

    if FINANCIAL_FILTERS:
        val = get_fundamentals(
            query(valuation).filter(valuation.code.in_(cands)), date=asof)
        val = val.set_index('code')
        val = val[(val['pe_ratio'] < PE_MAX) & (val['pb_ratio'] < PB_MAX)]
        cands = [c for c in cands if c in val.index]
        if not cands:
            return []

    cd = get_current_data()
    cands = [c for c in cands if not cd[c].paused]

    closes = history(VOL_LOOKBACK, '1d', 'close', security_list=cands)
    cands = [c for c in cands if closes[c].notna().sum() >= VOL_LOOKBACK]
    if not cands:
        return []
    rets = closes[cands].pct_change()
    vol60 = rets.std() * math.sqrt(250) * 100
    cands = [c for c in cands if vol60[c] < VOL60_MAX]
    if not cands:
        return []

    cands = _ban_filter(cands, dt)
    if not cands:
        return []

    chg = {c: closes[c].iloc[-1] / closes[c].iloc[-2] - 1 for c in cands}
    g.diag_reference_ranked = _python_builtins.sorted(cands, key=lambda code: -chg[code])
    return _rank_candidates(cands, chg, asof)


def on_close(context):
    """14:55：轮动日用T-1财务/日线生成次交易日名单。"""
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY != 0:
        return
    ranked = build_signal(context)
    _update_signal_diagnostics(ranked)
    g.batch_sizes.append(_python_builtins.len(ranked))
    g.pending = (context.current_dt, ranked)


def on_open(context):
    """9:31：先卖到期持仓，再按前一交易日14:55生成的名单买入。"""
    dt = context.current_dt
    cd = get_current_data()
    g.rank_today_old = _python_builtins.dict(g.rank_owned)
    g.rank_today_buy = {}

    # 卖出：持有 ≥20 个交易日的全部卖出（整批轮动日必然全到期，停牌的顺延）
    sold_today = _python_builtins.set()
    for code, pos in _python_builtins.list(context.portfolio.positions.items()):
        if pos.total_amount <= 0:
            continue
        since = g.hold_since.get(code)
        if since is None:
            continue
        held = _python_builtins.len(get_trade_days(start_date=since, end_date=dt)) - 1
        if held >= HOLD_DAYS:
            order_target(code, 0)
            sold_today.add(code)

    # 买入：等权 TOP_N，跳过停牌与开盘涨停
    bought_today = _python_builtins.set()
    if g.pending is not None:
        _, ranked = g.pending
        g.pending = None
        held = {c for c, p in context.portfolio.positions.items()
                if p.total_amount > 0}
        per = context.portfolio.total_value / TOP_N
        for rank, code in _python_builtins.enumerate(ranked[:TOP_N], start=1):
            g.rank_stats[rank]['selected'] += 1
            if code in held or cd[code].paused:
                continue
            if cd[code].day_open >= cd[code].high_limit - LIMIT_UP_PAD:
                continue
            g.rank_today_buy[code] = rank
            order_target_value(code, per)
            bought_today.add(code)

    # 持仓起始日登记：当日买入一律记今天（覆盖同日卖后回买的旧日期），
    # 顺延旧仓保留原日期
    for code, pos in context.portfolio.positions.items():
        if pos.total_amount > 0 and (code in bought_today
                                     or code not in g.hold_since):
            g.hold_since[code] = dt
    g.hold_since = {c: d for c, d in g.hold_since.items()
                    if context.portfolio.positions.get(c) is not None
                    and context.portfolio.positions[c].total_amount > 0}


def on_strategy_end(context):
    curve = g.curve
    if not curve:
        log.info("无净值曲线（窗口太短）")
        return
    vals = [v for _, v in curve]
    peak, mdd = vals[0], 0.0
    for v in vals:
        peak = _python_builtins.max(peak, v)
        mdd = _python_builtins.min(mdd, v / peak - 1)
    years = _python_builtins.max((curve[-1][0] - curve[0][0]).days / 365.25, 1e-9)
    tot = vals[-1] / context.portfolio.starting_cash - 1
    cagr = (vals[-1] / context.portfolio.starting_cash) ** (1 / years) - 1

    # 年度收益
    last = {}
    for d, v in curve:
        last[d.year] = v
    yearly, prev = {}, context.portfolio.starting_cash
    for y in _python_builtins.sorted(last):
        yearly[y] = _python_builtins.round((last[y] / prev - 1) * 100, 1)
        prev = last[y]

    # 原曲线分段累计：继承当时持仓，并非重新起跑的独立回测
    starts = []
    for ym in ('2019', '2020', '2021', '2022', '2023', '2024'):
        pts = [(d, v) for d, v in curve if d.strftime('%Y') >= ym]
        if pts:
            starts.append("%s:%+.1f%%" % (ym, (pts[-1][1] / pts[0][1] - 1) * 100))

    bs = g.batch_sizes
    lines = ["", "==================== ROE 质量轮动 v1.1.5 汇总 ====================",
             "财务筛选=%s  排序=%s  随机种子=%s"
             % ('开' if FINANCIAL_FILTERS else '关', RANK_MODE, RANDOM_SEED),
             "窗口: %s ~ %s | TOP_N=%d | 持有 %d 交易日 | 每轮候选数 均%.1f/最小%d/最大%d"
             % (curve[0][0].strftime('%F'), curve[-1][0].strftime('%F'), TOP_N,
                HOLD_DAYS,
                _python_builtins.sum(bs) / _python_builtins.max(_python_builtins.len(bs), 1), _python_builtins.min(bs) if bs else 0, _python_builtins.max(bs) if bs else 0),
             "总收益 %+7.1f%%  年化 %+5.1f%%  最大回撤 %6.1f%%" % (tot * 100, cagr * 100, mdd * 100),
             "年度收益: %s" % yearly,
             "曲线分段累计（非独立起跑）: %s" % "  ".join(starts),
             "==========================================================="]
    for ln in lines:
        log.info(ln)
    _log_rank_summary(context)
