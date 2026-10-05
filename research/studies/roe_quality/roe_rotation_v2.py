# -*- coding: utf-8 -*-
# roe_rotation_v2 —— ROE 质量轮动 · 四大类分位数等权合成版
#
# 相对 v1（roe_rotation.py）的改动（只动选股，执行/成本/汇总不变）：
#   v1 是"硬阈值过滤（ROE>12, PE<30, PB<5, 净利同比>10, vol60<35）
#   + 信号日涨跌幅降序排序"两段式；v2 删除全部数值阈值与涨跌幅排序，
#   改为四大类因子横截面分位数等权合成，一次排序取 TOP_N：
#     质量   = ROE 年化（×4/n，沿用 v1 口径）分位数
#     成长   = 净利同比（1%/99% 截尾后）分位数
#     估值   = EP(1/PE) 与 BP(1/PB) 分位数的均值（PE/PB 同属估值大类，合并防止估值隐性双倍权重）
#     低波   = vol60 反向分位数（波动越低分越高）
#   新增绝对底线：ROE 年化 > 0（剔亏损股，同时保证 EP 非负）；
#   PE<=0 或 PB<=0 视为数据异常剔除。
#   硬剔除保留：ST/退市、科创板、创业板、北交所、信号日停牌、
#   上市不足 61 日、未来 90 自然日有解禁。
#   买入/卖出/轮动节奏与 v1 完全一致（次日开盘等权、20 交易日整批轮动、
#   开盘涨停跳过、停牌顺延），保证对照只差选股一段。
#
# 口径差异（与 v1 相同）：
#   1) 基本面走 get_fundamentals(..., date=T)，Q1~年报混着来，
#      indicator.roe 按季度序号年化（×4/n），季节性行业 Q1 会被高估；
#   2) 估值表 14:55 只能取 T-1 快照，PE/PB 晚一个交易日；
#   3) 监管函/减持计划等聚宽无数据源，未实现；
#   4) 印花税按卖出 0.1% 全程（2023-08-28 后实际 0.05%，偏保守）；
#   5) TOP_N=10 为东财表单未抄入文档的参数。

# ==================== v1 基线结果（20 日轮动 + 涨跌幅降序） ====================
# 策略收益 86.10% | 年化 6.18% | 超额收益 50.57%（基准 23.60%）
# 阿尔法 0.035 | 贝塔 0.672 | 夏普 0.111 | 胜率 0.486 | 盈亏比 1.220
# 最大回撤 38.10%（2017/07/21~2018/10/18）
# 超额收益夏普 0.002 | 超额收益最大回撤 45.05% | 信息比率 0.256
# 盈利 613 / 亏损 649
# 附：60 日轮动变体已证伪（同窗口劣于 20 日）。

# ==================== v2 结果（待聚宽回填） ====================
# 策略收益
# -19.13%
# 策略年化收益
# -2.03%
# 超额收益
# -34.57%
# 基准收益
# 23.60%
# 阿尔法
# -0.047
# 贝塔
# 0.705
# 夏普比率
# -0.315
# 胜率
# 0.471
# 盈亏比
# 1.001
# 最大回撤 
# 53.06%
# 索提诺比率
# -0.412
# 日均超额收益
# -0.01%
# 超额收益最大回撤
# 60.93%
# 超额收益夏普比率
# -0.538
# 日胜率
# 0.484
# 盈利次数
# 607
# 亏损次数
# 682
# 信息比率
# -0.276
# 策略波动率
# 0.192
# 基准波动率
# 0.189
# 最大回撤区间
# 2017/08/09,2024/02/05

import math

import pandas as pd

from jqdata import *

TOP_N = 10               # 最大持仓数
HOLD_DAYS = 20           # 持有交易日数
ROTATE_EVERY = 20        # 轮动间隔（交易日）
VOL_LOOKBACK = 61        # 收益样本（含信号日）
WINSOR_P = 0.01          # 成长因子截尾分位（双侧）
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
    """信号日收盘选股：硬剔除 → 全池四因子分位数等权打分 → 降序名单。"""
    dt = context.current_dt
    pool = _mainboard_pool(context)

    pool = _st_filter(pool, dt)
    if not pool:
        return []

    # 基本面（全市场一次取，再收敛到主板池）：ROE>0 底线 + 成长非缺失
    ind = get_fundamentals(
        query(indicator.code, indicator.roe,
              indicator.inc_net_profit_year_on_year, indicator.statDate),
        date=dt)
    ind = ind.set_index('code')
    ind = ind[ind.index.isin(pool)]
    n = ind['statDate'].apply(lambda s: _report_quarter(s, dt))
    ind['roe_ann'] = ind['roe'] * 4.0 / n
    ind = ind[(ind['roe_ann'] > 0)
              & ind['inc_net_profit_year_on_year'].notna()]
    cands = list(ind.index)
    if not cands:
        return []

    cd = get_current_data()
    cands = [c for c in cands if not cd[c].paused]
    cands = _ban_filter(cands, dt)
    if not cands:
        return []

    # 估值（T-1 快照）：PE/PB 必须为正，否则 EP/BP 无意义
    val = get_fundamentals(
        query(valuation.code, valuation.pe_ratio, valuation.pb_ratio)
        .filter(valuation.code.in_(cands)), date=_prev_trade_day(dt))
    val = val.set_index('code')
    val = val[(val['pe_ratio'] > 0) & (val['pb_ratio'] > 0)]
    cands = [c for c in cands if c in val.index]
    if not cands:
        return []

    # 波动率：历史不足 61 日（上市太新）剔除
    closes = history(VOL_LOOKBACK, '1d', 'close', security_list=cands)
    cands = [c for c in cands if closes[c].notna().sum() >= VOL_LOOKBACK]
    if not cands:
        return []
    rets = closes[cands].pct_change()
    vol60 = rets.std() * math.sqrt(250)

    df = pd.DataFrame({
        'roe': ind.loc[cands, 'roe_ann'],
        'growth': ind.loc[cands, 'inc_net_profit_year_on_year'],
        'ep': 1.0 / val.loc[cands, 'pe_ratio'],
        'bp': 1.0 / val.loc[cands, 'pb_ratio'],
        'vol': vol60[cands],
    })
    lo = df['growth'].quantile(WINSOR_P)
    hi = df['growth'].quantile(1.0 - WINSOR_P)
    df['growth'] = df['growth'].clip(lo, hi)

    score = (df['roe'].rank(pct=True)
             + df['growth'].rank(pct=True)
             + (df['ep'].rank(pct=True) + df['bp'].rank(pct=True)) / 2.0
             + (-df['vol']).rank(pct=True)) / 4.0
    return list(score.sort_values(ascending=False).index)


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
    lines = ["", "==================== ROE 质量轮动 v2（分位数合成） 汇总 ====================",
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
