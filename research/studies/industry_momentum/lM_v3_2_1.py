# -*- coding: utf-8 -*-
# lM_v3_2_1 —— v3_2 + 可卖域交还给池（去重贪心以"已持仓"为种子）（2026-10-06，基于 lM_v3_2 复制）
# 命名：本版只改池的**作用域口径**、不新增任何信号规则，故按 v3_1_1 的先例做补丁级
#   bump（v3_2_1）而非 v3_3——v3_3 留给真正的第三条出场/入场规则实验。
# 本版唯一规则变更=rebuild_pool 第 5 关（250 日相关 ≥0.90 贪心去重）的起点：
#   kept 先塞入**当前已持仓的 code**（种子），候选仍按 20 日均额降序遍历，但与
#   种子高相关的候选一律被顶掉。种子不参与成熟 / 流动性 / 去重三关（跨境段与
#   命名两关只作用于新候选，天然不影响种子）。
#   不对称是刻意的：种子只保证**可卖**，不保证**可买**——它已在 held，买入循环
#   会跳过它；而它的孪生票连备选都不是（用户裁决理由：已经持有的仓位没有理由
#   换成同涨同跌的另一只，"换过去"只是白付一次来回佣金）。
#   动机（v3_2 台账 + 本地六关卡尺复现）：**池在本线一直同时兼任"选股域"和
#   "可卖域"两个角色**，而 run_rotation 卖侧第一行是 `r = rank.get(sec);
#   if r is None: continue`——一旦持仓被去重挤出池，它对排名闸和 v3_2 的峰回闸
#   同时免疫，且再也买不回来，还永久冻结 TOPN 的一个槽位。
#   覆盖率实尺（缓存复现六关；校准：34 个重建日去重后规模逐日中位差 -2 只、
#   合计 1337 vs 平台 1398=0.96，109 条成员铁证（买入日 / 触发[排名]卖出日必在
#   池）命中 107=98%，漏的 3 条是 511220/515900/520700 命名源差异）：
#     v3_2 全窗 1981 个持仓日里 **856 日（43.2%，市值口径 44.9%）无任何出场规则
#     管辖**，集中在两只——
#       512480 半导体 466/478 天出池（2024-11-04 起被 588200 顶掉，相关 0.92~0.95）
#         → 期末浮盈 +38,531；v3_1_1 的 -74,930 回撤里单它贡献 -39,3k=**52%**，
#         且它 06-30 市值占权益 45.8%。
#       515880 通信 240/243 天出池 → 2025-03-05 **重新入池的当天**就以
#         rank 33/36 被排名卖出，即台账 TOP1 的 +30,626。
#     判读改写：本线一直在抱怨"TOP3=208%、利润靠两只大票"，现在要说的是——
#     **最大的两笔利润不是策略选出来的，是策略卖不掉的。**
#   问题：那 856 个无管辖持仓日（占 43.2%）是漏管，不是策略的收益来源。
#   已知副作用（不做二次拆分）：本版同时动了买入候选集（孪生票被挤），严格说
#   不是纯作用域修复。
#   免费日志补丁（纯记录、零信号差异）：rebuild_pool 打印 kept 全列表与种子数
#   ——覆盖率从此可用平台日志直接核，不必再靠本地复现推断。
#   运行：**聚宽回测 2024-01-01 ~ 2026-09-30、初始资金 10 万、天频**（与
#   v3_1_1 / v3_2 实跑窗口逐字对齐；对照基准 = v3_2，不是 v3_1_1）。
#
# 结果头注（回填区）：
#   v3_2_1 本轮（2024-01-01 ~ 2026-09-30，10 万，实测已回填）：
#     总 **+50.78%** / 年化 16.67% / maxDD **-28.02%**（2026-01-28→09-28）/
#     夏普 0.440 / 索提诺 0.621 / β 1.133 / α 0.066 / IR 0.367 / 波动 28.8% /
#     胜率 43.8% / 盈亏比 1.318 / 105 笔（46盈59亏）/ 超额 +18.72pp /
#     超额回撤 -24.49% / 超额夏普 0.134 / 日胜率 51.1% / 基准 +27.00%。
#     vs v3_2（唯一差异=可卖域交还池）：收益 **-26.15pp**、年化 -7.22pp、
#     夏普 0.636→0.440、索提诺 0.909→0.621、IR 0.700→0.367、α 0.129→0.066、
#     超额回撤 -21.42%→-24.49% 恶化 3.07pp、交易 58→105 笔；maxDD
#     -28.31%→-28.02% 仅浅 0.29pp，且回撤形态从 2026-06-30→08-03 一段尖回撤
#     变成 2026-01-28→09-28 八个月长回撤。vs v3_1_1（同窗、无峰回、无种子）：
#     maxDD -31.06%→-28.02% 浅 3.04pp、收益 -18.67pp——但 v3_2 已证明那 3pp
#     用峰回闸就能拿到（-28.31%、+76.93%），**本版多付的 26pp 买到的只是
#     0.29pp 回撤改善**（面板口径）。
#   结论（2026-10-06，日志已回贴）：**动机证实**——那 856 天确是漏管，不是
#   策略的收益来源。
#     ① 无管辖日归零：每期重建把当期持仓塞回池（日志 34 个重建日全部"含种子 3"），
#       期内成员固定、持仓不可能出池；两只幽灵仓均带 rank 被卖——512480 两次、
#       515880 一次。日志无反例（停牌豁免未出现）。
#     ② maxDD -28.02%，浅于 v3_2 的 -28.31%——但只浅 0.29pp，代价是 26.15pp 的
#       收益（+76.93%→+50.78%），不划算。
#     两只幽灵仓受管后的实际路径：512480 在 2024-07-19（+849）、2024-11-26
#       （+2,760）两次排名卖出，v3_2 的 +38,531 期末浮盈在本路径变成两笔已实现
#       +3,609；515880 2024-04-02 以 rank 21/37 卖出 -395，但 2025-05-13 受管
#       再入、2025-09-02 以 rank 39/46 卖出 +36,399 = 本版台账 TOP1，比幽灵仓
#       还多。即：无管辖贡献了那笔特定利润，但主线能被有管辖的策略重新捕获，
#       真正的病根是"拿不住大赢家"（半导体被切后一路涨到 2026-06）。
#     峰回闸本版 4 次触发：银行 +2,928 / 金ETF南方 **-8,861** / 消费电子
#       +8,520 / 科创指基 **-8,243**，**净 -5,656**；两次亏损触发的卖出时
#       rank 为 16/59、13/60——都在排名闸保护带内，属峰回闸独立作为。MAE
#       反事实的 +8,038 是 v3_1_1 路径的结论，本路径为负——路径依赖再显形。
#     台账：Σ已实现 +42,222；**TOP3=+64,653=153.1%**（通信 +36,399 / 军工
#       +14,999 / 科创芯片 +13,255），其余 102 笔 -22,431；分年 2024 +12,995
#       （31 笔）/ 2025 +53,931（42 笔）/ **2026 -24,704**（32 笔）；期末
#       未实现 +9,027（v3_2 为 +45,871），**已实现占利润 83.2%**（v3_2 33.5% /
#       v3_1_1 32.8%）——持仓受管后利润从账面浮盈变成落袋，右尾集中度
#       208%→153% 同向改善，这是本版真正的结构收益。
#   最终裁定（2026-10-06）：**不采纳，留档标价**。
#     这版把"持仓逃出售卖规则"这个洞标出了价格：≈26pp/2.75 年——v3_2 的
#     超额主要就是两只幽灵仓扛出来的，管住它们 +26.15pp 就没了，而回撤只
#     改善 0.29pp、夏普 0.636→0.440，不值。它同时暴露更深的病根：退出规则
#     会把大趋势半路卖掉——半导体 2024-11-26 被切后一路涨到 2026-06（这一
#     刀 ≈35k，是 -26pp 的主体；通信同情形 2025 再入赚回 +36,399，卖对卖错
#     因票而异）；峰回闸本路径 4 触发净 -5,656，与排名闸叠加无正贡献。
#     真正该回答的问题是"怎么拿住大赢家"，不是继续修池——与 v3_2 结案
#     判读"痛点在射程外"互为印证。现行候选维持 v3_1 口径不变，本版留档。
#   对照基准 v3_2（2024-01-02 ~ 2026-09-30，10 万，实测已回填）：总 +76.93% /
#     年化 23.89% / maxDD -28.31%（2026-06-30→08-03）/ 夏普 0.636 / 58 笔 /
#     台账峰回全窗仅 1 笔（512480 因出池免疫未被触发——正是本版要修的洞）。
#     移动止盈（峰回 20%）+ 冷静期的机制、MAE 反事实与判读预案见 lM_v3_2.py
#     头注；v3_1_1 / v3_1 / v3 各版实验史见各自文件头注。自本版起头注不再
#     全量继承祖先链，只带：本版变更与结论、对照基准摘录、本版结果。
#
# 规则口径：池六关 / 打分器 / 排名闸 / 执行与成本模型 = v3_1_1 一字未改
#   （本版唯一变更见顶部）；正式规格见 strategy/industry_momentum.md。

