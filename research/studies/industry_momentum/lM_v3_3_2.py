# -*- coding: utf-8 -*-
# lM_v3_3_2 —— v3_3_1 + 保本损抬升：浮盈曾达 +12% → 限亏价抬到 成本×1.01（2026-10-06，基于 lM_v3_3_1 复制）
# 本版唯一规则变更 = 限亏线加一档"保本抬升"：任一日浮盈 ≥ +12%（相对成本）即置位
#   （布尔只升不降，清仓重置），此后限亏价从 成本×0.92 抬到 成本×1.01；score≤0
#   出场的 3 日确认、买入侧、池六关、打分器、执行与成本与 v3_3_1 一字未改。
# 动机（mae_v1 反事实，受管路径 105 笔，reports/mae_v1/）：纯 -8% 限亏 Σ+7,189、
#   0 赢单误伤；加保本抬升 Σ+8,589（边际 +1,400 ≈1.4% 本金），18 笔触发仍 0 误伤
#   ——结构保证：16 笔大浮盈赢单的成本回踩全部发生在 +12% 之前（赢单从不回成本），
#   抬升只救"从 +12% 跌回成本下方"的票（3 笔，实际亏 -1,256）。已知限制：模拟在
#   v3_2_1 路径（rank+峰回出场）上做的，v3_3_1 路径部分出场会被 score 闸提前处理，
#   边际可能缩水。实现注意：置位用布尔标志（浮盈比值对份额折算不变），不自维护
#   价格水位——v3_5 的 g.peaks 折算 bug 不重犯。
#   判读：vs v3_3_1（+62.51% / maxDD -21.77% / 夏普 0.589 / 81 笔）差值 = 保本
#   抬升净效应。本地边际只有 ≈1.4%，诚实预期是小幅正或零；收益/回撤至少一项
#   不劣化且方向为正 → 并入现行口径；明显变差 → 撤销结案。
#   运行：聚宽回测 2024-01-01 ~ 2026-09-30、初始资金 10 万、天频（与 v3_3_1 一致）。
#
# 结果头注（回填区）：
#   v3_3_2 本轮（2024-01-01 ~ 2026-09-30，10 万，实测已回填）：
#     总 **+64.86%** / 年化 20.64% / maxDD **-21.72%**（2026-06-25→09-28，
#     与 v3_3_1 同区间）/ 夏普 0.611 / 索提诺 0.872 / β 1.010 / α 0.112 /
#     IR 0.570 / 波动 27.2% / 胜率 46.3% / 盈亏比 1.697 / 80 笔（37盈43亏）/
#     超额 +29.81% / 超额回撤 -16.98% / 超额夏普 0.319 / 日胜率 50.9% /
#     基准 +27.00%。
#     vs v3_3_1（唯一差异=保本损抬升）：收益 **+2.35pp**（本地反事实预估
#     ≈+1.4pp，同量级兑现）、夏普 0.589→0.611、索提诺 0.841→0.872、
#     盈亏比 1.644→1.697、超额回撤 -17.94%→-16.98%、maxDD -21.77%→-21.72%
#     持平、交易 81→80、胜率 46.9%→46.3% 微降——方向与幅度均在本地预估
#     之内，无恶化项。
#   裁决：**并入现行口径，红线核毕正式收线（2026-10-07）**（预案命中：收益
#     改善、回撤不劣化）。日志红线核验：全篇触发[保本损] 仅 1 笔——电网
#     设备 159326，2026-03-23 @1.799（-286 元）；出场后先 -13.8%、6 月反弹
#     +25.5%、窗口末收 1.551（比出场价低 14.3%）——按"出场价 vs 期末价"
#     会计，出场反而占优，非错杀，0 误伤红线通过。
#   日志另见两处平台执行现实（非策略 bug）：崩盘日跌停致部分成交、卖出跨
#     两日（有色 2025-04-07/08、半导体设备 2026-07-02/03）；以及 score=
#     150.79 顶部买入半导体设备、两日 -13.7k 被 -8% 限亏当日接住——机制按
#     设计工作。台账 82 笔（37盈45亏）Σ+53,481，分年 2024 +17,727 / 2025
#     +32,858 / 2026 +2,896（受管后 2026 已实现转正），落袋 82.5%、TOP3
#     94.9%（通信 +32,475 / 石油 +9,219 / 消费电子 +9,066）。
#   收线状态：**v3_3_2 = 现行候选**（+64.86% / maxDD -21.72% / 夏普 0.611 /
#     β 1.010）。待办：strategy/industry_momentum.md 与生产对齐到 v3_3_2
#     口径（score 出场 3 日确认 + -8% 限亏 + 保本损抬升 + 量价热度闸常开 +
#     出池直评），另行裁决。
#   对照基准 v3_3_1：+62.51% / maxDD -21.77% / 夏普 0.589 / 81 笔（见
#     lM_v3_3_1.py 头注）。
#
# 规则口径：池六关 / 打分器 / 买入侧 / 执行与成本 = v3_3_1 一字未改；唯一变量 =
#   限亏价保本抬升。正式规格见 strategy/industry_momentum.md。

