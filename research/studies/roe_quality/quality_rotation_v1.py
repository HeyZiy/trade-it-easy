# -*- coding: utf-8 -*-
# quality_rotation_v1 —— quality_slow 慢质量线首版（2026-10-04 grill 定案）
#
# 研究问题：教科书口径的质量（多期+稳定性+行业内中性）在 20 日时钟下
#           有没有截面预测力。时钟=20 交易日，与 roe_quality v1(+86%)/v2(-19%)
#           同台对照，因子体系是主要变量。
#
# 定案规格（裁决记录见项目记忆，标"默"=用户未点名即采纳推荐）：
#   池子   科创/创业/北交所全放开，无市值门槛；ST/停牌/上市不足 61 日
#          仍剔（成交可行性），解禁窗沿用 v1_1（保持池定义与 v1 家族可比）。
#   质量腿 最近 8 个已披露报告期（pubDate<=T-1）单季 roe 的均值 + 标准差反向；
#          至少 6 期否则整只剔除；不剔 Q4。
#   成长腿 最新已披露报告期 营收同比 + 净利同比（至少一项有限值）。
#   价值腿 EP=1/pe_ratio(仅>0) 与 BP=1/pb_ratio(仅>0) 分位均值，缺项平均。
#   低波腿 vol60 反向分位（62 根日线、60 个收益样本，截止 T-1）。
#   红利腿 删除（平台无源：get_dividend NameError、dividend_ratio 平台侧无）。
#   动量   60 日动量只记录和诊断，不进合成。
#   中性化 申万一级行业内分位 -> 跨行业腿内平均 -> 全市场合成排名；
#          不加行业限额，日志输出入选行业分布供事后诊断。
#   持仓   TOP20 等权（默）；基准中证500（默）；区间 2016-01-04~2026-09-01、
#          100 万（默）；执行层沿用 v1_1：14:55 信号、9:31 下单、涨停跳过、
#          停牌顺延、整批 20 日、费率同（默）。
#   裁决   超额收益夏普≈0 → 收线（复用 trend_bt/roe_quality 既有裁决，
#          不自创分档）；"可用"四条件（IR≥0.65、净年化超额≥8%、回撤≤30%、
#          PF≥1.5）是后续轮次目标，不是本轮门槛。
#
# 与 v1/v2 对照的已知偏差（如实声明）：Q2 放开板块使本版池子比 v1/v2 宽，
#   "同台面"只保证同时钟/同执行层/同区间，不是严格单变量。
# 实现约束：avoid_future_data=True 下引擎禁用 statDate 查询（v1 头注实锤），
#   多期 roe 用 9 个回看日的 get_fundamentals(date=过去T-1k) 逐段取"该日已
#   披露最新一期"，按 statDate 去重拼出近 8 期；每只股票可得期数取决于
#   披露早晚，晚披露者靠 66 交易日步长缓冲，仍不足 6 期者被剔除。
#   历史财务修订覆盖仍未审计（与 v1_1 同限制）。
# 运行预估：每轮 9 次全市场财务查询 + 5000 只 62 日 history + 行业映射，
#   约 v1_1 的 5~10 倍耗时；10 年窗预计 30~60 分钟，若平台超时请报回耗时，
#   不要自行砍查询步数。
#
# ==================== 结果（聚宽回填 2026-10-04） ====================
# 区间 2016-01-04~2026-09-01 | 基准 中证500（基准收益仅 3.16%）| 100 万
# 策略收益 33.64% | 年化 2.84% | 超额收益 29.54%
# 阿尔法 0.014 | 贝塔 0.693 | 夏普比率 -0.061
# 胜率 0.484 | 盈亏比 1.146 | 索提诺 -0.078
# 最大回撤 40.22%（2017-04-11~2018-10-18）
# 超额收益最大回撤 41.78% | 超额收益夏普比率 -0.114 | 信息比率 0.197
# 日胜率 0.500 | 盈利次数 1238 | 亏损次数 1318 | 日均超额 0.01%
#
# 判读（裁决线=超额夏普≈0，预注册于头注）：
#   超额夏普 -0.114 < 阈值 → 收线，不做变体。
#   信息比率 0.197（10年 t≈0.62）= 噪声；年化超额 ~2.6% 且基准弱到
#   十年仅 +3.16%，仍跑不出正超额曲线；盈亏比 1.146 与 v1 家族同形态。
#   本推论："教科书口径质量（多期+稳定性+行业内中性、无动量腿）在
#   20 日轮动下无截面预测力"——质量口径修到标准姿势后仍救不了 20 日时钟。
#   未回收旁证：日志诊断行（入选动量分位均值/行业分布）未贴回，不影响裁决。
import math