import math

import numpy as np

from jqdata import *

TOPN, EXIT_PCT = 3, 0.40
CROWD_LOOKBACK, CROWD_MIN_OBS, CROWD_MAX = 250, 60, 90.0
MIN_BARS = 250
LIQ_AMT20_MIN = 50_000_000.0
REBUILD_EVERY = 20
CORR_DEDUPE = 0.90
MATURE_DAYS = 365
SCORE_DAYS = 25                      # new.py m_days：25 根收盘 + 当日价
TRAIL_DD = 0.20                      # 本版唯一新变量：浮盈最高点回撤 20% 清仓
REENTRY_BAN = 20                     # 跟踪出局后禁回购的交易日数（=REBUILD_EVERY）

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
    g.peak = {}                           # 持仓期最高 last_price（跟踪止盈基准）
    g.ban = {}                            # 跟踪出局 code -> 解禁交易日序号
    run_daily(run_rotation, time='14:55')


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


def rebuild_pool(dt, held):
    """规则动态池：时点候选 → 跨境/名称/成熟度/流动性/相关性去重（全用当时数据）。

    held（已持仓 code）作去重**种子**：不参与成熟/流动/去重三关，且挤掉与它
    相关 ≥0.90 的候选。池在本版起同时是选股域和可卖域——持仓一旦出池，卖侧
    `if r is None: continue` 会让它对排名闸和峰回闸双双免疫（v3_2 实测 43%
    持仓日如此）。种子只保证可卖：它已在 held，买侧自然跳过，不额外开买入口子。
    """
    secs = get_all_securities(['etf'], date=dt.strftime('%Y-%m-%d'))
    n_all = len(secs)
    today = dt.date()
    seeds = sorted(held)
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
        if code in seeds:                        # 已是种子，不再作候选
            continue
        cands.append(code)
    n_mature = len(cands)
    liq = []
    if cands:
        amt20 = history(20, '1d', 'money', security_list=cands).mean(axis=0)
        liq = [c for c in cands if amt20.get(c, 0) == amt20.get(c, 0)
               and amt20[c] >= LIQ_AMT20_MIN]
    if len(liq) <= TOPN:
        log.info("池重建 %s: 全表 %d | 跨境剔 %d | 名称过 %d | 成熟 %d | 流动 %d"
                 " | 种子 %d（去重跳过）"
                 % (dt.strftime('%Y-%m-%d'), n_all, n_xb, n_name, n_mature, len(liq),
                    len(seeds)))
        return seeds + liq
    rets = history(250, '1d', 'close',
                   security_list=sorted(set(liq) | set(seeds))).pct_change()
    corr = rets.corr(min_periods=120)
    kept = list(seeds)                            # 贪心起点=种子：占位并顶掉自己的孪生票
    for c in sorted(liq, key=lambda x: -amt20[x]):
        ok = True
        for k in kept:
            r = corr.at[c, k]
            if r == r and r >= CORR_DEDUPE:     # NaN 视为不可比 → 保留
                ok = False
                break
        if ok:
            kept.append(c)
    log.info("池重建 %s: 全表 %d | 跨境剔 %d | 名称过 %d | 成熟 %d | 流动 %d"
             " | 去重后 %d（含种子 %d）"
             % (dt.strftime('%Y-%m-%d'), n_all, n_xb, n_name, n_mature, len(liq),
                len(kept), len(seeds)))
    log.info("池成员 %s: %s" % (dt.strftime('%Y-%m-%d'), ' '.join(kept)))
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
        g.pool = rebuild_pool(dt, {s for s, p in context.portfolio.positions.items()
                                   if p.total_amount > 0})   # 持仓=去重种子
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

    # 先卖：持仓跌出前 40% 或 从浮盈最高点回撤 TRAIL_DD
    # 本版起持仓必在池内（种子），故 r is None 只剩停牌 / K线不足两种，仍持有
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0:
            continue
        r = rank.get(sec)
        if r is None:
            continue
        px = float(cd[sec].last_price)
        pk = max(g.peak.get(sec, px), px)
        g.peak[sec] = pk
        trail = px <= pk * (1.0 - TRAIL_DD)
        if not trail and r <= math.ceil(EXIT_PCT * n):
            continue
        pnl = ((px - pos.avg_cost) * pos.total_amount
               - px * pos.total_amount * 0.0001)   # 卖出万一佣金
        order_target(sec, 0)
        if trail:
            # 只对跟踪出局记 ban：排名出局的 code 本来就在后排买不到，记了会
            # 改变 v3_1_1 的排名行为，破坏单变量对照。
            g.ban[sec] = g.day + REENTRY_BAN
        g.peak.pop(sec, None)
        g.trades.append({'date': today, 'code': sec,
                         'name': get_security_name(sec), 'pnl': pnl})
        log.info("卖出 %s %s 触发[%s] 峰%.3f rank %d/%d 平仓盈亏 %+.0f"
                 % (sec, get_security_name(sec),
                    "峰回%.0f%%" % (TRAIL_DD * 100) if trail else "排名",
                    pk, r, n, pnl))

    # 后买：score>0 且量价热度<90（None 放行）的前空槽数等权；跟踪出局者冷静期内跳过
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
               or (r['crowd'] is not None and r['crowd'] >= CROWD_MAX) \
               or g.ban.get(code, -1) >= g.day:
                continue
            px = r['price']
            amount = min(int(per / (px * 1.0001) // 100) * 100,
                         int(cash / (px * 1.0001) // 100) * 100)
            if amount < 100:
                continue
            order(code, amount)
            g.peak[code] = px                 # 新批次以买入价为跟踪止盈基准
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