import math

import numpy as np

from jqdata import *

TOPN = 3
STOP_COST_PCT = 0.08                 # 限亏：跌破成本 -8% 清仓（MAE 重做：0 赢单误伤边界，Σ+7,189）
BREAKEVEN_TRIGGER = 0.12             # 本版唯一新参数：浮盈曾达 +12% → 限亏价抬到 保本上方 1%
BREAKEVEN_STOP = 0.01                # 保本微利线（成本×1.01）；布尔置位只升不降，清仓重置
SCORE_EXIT_CONFIRM = 3               # score≤0 连续 N 日才清仓（churn 缓冲，限亏不延迟）
CROWD_LOOKBACK, CROWD_MIN_OBS, CROWD_MAX = 250, 60, 101.0   # 量价热度准入阈值沿用 101 分（闸常开），热度仍计算入日志
MIN_BARS = 250
LIQ_AMT20_MIN = 50_000_000.0
REBUILD_EVERY = 20
CORR_DEDUPE = 0.90
MATURE_DAYS = 365
SCORE_DAYS = 25                      # new.py m_days：25 根收盘 + 当日价

# 名称剔除词：宽基/规模/风格/债券/货币/商品/跨境——行业与主题保留
# v2_1 补充（末尾 7 词）：堵 v2 实测漏网的跨境变体命名
EXCLUDE_KW = ('沪深300', '中证500', '中证1000', '中证800', '中证全指', '2000', '200',
              '中证A500', 'A500', 'A50', 'A100', '上证50', '上证180', '上证380', '科创50',
              '科创100', '科创综', '创业板50', '创业板综', '创业板指', '创业板',
              '双创', '北证', '深证100', '基本面50', '红利', '股息', '国债', '政金',
              '信用', '债券', '转债', '货币', '现金', '黄金', '白银', '原油', '豆粕',
              '商品', '纳斯达克', '纳指', '标普', '道琼', '日经', '德国', '法国',
              '亚太', '东南亚', '恒生', '香港', 'H股', '港股', '央企', '国企',
              '龙头', 'ESG', '养老', 'FOF', '联动', '增强', '价值', '成长',
              '质量', '低波', '动量', '多因子', '自由现金流',
              'HK', '225', '东证', '中韩', '美国', '恒指', '油气',
              '债', '上海金',
              # v3_1_1 补：货币/短融类命名漏网（511360/511880/511990/159003）
              '短融', '日利', '添益', '快线')

# 代码段硬剔除，不依赖命名：513xxx 沪市跨境 ETF / 518xxx 上金所黄金现货 ETF
EXCLUDE_CODE_PREFIXES = ('513', '518')


