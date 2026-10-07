# -*- coding: utf-8 -*-
# lM_v3_1_2 —— v3_1_1 + 可卖域交还给池（去重贪心以"已持仓"为种子），无移动止盈
# （2026-10-06，基于 lM_v3_2_1 复制后摘除峰回闸）
# 本版唯一规则变更（相对 v3_2_1）= 删掉 TRAIL_DD / REENTRY_BAN：卖侧回到 v3_1_1
#   的纯排名闸（rank 跌出前 40% 清仓、无峰回、无冷静期）；池作用域口径与
#   v3_2_1 一致（每期重建把当期持仓塞回去重种子，持仓必在池内）。池六关、
#   打分器、执行、成本模型一字未改。
# 目的：v3_2_1 终审（不采纳，留档标价 ≈26pp）发现两件事缠在一起——可卖域
#   修复的代价与峰回闸的贡献。本版摘掉峰回闸，一次跑分同时读两个数：
#   ① vs v3_2_1：峰回闸在受管路径的真实净贡献（日志直读 4 笔 -5,656 只是
#     损益，摘掉后路径全变，须重跑才知道全貌）；若 maxDD 不明显更深，
#     峰回闸从后续实验里移除；
#   ② vs v3_1_1：把持仓管进池本身的纯代价（预期主体 = 半导体 2024-11-26
#     那刀，v3_1_1 路径它持有到期末浮盈 +38,531）。
#   上下文：v3_2_1 终审结论 = 不采纳，真病根是"退出规则会把大趋势半路卖掉"；
#   本版先弄清两个出场部件各自值多少钱，再决定"拿住大赢家"往哪个方向做。
#   运行：聚宽回测 2024-01-01 ~ 2026-09-30、初始资金 10 万、天频（与
#   v3_1_1 / v3_2 / v3_2_1 实跑窗口一致）。
#
# 结果头注（回填区）：
#   v3_1_2 本轮（2024-01-01 ~ 2026-09-30，10 万，实测已回填）：
#     总 **+52.82%** / 年化 17.26% / maxDD **-26.58%**（2026-01-28→09-28）/
#     夏普 0.453 / 索提诺 0.648 / β 1.148 / α 0.071 / IR 0.389 / 波动 29.3% /
#     胜率 43.7% / 盈亏比 1.352 / 103 笔（45盈58亏）/ 超额 +20.32pp /
#     超额回撤 -23.40% / 超额夏普 0.158 / 日胜率 51.1% / 基准 +27.00%。
#     读数① vs v3_2_1（唯一差异=峰回闸）：收益 +2.04pp、maxDD -28.02%→
#     -26.58% 反而浅 1.44pp、夏普 0.440→0.453——**峰回闸受管路径三项全负，
#     移除**。与 v3_2_1 日志 4 触发净 -5,656 互证；MAE 反事实 +8,038 系
#     v3_1_1 路径结论，两条路径实测皆负，峰回闸在本线无价值，结案。
#     读数② vs v3_1_1（唯一差异=种子作用域）：收益 **-16.63pp**、maxDD
#     -31.06%→-26.58% **浅 4.48pp**、夏普 0.784→0.453、交易 59→103——
#     这就是"洞"的干净标价：管住幽灵仓，收益少 16.6pp，换回撤浅 4.5pp、
#     期末不再挂着幽灵仓的账面浮盈。种子修复是真 bug 修复（实盘持仓同样
#     会被挤出池，洞不是回测 artifact），-16.63pp 是退出规则满勤时的真实
#     收益水平——病根不变：截面排名把"别的行业更热"当成"这个趋势结束"。
#     版本地图（同窗 2024-01~2026-09，10 万）：
#       v3_1_1 无种子无峰回 +69.45% / -31.06% / 0.784（收益最高=幽灵仓红利）
#       v3_1_2 种子无峰回 +52.82% / **-26.58%** / 0.453（回撤最好）
#       v3_2   无种子有峰回 +76.93% / -28.31% / 0.636（收益最高，含单笔路径噪音）
#       v3_2_1 种子有峰回 +50.78% / -28.02% / 0.440（全面最差）
#     结论：峰回闸移除出后续实验；现行候选维持 v3_1 口径；种子修复是否并入
#     现行口径由用户裁决（行为自洽 vs 承认 -16.63pp 的真实水平）；"限亏与
#     拿趋势拆开"仍是下一个实验的题目。明细归因（半导体刀在 -16.63pp 中的
#     占比）待台账日志。
#
# 规则口径：池六关（含 v3_1_1 命名补口径 + v3_2_1 种子作用域）/ 打分器 /
#   执行与成本 = v3_1_1 一字未改；正式规格见 strategy/industry_momentum.md。

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
    run_daily(run_rotation, time='14:55')


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


def rebuild_pool(dt, held):
    """规则动态池：时点候选 → 跨境/名称/成熟度/流动性/相关性去重（全用当时数据）。

    held（已持仓 code）作去重**种子**：不参与成熟/流动/去重三关，且挤掉与它
    相关 ≥0.90 的候选。池在本线起同时是选股域和可卖域——持仓一旦出池，卖侧
    `if r is None: continue` 会让它对排名闸免疫（v3_2 实测 43% 持仓日如此）。
    种子只保证可卖：它已在 held，买侧自然跳过，不额外开买入口子。
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

    # 先卖：持仓跌出前 40%（纯排名闸；持仓必在池内，r is None 只剩停牌 /
    # K线不足两种，仍持有）
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0:
            continue
        r = rank.get(sec)
        if r is None:
            continue
        if r <= math.ceil(EXIT_PCT * n):
            continue
        px = float(cd[sec].last_price)
        pnl = ((px - pos.avg_cost) * pos.total_amount
               - px * pos.total_amount * 0.0001)   # 卖出万一佣金
        order_target(sec, 0)
        g.trades.append({'date': today, 'code': sec,
                         'name': get_security_name(sec), 'pnl': pnl})
        log.info("卖出 %s %s rank %d/%d 平仓盈亏 %+.0f"
                 % (sec, get_security_name(sec), r, n, pnl))

    # 后买：score>0 且量价热度<90（None 放行）的前空槽数等权
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
