# -*- coding: utf-8 -*-
# pool_ic_probe_v1 —— v1_1 质量池的区分力探针（信号层，不下单）
#
# 问题（一次跑答两个）：
#   Q1 池内区分力：v1_1 质量池内，T-1 一日涨幅排序对未来 20 日收益
#      有没有预测力？（每轮 Spearman rank-IC，汇总均值 + t 值）
#   Q2 准入价值：池内股票的未来 20 日收益是否显著高于池外？
#      （池外对照 = 同主板池、同 ST/停牌/解禁硬剔除、随机抽 300 只；
#       即 v1_6 想在策略层答但被"涨停并列→代码抽签"伪影毁掉的问题）
#
# 实验：
#   - 每 20 交易日一轮（与 v1_1 同时钟），14:55 用 T-1 数据采样；
#     21 个交易日后的 14:55 回看已实现收益 close(S+20)/close(S)-1，
#     近似 v1_1 的"S+1 买入、持有 20 日"窗口（不含涨停跳过与开盘撮合）。
#   - in 组构建完全照抄 v1_1：indicator(ROE×4>12% & 净利同比>10%) →
#     valuation(PE<30 & PB<5) → 61 根日线完整 → vol60<35%；
#     ST/停牌/解禁剔除对 in、out 两组同样施加（准入价值的公平对照）。
#   - 头部检查（防 v1_6 式伪影）：每轮记录 in 组规模与其中
#     T-1 涨幅≥9.8%（涨停区）的股票数；若涨停数≥10，池内排序头部
#     同样退化为抽签，Q1 结论需降级。
#   - 随机抽样以采样日日期为种子，可复现。
#
# 运行：聚宽策略回测粘贴，区间 2016-01-04 ~ 2026-09-01，100 万（不下单）。
#
# 结果（2026-10-04 聚宽回填，区间 2016-01-04~2026-09-01）
#   有效轮次 128；头部检查：in 组均 121 只，涨停区均 0.2 / 最大 5
#     → v1_1 排序头部不退化，v1_6 式抽签是无池专属问题。
#   Q1 池内 rank-IC：均值 +0.0029，t=+0.16，IC>0 占比 55% → |t|<2 无区分力
#     （与 roe_factor_study_jq 的 t=0.35~0.90 交叉印证）。
#   Q2 准入价值：fwd_in 均 +0.89% / fwd_out 均 +0.68% / 差 +0.20%，t=+0.61
#     → |t|<2 池为装饰。
# 结论：Q1 无区分力、Q2 为装饰（都 |t|<2）——"池内动量有区分力"与"池准入有
#   正贡献"两个动机**都证伪**；"池+趋势门"支线据此判死归档。
#   遗留谜题：策略层动量(+85.6%) ≫ random(+29.4%) 在信号层无对应截面梯度，
#   候选解释收窄为 (a) 仅头部凸性、(b) 高波动集中+复利路径；本探针数据
#   未保留个股级前瞻收益，验 (a) 需给 resolve 增加"每轮 in 组前 10 均值
#   vs in 组均值"差值序列并出 t——下轮再验，不追则封存。
import math
import random

import pandas as pd

from jqdata import *

ROTATE_EVERY = 20        # 采样间隔（交易日），与 v1_1 同时钟
HORIZON = 20             # 前瞻收益窗口（交易日）
OUT_SAMPLE = 300         # 池外对照组每轮抽样数
ROE_MIN = 12.0           # 以下五个阈值照抄 v1_1
PE_MAX = 30.0
PB_MAX = 5.0
NP_YOY_MIN = 10.0
VOL60_MAX = 35.0
VOL_LOOKBACK = 61
BAN_WINDOW = 90
LIMIT_UP_CHG = 0.098     # 头部检查：T-1 涨幅≥9.8% 视为涨停区


def initialize(context):
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark("000300.XSHG")
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.day = 0
    g.pending = {}        # 到期 g.day → (采样日, {code: (信号, 'in'/'out')})
    g.records = []        # 每轮 (采样日, ic, n_in, n_out, fwd_in, fwd_out)
    g.round_info = []     # 每轮 (n_in, n_out, in组涨停数)
    log.info("池区分力探针 v1：不下单，只测 Q1 池内IC / Q2 准入价值")
    run_daily(probe, time='14:55')


