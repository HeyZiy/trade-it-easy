# -*- coding: utf-8 -*-
# roe_rotation_jq —— ROE 质量轮动的聚宽复刻（对照 strategy/roe_quality_backtest.md
# 的东方财富智能回测口径）
#
# 规则（与文档逐条对应）：
#   选股（信号日收盘，每 20 个交易日一批）：
#     ROE（最新报告期年化，见下方差异）> 12；PE(TTM) < 30；PB < 5；
#     净利润同比 > 10（同一报告期）；60 日年化波动率 < 35%；
#     硬剔除：ST/退市、科创板、创业板、北交所、信号日停牌、上市不足 61 日、
#             未来 90 自然日有解禁。
#   买入：信号日次日开盘等权买入，优先级 = 信号日涨跌幅降序，最多 TOP_N 只；
#     开盘涨停（day_open ≥ high_limit−0.001）跳过（实际买不进）。
#   卖出：整批持仓满 20 个交易日在下一轮动日开盘全部卖出（停牌顺延到可卖开盘），
#     无止盈止损、无市场门控（文档明确）。
#   轮动：每 20 个交易日一批同进同出（≈年 12 轮）；候选不足留现金（无现金门控）。
#
# 口径差异（东财有源、聚宽无源——本回测偏宽松，结论需打折）：
#   1) 基本面走 get_fundamentals(..., date=T)：avoid_future_data=True 的回测里
#      引擎禁用 statDate（抛 FutureDataError），date= 只能拿到"该日之前已披露的
#      最近一期"，Q1~年报混着来；indicator.roe 是报告期累计值，故按季度序号
#      年化（×4/n）后再比阈值。比原"上年年报"口径更接近东财的 TTM/最新财报，
#      代价是季节性行业（Q1 占全年比重高）会被 ×4 高估；
#   2) 估值表在 14:55 只能取 T-1 快照（引擎：当日市值表 15:00 后才可得），
#      所以 PE/PB 是前一交易日收盘口径，比东财晚一个交易日；
#   3) 监管函/工作函、减持计划、新规风险：聚宽无数据源，未实现；
#   4) 印花税按卖出 0.1% 全程（2023-08-28 后实际 0.05%，本回测略偏保守）；
#   5) 持仓数量是东财表单里存在但没抄进文档的参数，默认 TOP_N=10——按实际改常量。

# 策略收益
# 86.10%
# 策略年化收益
# 6.18%
# 超额收益
# 50.57%
# 基准收益
# 23.60%
# 阿尔法
# 0.035
# 贝塔
# 0.672
# 夏普比率
# 0.111
# 胜率
# 0.486
# 盈亏比
# 1.220
# 最大回撤 
# 38.10%
# 索提诺比率
# 0.151
# 日均超额收益
# 0.02%
# 超额收益最大回撤
# 45.05%
# 超额收益夏普比率
# 0.002
# 日胜率
# 0.506
# 盈利次数
# 613
# 亏损次数
# 649
# 信息比率
# 0.256
# 策略波动率
# 0.195
# 基准波动率
# 0.189
# 最大回撤区间
# 2017/07/21,2018/10/18

import math

import pandas as pd

from jqdata import *

TOP_N = 10               # 最大持仓数（东财表单未抄入文档，默认 10）
HOLD_DAYS = 20           # 持有交易日数
ROTATE_EVERY = 20        # 轮动间隔（交易日）
ROE_MIN = 12.0           # ROE 下限（%，最新报告期年化后）
PE_MAX = 30.0            # PE(TTM) 上限
PB_MAX = 5.0             # PB 上限
NP_YOY_MIN = 10.0        # 净利润同比增长下限（%，同一报告期）
VOL60_MAX = 35.0         # 60 日年化波动率上限（%）
VOL_LOOKBACK = 61        # 收益样本（含信号日）
LIMIT_UP_PAD = 0.001     # 开盘涨停判定容差
FEE_COMMISSION = 0.00025 # 佣金单边
FEE_TAX = 0.001          # 卖出印花税（2023-08 前，全程保守）
BAN_WINDOW = 90          # 未来解禁窗口（自然日）


def initialize(context):
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
    run_daily(on_open, time='9:31')
    run_daily(on_close, time='14:55')
    run_daily(mark_nav, time='14:56')


def mark_nav(context):
    g.curve.append((context.current_dt, context.portfolio.total_value))


def _report_quarter(stat_date, dt):
    """报告期季度序号（年化分母）：优先 statDate（'2025q3'→3），缺失按查询月推。"""
    try:
        return max(1, min(4, int(str(stat_date)[-1])))
    except (TypeError, ValueError):
        return (dt.month - 1) // 3 + 1


def _prev_trade_day(dt):
    """T-1 交易日：14:55 取不到当日市值表（引擎 15:00 后才放）。"""
    return get_trade_days(end_date=dt, count=2)[0]


def _mainboard_pool(context):
    """全 A 主板（时点）：剔科创板(68x)、创业板(30x)、北交所(4/8/92 前缀)。"""
    secs = get_all_securities(['stock'], date=context.current_dt)
    return [c for c in secs.index
            if not (c[:2] in ('68', '30') or c[0] in ('4', '8') or c[:2] == '92')]


