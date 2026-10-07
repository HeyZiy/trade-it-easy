# -*- coding: utf-8 -*-
# quality_rotation_v1_1 —— 质量腿替换对照：华证新质量四因子（mean/std 版）替换 8期ROE均值+稳定性
#
# 问题：同钟同池同执行下，把质量腿从"最近8期单季ROE均值+稳定性"换成华证新质量
#           四因子，20日时钟下的结果是否改变？（报告：《指数问道系列之新风格体系篇（二）：
#           华证新质量因子的构建与应用》2025-03-13，docs/ 有全文 PDF）
# 对照：quality_rotation_v1.py，唯一变量是质量腿内部定义；成长/价值/低波三条腿、池子、
#       中性化方式、TOP20、20日整批、执行层、费率、区间、资金、基准全部沿用 v1。
# v1 对照成绩：策略+33.64% | 超额+29.54% | 超额夏普-0.114（基准中证500，十年仅+3.16%）。
#
# 实验（四因子规格，全部 均值÷标准差，取最近8个有效单季，至少6期，总体标准差口径）：
#   销售净利率   = 单季净利润 / 单季营业收入（income 累计报表差分）
#   GPOA         = 单季毛利(营收-营业成本) / 总资产（balance 按季匹配，缺则取之前最近一期）
#   营收份额     = 单季营收 / 同申万一级行业单季营收合计（行业归属取信号日，全市场聚合）
#   利润现金比率 = 单季经营现金流净额 / 单季净利润，仅净利润>0 的期计入（cash_flow 表差分）
#   任一因子有效期数不足6、或序列常数（std≈0）→ 整只剔除。
# 合成：四因子在申万一级行业内分位后等权平均 = quality 腿（沿用本目录中性化规格；
#   华证报告用非中性化版本——这是有意的单变量选择而非遗漏，作为已知差异声明）。
#
# 失误率诊断（偷自华证报告，口径改池内）：上一轮质量分前1/3 ∩ 本轮候选池单季ROE后1/3，
#   分母=上一轮前1/3中本轮仍在候选池的只数；本轮缺上轮对照或 ROE 样本<3 时跳过。
#   华证口径为月频全市场，与本口径不可直接对表，只作方向参考。
#
# 已知偏差（如实声明）：营收份额与中性化的行业归属用信号日时点，历史行业重分类带
#   轻微前视（仅分类信息，非财务数据）；累计报表差分依赖连续两季同年在场，缺季弃该期；
#   历史财务修订覆盖仍未审计（沿用 v1_1 同限制）。华证报告本身是月频/华证全指/
#   市值加权风格组合，本实验只取其因子定义，不是复刻其组合，成绩不可与之对表。
#
# 实现约束：avoid_future_data 下禁用 statDate 查询，沿用 9 段回看（66 交易日步长）；
#   income/cash_flow 为累计报表，Q1 单季=累计值，Q2~4 需上一季同年在场做差分；
#   同一 statDate 多段重复出现时后段覆盖（即保留最新修订）。
# 运行预估：每轮 27 次全市场财务查询（income/cash_flow/balance × 9 段）+ 估值/指标/行情，
#   约 v1 的 3 倍耗时，全区间预计 1.5~3 小时。建议先缩短区间（如 2016-01-04~2016-12-31）
#   验证字段与耗时再跑全程，缩短区间不改口径。字段报错请把日志原文贴回，不要自行换字段。
#
# ==================== 结果（聚宽 2026-10-04 中止） ====================
# 跑至 2021-03-10 因平台旧版 pandas groupby.rank(pct=True) 组内整列全 NaN
# 除零崩溃中止（build_signal 已改为 秩÷组内非NaN计数，语义等价，未重跑）。
# 中止时点（2016-01~2021-03，约窗口一半）累计超额≈0，曲线读数非精确统计。
# 判定为中途无效性中止，非全窗口裁决执行：过半窗口零超额，全窗口过线需
# 后段异常强贡献，不予修复重跑。
# 裁决：质量定义分支关闭，quality_slow 维持收线（同 v1）。
import math

import pandas as pd

from jqdata import *