def probe(context):
    g.day += 1
    due = g.pending.pop(g.day, None)
    if due is not None:
        resolve(context, due)
    if (g.day - 1) % ROTATE_EVERY == 0:
        sample(context)


# ---------- v1_1 抄件：池与硬剔除 ----------

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
    return get_trade_days(end_date=dt.date(), count=2)[0]


def _mainboard_pool(context):
    """全 A 主板（时点）：剔科创板(68x)、创业板(30x)、北交所(4/8/92 前缀)。"""
    secs = get_all_securities(['stock'], date=context.current_dt)
    return [c for c in secs.index
            if not (c[:2] in ('68', '30') or c[0] in ('4', '8') or c[:2] == '92')]


def _st_filter(cands, dt):
    try:
        st = get_extras('is_st', security_list=cands, end_date=dt, count=1)
        return [c for c in cands if not bool(st[c].iloc[-1])]
    except Exception:
        return cands


def _ban_filter(cands, dt):
    try:
        rows = get_locked_shares(stock_list=cands, start_date=dt,
                                 end_date=dt + pd.Timedelta(days=BAN_WINDOW))
        banned = set(rows['code']) if rows is not None and len(rows) else set()
        return [c for c in cands if c not in banned]
    except Exception:
        return cands


# ---------- 采样 ----------

def sample(context):
    dt = context.current_dt
    asof = _prev_trade_day(dt)

    # 共同硬剔除（in/out 公平对照）：ST、停牌
    pool = _mainboard_pool(context)
    pool = _st_filter(pool, dt)
    cd = get_current_data()
    pool = [c for c in pool if not cd[c].paused]
    if not pool:
        return

    # in 组：v1_1 五道质量筛（顺序与 v1_1 一致）
    ind = get_fundamentals(
        query(indicator.code, indicator.roe,
              indicator.inc_net_profit_year_on_year, indicator.statDate,
              indicator.pubDate), date=asof)
    ind = _prepare_fundamentals(ind, asof)
    ind = ind[(ind['roe_ann'] > ROE_MIN)
              & (ind['inc_net_profit_year_on_year'] > NP_YOY_MIN)]
    q = [c for c in pool if c in ind.index]
    if q:
        val = get_fundamentals(
            query(valuation).filter(valuation.code.in_(q)), date=asof)
        val = val.set_index('code')
        val = val[(val['pe_ratio'] < PE_MAX) & (val['pb_ratio'] < PB_MAX)]
        q = [c for c in q if c in val.index]
    q = _ban_filter(q, dt)
    in_group = []
    sig = {}
    if q:
        closes_q = history(VOL_LOOKBACK, '1d', 'close', security_list=q)
        q = [c for c in q if closes_q[c].notna().sum() >= VOL_LOOKBACK]
        if q:
            rets = closes_q[q].pct_change()
            vol60 = rets.std() * math.sqrt(250) * 100
            q = [c for c in q if vol60[c] < VOL60_MAX]
            for c in q:
                p0 = closes_q[c].iloc[-2]
                p1 = closes_q[c].iloc[-1]
                if pd.isna(p0) or pd.isna(p1) or p0 <= 0:
                    continue
                sig[c] = p1 / p0 - 1
                in_group.append(c)

    # out 组：同池同硬剔除、未过质量筛，日期种子随机抽样
    in_set = set(in_group)
    out_pool = [c for c in pool if c not in in_set]
    rng = random.Random(int(dt.strftime('%Y%m%d')))
    out_sample = rng.sample(out_pool, min(OUT_SAMPLE, len(out_pool)))
    out_sample = _ban_filter(out_sample, dt)
    out_group = []
    if out_sample:
        closes_o = history(3, '1d', 'close', security_list=out_sample)
        for c in out_sample:
            p0 = closes_o[c].iloc[-2]
            p1 = closes_o[c].iloc[-1]
            if pd.isna(p0) or pd.isna(p1) or p0 <= 0:
                continue
            sig[c] = p1 / p0 - 1
            out_group.append(c)

    if not in_group or not out_group:
        log.info("采样日 %s：in=%d out=%d，样本不足跳过" %
                 (dt.date(), len(in_group), len(out_group)))
        return

    # 头部检查：in 组涨停区数量（≥TOP_N=10 时 Q1 结论需降级）
    n_lu = len([c for c in in_group if sig[c] >= LIMIT_UP_CHG])
    g.round_info.append((len(in_group), len(out_group), n_lu))
    log.info("采样日 %s：in=%d（涨停区 %d）out=%d" %
             (dt.date(), len(in_group), n_lu, len(out_group)))

    saved = {c: (sig[c], 'in') for c in in_group}
    for c in out_group:
        saved[c] = (sig[c], 'out')
    g.pending[g.day + HORIZON + 1] = (dt.date(), saved)


