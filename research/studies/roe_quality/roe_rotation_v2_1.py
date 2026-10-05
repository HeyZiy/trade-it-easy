# -*- coding: utf-8 -*-
# roe_rotation_v2_1 —— ROE 质量轮动 · v2 共同口径修订版
#
# 选股：保留 v2 的四类横截面分位数等权合成，质量=ROE 年化近似，
#       成长=单季度净利润同比（1%/99%截尾），估值=EP/BP 分位数均值，
#       低波=vol60 反向分位数；保留 ROE>0、PE>0、PB>0 底线。
# 硬剔除、TOP_N=10、20 交易日整批轮动、9:31 下单和费率沿用 v2。
#
# 共同修正：
#   1) JQData 六家公司五期实值已确认 indicator.roe 是单季度百分数，
#      样本见 roe_field_probe.csv；统一使用单季度 ROE×4 作年化近似，
#      删除 4/n。该近似仍有季节性偏差，不能称为 TTM 或全年真实 ROE。
#   2) statDate 按季度末日期解析，pubDate 按披露日校验；无效/缺失日期
#      和非有限财务值剔除，不按信号日所在季度补猜报告期。
#   3) 14:55 生成名单：基本面和估值都查询 T-1；history('1d') 同样
#      截止 T-1，信号日状态过滤沿用原版。次交易日 9:31 下单，成交价格
#      由平台撮合决定，不能标注为严格开盘成交。
#   4) 汇总的年份切片改称曲线分段累计，不视作独立起跑回测。
#
# 仍需审计：历史财务修订覆盖、未来解禁公告的公开时点及缺失时放行；
# 监管函/减持计划等缺少数据源。净值采样仍在14:56，印花税仍全程0.1%。
# 原有到期清仓后回买继续保留；减少换手另做实验。
# 数据来源：JoinQuant 官方 SDK 字段定义及 get_history_fundamentals 文档。
# https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/api.py
# https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/fundamentals_tables_gen.py
#
# 策略收益
# -18.92%
# 策略年化收益
# -2.00%
# 超额收益
# -34.40%
# 基准收益
# 23.60%
# 阿尔法
# -0.046
# 贝塔
# 0.704
# 夏普比率
# -0.314
# 胜率
# 0.472
# 盈亏比
# 1.001
# 最大回撤 
# 50.34%
# 索提诺比率
# -0.410
# 日均超额收益
# -0.01%
# 超额收益最大回撤
# 59.74%
# 超额收益夏普比率
# -0.537
# 日胜率
# 0.482
# 盈利次数
# 608
# 亏损次数
# 681
# 信息比率
# -0.274
# 策略波动率
# 0.191
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
VOL_LOOKBACK = 61        # 61根日线，形成60个收益样本，截止T-1
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
    log.info("ROE v2.1：单季度ROE(%)×4年化近似；财务/估值/日线截止T-1；9:31下单")
    run_daily(on_open, time='9:31')
    run_daily(on_close, time='14:55')
    run_daily(mark_nav, time='14:56')


def mark_nav(context):
    g.curve.append((context.current_dt, context.portfolio.total_value))


def _report_date(value):
    """解析真实季度末日期；兼容 YYYYqN，缺失或非法值不猜测。"""
    if pd.isna(value):
        return pd.NaT
    text = str(value).strip().lower()
    try:
        if (len(text) == 6 and text[:4].isdigit()
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
    ind = ind.replace([float('inf'), float('-inf')], float('nan'))
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
    """14:55 选股：信号日状态剔除，T-1 财务/日线四因子分位数等权排序。"""
    dt = context.current_dt
    asof = _prev_trade_day(dt)
    pool = _mainboard_pool(context)

    pool = _st_filter(pool, dt)
    if not pool:
        return []

    # 基本面（全市场一次取，再收敛到主板池）：ROE>0 底线 + 成长非缺失
    ind = get_fundamentals(
        query(indicator.code, indicator.roe,
              indicator.inc_net_profit_year_on_year, indicator.statDate,
              indicator.pubDate), date=asof)
    ind = _prepare_fundamentals(ind, asof)
    ind = ind[ind.index.isin(pool)]
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
        .filter(valuation.code.in_(cands)), date=asof)
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
    """14:55：轮动日用T-1财务/日线生成次交易日名单。"""
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY != 0:
        return
    ranked = build_signal(context)
    g.batch_sizes.append(len(ranked))
    g.pending = (context.current_dt, ranked)


def on_open(context):
    """9:31：先卖到期持仓，再按前一交易日14:55生成的名单买入。"""
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

    # 原曲线分段累计：继承当时持仓，并非重新起跑的独立回测
    starts = []
    for ym in ('2019', '2020', '2021', '2022', '2023', '2024'):
        pts = [(d, v) for d, v in curve if d.strftime('%Y') >= ym]
        if pts:
            starts.append("%s:%+.1f%%" % (ym, (pts[-1][1] / pts[0][1] - 1) * 100))

    bs = g.batch_sizes
    lines = ["", "==================== ROE 质量轮动 v2.1（分位数合成） 汇总 ====================",
             "窗口: %s ~ %s | TOP_N=%d | 持有 %d 交易日 | 每轮候选数 均%.1f/最小%d/最大%d"
             % (curve[0][0].strftime('%F'), curve[-1][0].strftime('%F'), TOP_N,
                HOLD_DAYS,
                sum(bs) / max(len(bs), 1), min(bs) if bs else 0, max(bs) if bs else 0),
             "总收益 %+7.1f%%  年化 %+5.1f%%  最大回撤 %6.1f%%" % (tot * 100, cagr * 100, mdd * 100),
             "年度收益: %s" % yearly,
             "曲线分段累计（非独立起跑）: %s" % "  ".join(starts),
             "==========================================================="]
    for ln in lines:
        log.info(ln)