TOP_N = 20
HOLD_DAYS = 20
ROTATE_EVERY = 20
FIN_MIN_PERIODS = 6      # 每因子至少 6 个有效单季值（定案，沿用 v1 纪律）
FIN_KEEP_PERIODS = 8     # 取最近 8 个有效单季
FIN_QUERY_STEP = 66      # 回看步长（交易日），≈一个季度+披露缓冲
FIN_LOOK_STEPS = 8       # 除最新一段外再回看 8 段，覆盖晚披露
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
    g.prev_top = None       # 上一轮质量分前1/3（失误率用）
    g.miss_hist = []
    log.info("quality v1.1：华证四因子质量腿（mean/std）；成长/价值/低波/池/执行同 v1；"
             "TOP%d 等权，%d 交易日整批；财务/估值/日线截止 T-1，9:31 下单"
             % (TOP_N, ROTATE_EVERY))
    run_daily(on_open, time='9:31')
    run_daily(on_close, time='14:55')
    run_daily(mark_nav, time='14:56')


def mark_nav(context):
    g.curve.append((context.current_dt, context.portfolio.total_value))


def _report_date(value):
    """解析真实季度末日期；兼容 YYYYqN，缺失或非法值不猜测。（沿用 v1）"""
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
    """按 pubDate<=asof 校验披露可见性，返回 code 索引的有限数值帧。（沿用 v1）"""
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


def _single_quarters(cum):
    """累计报表值 -> 单季值：Q1=累计；Q2~4 需上一季同年累计在场，缺则弃该期。"""
    out = {}
    for sd, v in cum.items():
        if not math.isfinite(v):
            continue
        if sd.quarter == 1:
            out[sd] = v
        else:
            prev = cum.get(sd - 1)
            if prev is not None and math.isfinite(prev):
                out[sd] = v - prev
    return out


def _stable_ratio(series):
    """{Period: 值} -> 最近 FIN_KEEP_PERIODS 期的 均值÷标准差；不足期数或 std≈0 -> None。"""
    vals = [v for _, v in sorted(series.items())[-FIN_KEEP_PERIODS:]]
    n = len(vals)
    if n < FIN_MIN_PERIODS:
        return None
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    std = math.sqrt(var)
    if std <= 1e-12:
        return None
    return mean / std


def _assets_at(bal_by_q, q):
    """该季及之前最近一期的总资产；无则 None。"""
    best = None
    for sd, v in bal_by_q.items():
        if sd <= q and math.isfinite(v) and v > 0:
            if best is None or sd > best:
                best = sd
    return None if best is None else bal_by_q[best]


def collect_fin_periods(context, asof):
    """9 段回看拼累计报表：返回 (inc, ocf, bal)。
    inc[code] = {Period: (rev_cum, cost_cum, np_cum)}
    ocf[code] = {Period: ocf_cum}
    bal[code] = {Period: total_assets}"""
    inc, ocf, bal = {}, {}, {}
    for k in range(FIN_LOOK_STEPS + 1):
        past = get_trade_days(end_date=asof, count=k * FIN_QUERY_STEP + 1)[0]
        rows = _valid_rows(get_fundamentals(
            query(income.code, income.statDate, income.pubDate,
                  income.operating_revenue, income.operating_cost,
                  income.net_profit), date=past),
            past, ['operating_revenue', 'operating_cost', 'net_profit'])
        if rows is not None:
            for code, sd, rev, cost, np_ in zip(
                    rows.index, rows['statDate'].apply(_report_date),
                    rows['operating_revenue'], rows['operating_cost'],
                    rows['net_profit']):
                if pd.isna(sd) or not (math.isfinite(rev) and math.isfinite(cost)
                                       and math.isfinite(np_)):
                    continue
                inc.setdefault(code, {})[pd.Period(sd, freq='Q')] = (rev, cost, np_)
        rows = _valid_rows(get_fundamentals(
            query(cash_flow.code, cash_flow.statDate, cash_flow.pubDate,
                  cash_flow.net_operate_cash_flow), date=past),
            past, ['net_operate_cash_flow'])
        if rows is not None:
            for code, sd, o in zip(rows.index, rows['statDate'].apply(_report_date),
                                   rows['net_operate_cash_flow']):
                if pd.isna(sd) or not math.isfinite(o):
                    continue
                ocf.setdefault(code, {})[pd.Period(sd, freq='Q')] = o
        rows = _valid_rows(get_fundamentals(
            query(balance.code, balance.statDate, balance.pubDate,
                  balance.total_assets), date=past),
            past, ['total_assets'])
        if rows is not None:
            for code, sd, ta in zip(rows.index, rows['statDate'].apply(_report_date),
                                    rows['total_assets']):
                if pd.isna(sd) or not math.isfinite(ta) or ta <= 0:
                    continue
                bal.setdefault(code, {})[pd.Period(sd, freq='Q')] = ta
    return inc, ocf, bal


