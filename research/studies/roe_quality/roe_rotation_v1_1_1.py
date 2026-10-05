# -*- coding: utf-8 -*-
# roe_rotation_v1_1_1 —— v1.1 排序消融与聚宽自定义曲线
#
# 选股：保留 v1 的 ROE 年化近似>12%、PE(TTM)<30、PB<5、
#       单季度净利润同比>10%、60 日年化波动率<35%；默认随机排序。
# 硬剔除、TOP_N=10、20 交易日整批轮动、9:31 下单和费率沿用 v1。
# 唯一交易实验变量是 RANK_MODE：random / prev_change；RANDOM_SEED 固定后可复现。
# 随机顺序由种子、T-1日期、代码的 SHA256 生成，与输入行序和 Python hash 无关。
# 同一参数同一日期顺序相同；每轮随日期变化。预先固定种子，不按成绩挑种子。
# 切回 prev_change 恢复 v1.1 排序；两组使用相同区间、资金、频率和平台设置。
# 本版已跑（2026-10-04 回填）：random 排序明显劣于 v1.1 动量排序，
# 未跑赢基准；成绩见文末头注。
# PLOT_DIAGNOSTICS=True：after_trading_end 用 record 画诊断线（不额外请求行情）。
# 候选数/留存率是最近调仓信号值，非调仓日沿用；留存率=交集数/上轮数量×100。
# 首轮或上轮为空时不记录对应留存率，避免把缺少对照伪装成 0%/100%。
# 原涨幅前十留存率始终计算，作为影子名单，不参与 random 模式交易。
# 每日双边换手=当天实际成交买卖金额之和/上一交易日收盘净值×100；不是单边换手。
# 同日回买数只计买卖双方都有实际成交的股票；不把下单当作成交。
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
# 29.40%
# 策略年化收益
# 2.52%
# 超额收益
# 4.69%
# 基准收益
# 23.60%
# 阿尔法
# -0.003
# 贝塔
# 0.627
# 夏普比率
# -0.085
# 胜率
# 0.508
# 盈亏比
# 1.147
# 最大回撤
# 38.30%
# 索提诺比率
# -0.113
# 日均超额收益
# 0.01%
# 超额收益最大回撤
# 38.40%
# 超额收益夏普比率
# -0.241
# 盈利次数
# 637
# 亏损次数
# 616
# 策略波动率
# 0.175
# 基准波动率
# 0.189
# 最大回撤区间
# 2020/08/17,2024/02/05
# （区间 2016-01-04~2026-09-01、100 万，与 v1.1 同台：动量 +85.63%/超额夏普 0.000
#  vs random +29.40%/超额夏普 -0.241）
import hashlib
import math

import pandas as pd

from jqdata import *

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
RANK_MODE = 'random'     # random：去除涨幅排序；prev_change：v1.1 对照
RANDOM_SEED = 0          # 随机对照种子；建议预先固定 0、1、2，分别运行
PLOT_DIAGNOSTICS = True  # 聚宽 record 自定义指标曲线开关


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
    log.info("ROE v1.1.1：排序=%s，种子=%s；财务/估值/日线截止T-1；9:31下单"
             % (RANK_MODE, RANDOM_SEED))
    run_daily(on_open, time='9:31')
    run_daily(on_close, time='14:55')
    run_daily(mark_nav, time='14:56')


def mark_nav(context):
    g.curve.append((context.current_dt, context.portfolio.total_value))


def _rank_candidates(cands, change, asof):
    if RANK_MODE == 'prev_change':
        # 保留 v1.1 的稳定排序及同分时输入顺序。
        return sorted(cands, key=lambda code: -change[code])
    if RANK_MODE != 'random':
        raise ValueError('RANK_MODE must be random or prev_change')
    prefix = '%s|%s|' % (RANDOM_SEED, pd.Timestamp(asof).strftime('%Y-%m-%d'))
    return sorted(cands, key=lambda code: (
        hashlib.sha256((prefix + code).encode('utf-8')).hexdigest(), code))


def _update_signal_diagnostics(ranked):
    pool, top = set(ranked), set(ranked[:TOP_N])
    reference_top = set(g.diag_reference_ranked[:TOP_N])
    g.diag['候选数'] = len(pool)
    for name, current, previous in (
            ('候选留存率_pct', pool, g.diag_prev_pool),
            ('前十留存率_pct', top, g.diag_prev_top),
            ('原涨幅前十留存率_pct', reference_top, g.diag_prev_reference_top)):
        g.diag.pop(name, None)
        if previous:
            g.diag[name] = 100.0 * len(current & previous) / len(previous)
    g.diag_prev_pool = pool
    g.diag_prev_top = top
    g.diag_prev_reference_top = reference_top


def after_trading_end(context):
    """收盘后按实际已成交数量画线；不改变信号、仓位或交易调用。"""
    nav = context.portfolio.total_value
    if PLOT_DIAGNOSTICS:
        buy_amount, sell_amount = 0.0, 0.0
        bought, sold = set(), set()
        for order in get_orders().values():
            if order.filled <= 0:
                continue
            amount = order.filled * order.price
            if order.is_buy:
                buy_amount += amount
                bought.add(order.security)
            else:
                sell_amount += amount
                sold.add(order.security)
        values = dict(g.diag)
        values.update({
            '持仓数': sum(p.total_amount > 0 for p in context.portfolio.positions.values()),
            '仓位_pct': 100.0 * (nav - context.portfolio.cash) / nav if nav > 0 else 0.0,
            '同日回买数': len(bought & sold),
        })
        if g.diag_prev_nav > 0:
            values['每日双边换手_pct'] = 100.0 * (buy_amount + sell_amount) / g.diag_prev_nav
        record(**values)
    g.diag_prev_nav = nav


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
    """14:55 选股：沿用 v1.1 筛选，按 RANK_MODE 排序。"""
    g.diag_reference_ranked = []  # 所有空池提前返回也必须清空影子名单
    dt = context.current_dt
    asof = _prev_trade_day(dt)
    pool = _mainboard_pool(context)

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

    cands = _st_filter(cands, dt)
    if not cands:
        return []

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
    g.diag_reference_ranked = sorted(cands, key=lambda code: -chg[code])
    return _rank_candidates(cands, chg, asof)


def on_close(context):
    """14:55：轮动日用T-1财务/日线生成次交易日名单。"""
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY != 0:
        return
    ranked = build_signal(context)
    _update_signal_diagnostics(ranked)
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
    lines = ["", "==================== ROE 质量轮动 v1.1.1 汇总 ====================",
             "排序=%s  随机种子=%s" % (RANK_MODE, RANDOM_SEED),
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