import pandas as pd

from jqdata import *

TOP_N = 20
HOLD_DAYS = 20
ROTATE_EVERY = 20
ROE_MIN_PERIODS = 6      # 少于 6 期直接剔除（定案）
ROE_PERIODS_KEEP = 8     # 取最近 8 期算均值/标准差
ROE_QUERY_STEP = 66      # 回看步长（交易日），≈一个季度+披露缓冲
ROE_LOOK_STEPS = 8       # 除最新一期外再回看 8 段，覆盖晚披露
VOL_BARS = 62            # 62 根日线 -> 60 个收益样本 + 60 日动量两端
LIMIT_UP_PAD = 0.001
FEE_COMMISSION = 0.00025
FEE_TAX = 0.001
BAN_WINDOW = 90


def initialize(context):
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark("000905.XSHG")
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    set_order_cost(OrderCost(open_commission=FEE_COMMISSION,
                             close_commission=FEE_COMMISSION,
                             open_tax=0, close_tax=FEE_TAX,
                             min_commission=0), type='stock')
    g.day = 0
    g.pending = None
    g.batch_sizes = []
    g.hold_since = {}
    g.curve = []
    log.info("quality v1：多期ROE+稳定性/成长/价值/低波，行业内分位等权合成，"
             "TOP%d 等权，%d 交易日整批；财务/估值/日线截止 T-1，9:31 下单"
             % (TOP_N, ROTATE_EVERY))
    run_daily(on_open, time='9:31')
    run_daily(on_close, time='14:55')
    run_daily(mark_nav, time='14:56')


def mark_nav(context):
    g.curve.append((context.current_dt, context.portfolio.total_value))


def _report_date(value):
    """解析真实季度末日期；兼容 YYYYqN，缺失或非法值不猜测。（沿用 v1_1）"""
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


def _prev_trade_day(dt):
    return get_trade_days(end_date=dt.date(), count=2)[0]


def _valid_rows(rows, asof, fields):
    """按 pubDate<=asof 校验披露可见性，返回 code 索引的有限数值帧。"""
    if rows is None or len(rows) == 0:
        return None
    ind = rows.copy().set_index('code')
    cutoff = pd.Timestamp(asof).normalize()
    sd = ind['statDate'].apply(_report_date)
    pub = pd.to_datetime(ind['pubDate'], errors='coerce')
    ok = sd.notna() & pub.notna() & (sd <= cutoff) & (pub <= cutoff)
    ind = ind.loc[ok]
    if len(ind) == 0:
        return None
    for f in fields:
        ind[f] = pd.to_numeric(ind[f], errors='coerce')
    return ind


def collect_roe_periods(context, asof):
    """9 段回看拼单季 roe 序列：code -> {statDate: roe}。"""
    hist = {}
    q_fields = ['roe']

    def feed(rows, cutoff):
        if rows is None:
            return
        ind = _valid_rows(rows, cutoff, q_fields)
        if ind is None:
            return
        for code, sd, roe in zip(ind.index,
                                 ind['statDate'].apply(_report_date),
                                 ind['roe']):
            if roe is None or not math.isfinite(roe):
                continue
            hist.setdefault(code, {})[sd] = roe

    rows = get_fundamentals(
        query(indicator.code, indicator.roe, indicator.statDate,
              indicator.pubDate), date=asof)
    feed(rows, asof)
    for k in range(1, ROE_LOOK_STEPS + 1):
        past = get_trade_days(end_date=asof,
                              count=k * ROE_QUERY_STEP + 1)[0]
        rows = get_fundamentals(
            query(indicator.code, indicator.roe, indicator.statDate,
                  indicator.pubDate), date=past)
        feed(rows, past)
    return hist