def _quality_scores(asof, inc, ocf, bal):
    """华证四因子 mean/std：返回 ({code: (f_margin, f_gpoa, f_share, f_cash)},
    {code: 行业代码})。行业归属取信号日 asof，营收合计按全市场聚合。"""
    sq = {}
    for code, d in inc.items():
        revs = {q: t[0] for q, t in d.items()}
        cost_sq = _single_quarters({q: t[1] for q, t in d.items()})
        np_sq = _single_quarters({q: t[2] for q, t in d.items()})
        rev_sq = _single_quarters(revs)
        if not rev_sq:
            continue
        gp_sq = {q: rev_sq[q] - cost_sq[q] for q in rev_sq if q in cost_sq}
        sq[code] = {'rev': rev_sq, 'gp': gp_sq, 'np': np_sq,
                    'ocf': _single_quarters(ocf.get(code, {}))}
    if not sq:
        return {}, {}
    codes = list(sq.keys())
    info = get_industry(codes, date=asof)
    ind_of = {c: (info.get(c) or {}).get('sw_l1', {}).get('industry_code', 'NA')
              for c in codes}
    ind_rev = {}
    for code, d in sq.items():
        ind = ind_of[code]
        for q, v in d['rev'].items():
            key = (ind, q)
            ind_rev[key] = ind_rev.get(key, 0.0) + v
    scores = {}
    for code, d in sq.items():
        ind = ind_of[code]
        ta = bal.get(code, {})
        margin, gpoa, share, cash = {}, {}, {}, {}
        for q, rev in d['rev'].items():
            npq = d['np'].get(q)
            if rev > 0 and npq is not None and math.isfinite(npq):
                margin[q] = npq / rev
            gp = d['gp'].get(q)
            if gp is not None:
                a = _assets_at(ta, q)
                if a is not None:
                    gpoa[q] = gp / a
            total = ind_rev.get((ind, q))
            if total is not None and total > 0:
                share[q] = rev / total
            ocfq = d['ocf'].get(q)
            if npq is not None and npq > 0 and ocfq is not None:
                cash[q] = ocfq / npq
        f1 = _stable_ratio(margin)
        f2 = _stable_ratio(gpoa)
        f3 = _stable_ratio(share)
        f4 = _stable_ratio(cash)
        if None in (f1, f2, f3, f4):
            continue
        scores[code] = (f1, f2, f3, f4)
    return scores, ind_of


def _miss_rate(roe_series, quality, cands):
    """失误率：上一轮质量分前1/3 ∩ 本轮候选池单季ROE后1/3；
    分母=上一轮前1/3中本轮仍在候选池的只数。返回比率或 None。"""
    miss = None
    q = quality.dropna()
    roe = roe_series.dropna()
    roe = roe.loc[roe.index.isin(cands)]
    if len(q) >= 3 and len(roe) >= 3:
        if g.prev_top is not None:
            kt = max(1, int(round(len(roe) / 3.0)))
            bottom = set(roe.sort_values(ascending=True).head(kt).index)
            still = g.prev_top & set(roe.index)
            if still:
                miss = len(bottom & still) / len(still)
        kq = max(1, int(round(len(q) / 3.0)))
        g.prev_top = set(q.sort_values(ascending=False).head(kq).index)
    else:
        g.prev_top = None
    if miss is not None:
        g.miss_hist.append(miss)
    return miss