def initialize(context):
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
    """new.py 动量评分器镜像（EtfRotation.filter_moment_rank 逐条一致）。

    close_tail 末 SCORE_DAYS 根收盘 + 当日 last_price 共 26 点；
    对数价格加权回归（w=linspace(1,2)），score=年化×加权R²；
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
        ss_res = np.sum(w * (logp - (slope * x + intercept)) ** 2)
        ss_tot = np.sum(w * (logp - np.mean(logp)) ** 2)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
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
    if not g.pool:
        return
    cd = get_current_data()
    today = dt.strftime('%Y-%m-%d')

    closes = history(CROWD_LOOKBACK, '1d', 'close', security_list=g.pool)
    money = history(CROWD_LOOKBACK, '1d', 'money', security_list=g.pool)
    rowsum = money.sum(axis=1)

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

    # 先卖：两条自身规则，不依赖池——①自身动量 score 转负（≤0，含跳水清零），
    # 加 3 日确认；②限亏线：默认 成本×0.92（急跌保险，不延迟），浮盈曾达 +12%
    # 后抬至 成本×1.01（保本损，布尔置位只升不降；浮盈比值对份额折算不变，
    # 无 v3_5 式水位 bug）。持仓出池则直接评分，池洞结构性消失
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
            if h is None or len(h) < SCORE_DAYS:
                continue                            # 数据不足不评估，仍持有
            score = momentum_score(list(h['close'].values), px)
        neg = 0 if score > 0 else g.neg_run.get(sec, 0) + 1
        g.neg_run[sec] = neg
        if not below_stop and neg < SCORE_EXIT_CONFIRM:
            continue                                # 确认期未满，仍持有
        reason = ("保本损" if latched else "跌破成本-8%") if below_stop \
            else "score转负x%d日" % neg
        pnl = ((px - pos.avg_cost) * pos.total_amount
               - px * pos.total_amount * 0.0001)   # 卖出万一佣金
        order_target(sec, 0)
        g.neg_run.pop(sec, None)
        g.be.pop(sec, None)
        g.trades.append({'date': today, 'code': sec,
                         'name': get_security_name(sec), 'pnl': pnl})
        log.info("卖出 %s %s 触发[%s] score=%.4f 成本%.3f 现价%.3f 平仓盈亏 %+.0f"
                 % (sec, get_security_name(sec), reason, score,
                    pos.avg_cost, px, pnl))

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


def on_strategy_end(context):
    """免费日志替代交易 CSV 导出：集中度/分年/浮盈口径一次性打全。"""
    trades = g.trades
    total = float(sum([t['pnl'] for t in trades]))
    wins = [t for t in trades if t['pnl'] > 0]
    log.info("【台账】平仓 %d 笔（盈 %d 亏 %d）| Σ已实现 %+.0f"
             % (len(trades), len(wins), len(trades) - len(wins), total))
    ts = sorted(trades, key=lambda t: -t['pnl'])
    for i, t in enumerate(ts[:5]):
        log.info("【台账】TOP%d %s %s %s pnl=%+.0f"
                 % (i + 1, t['date'], t['code'], t['name'], t['pnl']))
    top3 = float(sum([t['pnl'] for t in ts[:3]]))
    ratio = top3 / total * 100 if total > 0 else float('nan')
    log.info("【台账】TOP3=%+.0f 占净利 %.1f%% | 其余 %d 笔合计 %+.0f"
             % (top3, ratio, len(ts) - 3, total - top3))
    years = {}
    for t in trades:
        d = years.setdefault(t['date'][:4], [0.0, 0])
        d[0] += t['pnl']
        d[1] += 1
    for y in sorted(years):
        log.info("【台账】分年平仓 %s: %+.0f（%d 笔）" % (y, years[y][0], years[y][1]))
    upnl = 0.0
    for s, p in context.portfolio.positions.items():
        if p.total_amount > 0:
            u = p.value - p.total_amount * p.avg_cost
            upnl += u
            log.info("【台账】期末持仓 %s %s 浮盈 %+.0f"
                     % (s, get_security_name(s), u))
    tv = context.portfolio.total_value
    sc = context.portfolio.starting_cash
    log.info("【台账】期末未实现 %+.0f | 期末权益 %.0f（总收益 %.2f%%）| 已实现占利润 %.1f%%"
             % (upnl, tv, (tv / sc - 1) * 100, total / (tv - sc) * 100))