def _quality_stats(hist):
    """code -> (mean, std)：最近 8 期，至少 6 期，否则不入选。"""
    stats = {}
    for code, d in hist.items():
        vals = [d[s] for s in sorted(d.keys(), reverse=True)
                [:ROE_PERIODS_KEEP]]
        n = len(vals)
        if n < ROE_MIN_PERIODS:
            continue
        mean = sum(vals) / n
        var = sum([(v - mean) ** 2 for v in vals]) / n
        stats[code] = (mean, math.sqrt(var))
    return stats


def build_signal(context):
    dt = context.current_dt
    asof = _prev_trade_day(dt)

    # 全板块股票池（时点），只做成交可行性剔除，不做板块/市值偏好
    secs = get_all_securities(['stock'], date=dt)
    pool = list(secs.index)

    # 质量腿（多期）
    hist = collect_roe_periods(context, asof)
    qstats = _quality_stats(hist)
    cands = [c for c in pool if c in qstats]
    if not cands:
        return []

    # 最新一期成长 + 最近质量所需的可见性已由 pubDate 校验保证
    latest = _valid_rows(
        get_fundamentals(
            query(indicator.code, indicator.roe, indicator.statDate,
                  indicator.pubDate, indicator.inc_revenue_year_on_year,
                  indicator.inc_net_profit_year_on_year), date=asof),
        asof, ['roe', 'inc_revenue_year_on_year',
               'inc_net_profit_year_on_year'])
    if latest is None:
        return []
    latest = latest[['inc_revenue_year_on_year',
                     'inc_net_profit_year_on_year']]
    latest = latest.groupby(latest.index).first()
    cands = [c for c in cands if c in latest.index]
    if not cands:
        return []

    # 价值腿：EP/BP 仅正值入样
    val = get_fundamentals(query(valuation), date=asof).set_index('code')
    val = val.loc[[c for c in cands if c in val.index]]
    if len(val) == 0:
        return []
    ep = pd.to_numeric(val['pe_ratio'], errors='coerce')
    ep = ep.where(ep > 0)
    bp = pd.to_numeric(val['pb_ratio'], errors='coerce')
    bp = bp.where(bp > 0)
    F = pd.DataFrame({
        'q_mean': [qstats[c][0] for c in val.index],
        'q_std': [-qstats[c][1] for c in val.index],
        'g_rev': [pd.to_numeric(latest['inc_revenue_year_on_year'].get(c),
                                errors='coerce') for c in val.index],
        'g_np': [pd.to_numeric(latest['inc_net_profit_year_on_year'].get(c),
                               errors='coerce') for c in val.index],
        'v_ep': (1.0 / ep).tolist(),
        'v_bp': (1.0 / bp).tolist(),
    }, index=val.index)
    # 成长腿至少要有一项（质量腿已由 qstats 保证）
    F = F[F['g_rev'].notna() | F['g_np'].notna()]
    cands = list(F.index)
    if not cands:
        return []

    # 成交可行性硬剔除：ST → 停牌 → 日线完整性(兼次新) → 解禁
    try:
        st = get_extras('is_st', security_list=cands, end_date=dt, count=1)
        cands = [c for c in cands if not bool(st[c].iloc[-1])]
    except Exception:
        pass
    if not cands:
        return []
    cd = get_current_data()
    cands = [c for c in cands if not cd[c].paused]

    closes = history(VOL_BARS, '1d', 'close', security_list=cands)
    cands = [c for c in cands if closes[c].notna().sum() >= VOL_BARS]
    if not cands:
        return []
    F = F.loc[cands]
    rets = closes[cands].pct_change()
    vol60 = rets.tail(60).std() * math.sqrt(250) * 100
    mom60 = {c: closes[c].iloc[-1] / closes[c].iloc[-VOL_BARS] - 1
             for c in cands}
    F['l_vol'] = -vol60

    try:
        rows = get_locked_shares(stock_list=cands, start_date=dt,
                                 end_date=dt + pd.Timedelta(days=BAN_WINDOW))
        banned = set() if rows is None else set(rows['code'])
        cands = [c for c in cands if c not in banned]
    except Exception:
        pass
    if not cands:
        return []
    F = F.loc[cands]

    # 申万一级行业内分位 → 腿内平均 → 四腿合成（缺项平均）
    info = get_industry(cands, date=asof)
    ind = pd.Series({c: (info.get(c) or {}).get('sw_l1', {})
                     .get('industry_code', 'NA') for c in cands})
    # 平台旧版 pandas 的 rank(pct=True) 在组内整列全 NaN 时除零（v1.1 平台实锤）；
    # 等价改写：平均秩÷组内非NaN计数，空组记 NaN。
    G = F.groupby(ind)
    cnt = G.transform('count')
    R = G.rank() / cnt.mask(cnt == 0)
    legs = pd.DataFrame({
        'quality': R[['q_mean', 'q_std']].mean(axis=1),
        'growth': R[['g_rev', 'g_np']].mean(axis=1),
        'value': R[['v_ep', 'v_bp']].mean(axis=1),
        'lowvol': R['l_vol'],
    })
    legs = legs[legs['quality'].notna() & legs['growth'].notna()
                & legs['lowvol'].notna()]
    total = legs.mean(axis=1)
    if len(total) < 3:
        return []

    ranked = total.sort_values(ascending=False)
    top = ranked.head(TOP_N)

    # 诊断：60 日动量分位（全局，仅记录）、入选行业分布
    mom_rank = pd.Series(mom60).rank(pct=True)
    mom_mean = sum([float(mom_rank.get(c, float('nan')))
                    for c in top.index]) / len(top)
    dist = {}
    for c in top.index:
        key = ind.get(c, 'NA')
        dist[key] = dist.get(key, 0) + 1
    dist_top = sorted(dist.items(), key=lambda kv: -kv[1])[:5]
    log.info("[quality v1] %s 候选%d 入选%d 腿不足%d 动量分位均值%.2f "
             "行业top=%s" % (dt.strftime('%F'), len(ranked), len(top),
                             len(total) - len(ranked.head(TOP_N)),
                             mom_mean, dist_top))
    return list(top.index)