def build_signal(context):
    dt = context.current_dt
    asof = _prev_trade_day(dt)

    # 全板块股票池（时点），只做成交可行性剔除（同 v1）
    secs = get_all_securities(['stock'], date=dt)
    pool = list(secs.index)

    # 华证四因子质量腿
    inc, ocf, bal = collect_fin_periods(context, asof)
    qscores, ind_of = _quality_scores(asof, inc, ocf, bal)
    cands = [c for c in pool if c in qscores]
    if not cands:
        return []

    # 最新一期成长 + 失误率用 ROE（同一查询，不额外加请求）
    latest = _valid_rows(
        get_fundamentals(
            query(indicator.code, indicator.roe, indicator.statDate,
                  indicator.pubDate, indicator.inc_revenue_year_on_year,
                  indicator.inc_net_profit_year_on_year), date=asof),
        asof, ['roe', 'inc_revenue_year_on_year',
               'inc_net_profit_year_on_year'])
    if latest is None:
        return []
    latest = latest.groupby(latest.index).first()
    cands = [c for c in cands if c in latest.index]
    if not cands:
        return []

    # 价值腿：EP/BP 仅正值入样（同 v1）
    val = get_fundamentals(query(valuation), date=asof).set_index('code')
    val = val.loc[[c for c in cands if c in val.index]]
    if len(val) == 0:
        return []
    ep = pd.to_numeric(val['pe_ratio'], errors='coerce')
    ep = ep.where(ep > 0)
    bp = pd.to_numeric(val['pb_ratio'], errors='coerce')
    bp = bp.where(bp > 0)
    F = pd.DataFrame({
        'f_margin': [qscores[c][0] for c in val.index],
        'f_gpoa': [qscores[c][1] for c in val.index],
        'f_share': [qscores[c][2] for c in val.index],
        'f_cash': [qscores[c][3] for c in val.index],
        'g_rev': [pd.to_numeric(latest['inc_revenue_year_on_year'].get(c),
                                errors='coerce') for c in val.index],
        'g_np': [pd.to_numeric(latest['inc_net_profit_year_on_year'].get(c),
                               errors='coerce') for c in val.index],
        'v_ep': (1.0 / ep).tolist(),
        'v_bp': (1.0 / bp).tolist(),
    }, index=val.index)
    # 成长腿至少要有一项（质量腿已由 qscores 保证）
    F = F[F['g_rev'].notna() | F['g_np'].notna()]
    cands = list(F.index)
    if not cands:
        return []

    # 成交可行性硬剔除：ST → 停牌 → 日线完整性(兼次新) → 解禁（同 v1）
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

    # 申万一级行业内分位 → 腿内平均 → 四腿合成（行业归属复用聚合时的信号日快照）
    ind = pd.Series({c: ind_of.get(c, 'NA') for c in cands})
    # 平台旧版 pandas 的 rank(pct=True) 在组内整列全 NaN 时除零（2026-10-04 本版平台实锤）；
    # 等价改写：平均秩÷组内非NaN计数，空组记 NaN。
    G = F.groupby(ind)
    cnt = G.transform('count')
    R = G.rank() / cnt.mask(cnt == 0)
    legs = pd.DataFrame({
        'quality': R[['f_margin', 'f_gpoa', 'f_share', 'f_cash']].mean(axis=1),
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

    # 诊断：失误率 + 60 日动量分位（仅记录）+ 行业分布
    miss = _miss_rate(latest['roe'], legs['quality'], cands)
    mom_rank = pd.Series(mom60).rank(pct=True)
    mom_mean = sum([float(mom_rank.get(c, float('nan')))
                    for c in top.index]) / len(top)
    dist = {}
    for c in top.index:
        key = ind.get(c, 'NA')
        dist[key] = dist.get(key, 0) + 1
    dist_top = sorted(dist.items(), key=lambda kv: -kv[1])[:5]
    log.info("[quality v1.1] %s 候选%d 入选%d 腿不足%d 动量分位均值%.2f "
             "失误率%s 行业top=%s"
             % (dt.strftime('%F'), len(ranked), len(top),
                len(total) - len(ranked.head(TOP_N)), mom_mean,
                ('%.3f' % miss) if miss is not None else 'NA', dist_top))
    return list(top.index)


def on_close(context):
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY != 0:
        return
    ranked = build_signal(context)
    g.batch_sizes.append(len(ranked))
    g.pending = (context.current_dt, ranked)


def on_open(context):
    """9:31：先卖到期持仓，再按前一交易日 14:55 名单等权买入。（沿用 v1）"""
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
    if g.miss_hist:
        miss_line = "失误率均值 %.3f（池内口径，n=%d）" % (
            sum(g.miss_hist) / len(g.miss_hist), len(g.miss_hist))
    else:
        miss_line = "失误率：无有效样本"
    lines = ["", "==================== quality_slow v1.1 汇总 ====================",
             "窗口: %s ~ %s | TOP_N=%d | 轮动 %d 交易日 | 每轮候选数 均%.1f/最小%d/最大%d"
             % (curve[0][0].strftime('%F'), curve[-1][0].strftime('%F'), TOP_N,
                ROTATE_EVERY,
                sum(bs) / max(len(bs), 1), min(bs) if bs else 0,
                max(bs) if bs else 0),
             "总收益 %+7.1f%%  年化 %+5.1f%%  最大回撤 %6.1f%%" % (tot * 100, cagr * 100, mdd * 100),
             "年度收益: %s" % yearly,
             "曲线分段累计（非独立起跑）: %s" % "  ".join(starts),
             miss_line,
             "==========================================================="]
    for ln in lines:
        log.info(ln)