def _st_filter(cands, dt):
    """剔除信号日 ST（含*ST）。"""
    try:
        st = get_extras('is_st', security_list=cands, end_date=dt, count=1)
        return [c for c in cands if not bool(st[c].iloc[-1])]
    except Exception:
        return cands


def _ban_filter(cands, dt):
    """剔除未来 BAN_WINDOW 自然日内有解禁的标的（数据缺失时放行）。"""
    try:
        rows = get_locked_shares(stock_list=cands, start_date=dt,
                                 end_date=dt + pd.Timedelta(days=BAN_WINDOW))
        banned = set(rows['code']) if rows is not None and len(rows) else set()
        return [c for c in cands if c not in banned]
    except Exception:
        return cands


def build_signal(context):
    """信号日收盘选股：基本面 → ST → 估值 → 停牌/波动率 → 解禁 → 涨跌幅降序。"""
    dt = context.current_dt
    pool = _mainboard_pool(context)

    ind = get_fundamentals(
        query(indicator.code, indicator.roe,
              indicator.inc_net_profit_year_on_year, indicator.statDate),
        date=dt)
    ind = ind.set_index('code')
    n = ind['statDate'].apply(lambda s: _report_quarter(s, dt))
    ind = ind[(ind['roe'] * 4.0 / n > ROE_MIN)
              & (ind['inc_net_profit_year_on_year'] > NP_YOY_MIN)]
    cands = [c for c in pool if c in ind.index]
    if not cands:
        return []

    cands = _st_filter(cands, dt)
    if not cands:
        return []

    val = get_fundamentals(
        query(valuation).filter(valuation.code.in_(cands)), date=_prev_trade_day(dt))
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
    return [c for c, _ in sorted(chg.items(), key=lambda kv: -kv[1])]


def on_close(context):
    """14:55：轮动日算次日买入名单。"""
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY != 0:
        return
    ranked = build_signal(context)
    g.batch_sizes.append(len(ranked))
    g.pending = (context.current_dt, ranked)


def on_open(context):
    """9:31：先卖（到期整批 + 停牌顺延）→ 后买（上一收盘名单等权）。"""
    dt = context.current_dt
    cd = get_current_data()

    # 卖出：持有 ≥20 个交易日的全部卖出（整批轮动日必然全到期，停牌的顺延）
    sold_today = set()
    for code, pos in list(context.portfolio.positions.items()):
        if pos.total_amount <= 0:
            continue
        since = g.hold_since.get(code)
        if since is None:
            continue
        held = len(get_trade_days(start_date=since, end_date=dt)) - 1
        if held >= HOLD_DAYS:
            order_target(code, 0)
            sold_today.add(code)

    # 买入：等权 TOP_N，跳过停牌与开盘涨停
    bought_today = set()
    if g.pending is not None:
        _, ranked = g.pending
        g.pending = None
        held = {c for c, p in context.portfolio.positions.items()
                if p.total_amount > 0}
        per = context.portfolio.total_value / TOP_N
        for code in ranked[:TOP_N]:
            if code in held or cd[code].paused:
                continue
            if cd[code].day_open >= cd[code].high_limit - LIMIT_UP_PAD:
                continue
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
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    years = max((curve[-1][0] - curve[0][0]).days / 365.25, 1e-9)
    tot = vals[-1] / context.portfolio.starting_cash - 1
    cagr = (vals[-1] / context.portfolio.starting_cash) ** (1 / years) - 1

    # 年度收益
    last = {}
    for d, v in curve:
        last[d.year] = v
    yearly, prev = {}, context.portfolio.starting_cash
    for y in sorted(last):
        yearly[y] = round((last[y] / prev - 1) * 100, 1)
        prev = last[y]

    # 多起跑点累计
    starts = []
    for ym in ('2019', '2020', '2021', '2022', '2023', '2024'):
        pts = [(d, v) for d, v in curve if d.strftime('%Y') >= ym]
        if pts:
            starts.append("%s:+%.1f%%" % (ym, (pts[-1][1] / pts[0][1] - 1) * 100))

    bs = g.batch_sizes
    lines = ["", "==================== ROE 质量轮动 汇总 ====================",
             "窗口: %s ~ %s | TOP_N=%d | 持有 %d 交易日 | 每轮候选数 均%.1f/最小%d/最大%d"
             % (curve[0][0].strftime('%F'), curve[-1][0].strftime('%F'), TOP_N,
                HOLD_DAYS,
                sum(bs) / max(len(bs), 1), min(bs) if bs else 0, max(bs) if bs else 0),
             "总收益 %+7.1f%%  年化 %+5.1f%%  最大回撤 %6.1f%%" % (tot * 100, cagr * 100, mdd * 100),
             "年度收益: %s" % yearly,
             "多起跑点累计: %s" % "  ".join(starts),
             "==========================================================="]
    for ln in lines:
        log.info(ln)