# ---------- 结算 ----------

def resolve(context, due):
    s_date, saved = due
    codes = list(saved.keys())
    closes = history(HORIZON + 1, '1d', 'close', security_list=codes)
    ins, outs, pairs = [], [], []
    for c in codes:
        p0 = closes[c].iloc[0]
        p1 = closes[c].iloc[-1]
        if pd.isna(p0) or pd.isna(p1) or p0 <= 0:
            continue
        fwd = p1 / p0 - 1
        s, grp = saved[c]
        if grp == 'in':
            ins.append(fwd)
            pairs.append((s, fwd))
        else:
            outs.append(fwd)
    if len(pairs) < 3 or not outs:
        return
    df = pd.DataFrame(pairs, columns=['sig', 'fwd'])
    # 秩相关走 rank+pearson，避免引擎无 scipy 时 method='spearman' 报错
    ic = df['sig'].rank().corr(df['fwd'].rank())
    g.records.append((s_date, ic, len(ins), len(outs),
                      sum(ins) / len(ins), sum(outs) / len(outs)))


# ---------- 汇总 ----------

def _tstat(xs):
    n = len(xs)
    if n < 3:
        return 0.0, 0.0
    m = sum(xs) / n
    var = sum([(x - m) ** 2 for x in xs]) / (n - 1)
    sd = math.sqrt(var)
    return m, (m / sd * math.sqrt(n) if sd > 0 else 0.0)


def on_strategy_end(context):
    recs = [r for r in g.records if not pd.isna(r[1])]
    info = g.round_info
    if not recs:
        log.info("无有效样本轮次")
        return
    ics = [r[1] for r in recs]
    m_ic, t_ic = _tstat(ics)
    pos_frac = len([x for x in ics if x > 0]) / len(ics)
    diffs = [r[4] - r[5] for r in recs]
    m_diff, t_diff = _tstat(diffs)
    m_in = sum([r[4] for r in recs]) / len(recs)
    m_out = sum([r[5] for r in recs]) / len(recs)
    avg_n_in = sum([i[0] for i in info]) / len(info) if info else 0
    avg_n_lu = sum([i[2] for i in info]) / len(info) if info else 0
    max_n_lu = max([i[2] for i in info]) if info else 0
    lines = [
        "",
        "==================== 池区分力探针 v1 汇总 ====================",
        "有效轮次: %d（采样每%d交易日，前瞻%d交易日）" % (len(recs), ROTATE_EVERY, HORIZON),
        "头部检查: in组均 %.0f 只，其中涨停区均 %.1f / 最大 %d（≥10 则Q1降级）"
        % (avg_n_in, avg_n_lu, max_n_lu),
        "Q1 池内 rank-IC: 均值 %+.4f  t=%+.2f  IC>0占比 %.0f%%"
        % (m_ic, t_ic, pos_frac * 100),
        "Q2 准入价值: fwd_in 均 %+.2f%%  fwd_out 均 %+.2f%%  差 %+.2f%%  t=%+.2f"
        % (m_in * 100, m_out * 100, m_diff * 100, t_diff),
        "判读: |t|≥2 显著；Q1 t≤-2 为反转；Q2 t≤-2 池为拖累；|t|<2 无区分/装饰",
        "=============================================================",
    ]
    for ln in lines:
        log.info(ln)