def on_close(context):
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY != 0:
        return
    ranked = build_signal(context)
    g.batch_sizes.append(len(ranked))
    g.pending = (context.current_dt, ranked)


def on_open(context):
    """9:31：先卖到期持仓，再按前一交易日 14:55 名单等权买入。（沿用 v1_1）"""
    dt = context.current_dt
    cd = get_current_data()

    for code, pos in list(context.portfolio.positions.items()):
        if pos.total_amount <= 0:
            continue
        since = g.hold_since.get(code)
        if since is None:
            continue
        held = len(get_trade_days(start_date=since, end_date=dt)) - 1
        if held >= HOLD_DAYS:
            order_target(code, 0)

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

    last = {}
    for d, v in curve:
        last[d.year] = v
    yearly, prev = {}, context.portfolio.starting_cash
    for y in sorted(last):
        yearly[y] = round((last[y] / prev - 1) * 100, 1)
        prev = last[y]

    starts = []
    for ym in ('2019', '2020', '2021', '2022', '2023', '2024'):
        pts = [(d, v) for d, v in curve if d.strftime('%Y') >= ym]
        if pts:
            starts.append("%s:%+.1f%%" % (ym, (pts[-1][1] / pts[0][1] - 1) * 100))

    bs = g.batch_sizes
    lines = ["", "==================== quality_slow v1 汇总 ====================",
             "窗口: %s ~ %s | TOP_N=%d | 轮动 %d 交易日 | 每轮候选数 均%.1f/最小%d/最大%d"
             % (curve[0][0].strftime('%F'), curve[-1][0].strftime('%F'), TOP_N,
                ROTATE_EVERY,
                sum(bs) / max(len(bs), 1), min(bs) if bs else 0,
                max(bs) if bs else 0),
             "总收益 %+7.1f%%  年化 %+5.1f%%  最大回撤 %6.1f%%" % (tot * 100, cagr * 100, mdd * 100),
             "年度收益: %s" % yearly,
             "曲线分段累计（非独立起跑）: %s" % "  ".join(starts),
             "裁决线：超额收益夏普≈0 → 收线（复用既有裁决）",
             "==========================================================="]
    for ln in lines:
        log.info(ln)
