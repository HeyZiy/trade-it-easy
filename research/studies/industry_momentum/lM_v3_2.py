# -*- coding: utf-8 -*-
# lM_v3_2 —— v3_1_1 + 上行跟踪止盈（2026-10-06，基于 lM_v3_1_1 复制）
# 本版唯一变更=出场规则加一条"从浮盈最高点回撤 20% 即清仓"，配套一条买入侧
# 冷静期；池、打分器、排名闸、执行、成本模型一字未改。
#   动机（lM_v3_1_1 实测，result_IM_v3_1_1.csv + 台账日志，窗口 2024-01-02~
#   2026-09-30）：maxDD -31.06%（峰 2026-06-30 净值 241,260 → 谷 2026-09-15
#   166,330，期末未修复），其中 -74,930 的回吐**全部是持仓浮盈回吐**——同期
#   8 笔平仓净贡献 -581。主犯是 512480 半导体：2024-10-17 建仓后 478 个交易日
#   没有一天收盘低于成本，2026-06-30 见顶 1.4814（成本 +242%），随后跌到
#   0.955（峰值 -35.5%）。它相对成本仍 +118% 故成本止损永不触发，它一直在
#   前 40% 排名内故排名闸也不卖——两条既有出场规则对"利润高点自由落体"全盲。
#   MAE/MFE 反事实（本地复权缓存，59/59 与平台 pnl 恒等校验通过）：
#     成本止损 -8%/-10%/-12%/-15% 的 delta = -11,317/-8,700/-7,976/-10,351
#     ——**全为负**。因为排名闸本身已等效一个 -12~-15% 成本止损（光伏途深
#     -12.2%、有色 -13.6%、地产 -12.5%，最低点即卖出日，加止损省不到钱），
#     而唯一被误砍的通信 2024-03 那笔（途深 -17%、持 243 天、最终 +4,692）
#     单独就贡献 -9,297。故本版**不做成本止损**。
#     移动止盈 峰回 15%/20%/25%/30% 的 delta = -51,040/**+8,038**/+7,881/+2,675
#     ——15% 会把半导体砍在 +2,384（-55,429），20% 起大牛全部存活，20~25% 是
#     唯一可行带。取 20%：半导体那笔浮盈从 +39,466 变 +54,884（+15,418），
#     正好截在造成 -31% 的那一段上。
#   冷静期 REENTRY_BAN=20 交易日（结构值=REBUILD_EVERY，不新调参数）：跟踪
#   止盈与 score 正交——破位后 25 日回归斜率可能仍是全场第 1，而刚卖掉的
#   code 已不在 held 里，同一次 14:55 的"后买"循环会把它按破位价买回，止损
#   退化为纯摩擦。故**仅跟踪止盈出局的 code** 记入 ban（排名闸出局的 code
#   本来就在后排，不记 ban——保证与 v3_1_1 的排名行为逐字一致）。ban 只让
#   买入循环跳到再下一名，槽位不空转、钱照样按 sleeve/TOPN 打出去。
#   已知局限（读结果时必带）：①+8,038 里 +15,418 来自半导体一笔，n=1 主导，
#   与本线一贯"右尾定生死"同病；②本地反事实不含再投资效应（提前腾出的槽位
#   买谁只有平台知道），只有本版平台跑分才算真测；③本线胜率/盈亏比不因
#   止盈改变对入场质量的评价——MAE 表显示赢单几乎从不破成本、亏单几乎从不
#   回头，入场侧无可用的区分量，这一条仍是"不到能用"的老问题。
#   判读预案：若 maxDD 显著浅于 -31.06% 而总收益不低于 +69.45%，说明跟踪
#   止盈打中了痛点；若总收益大幅掉回，说明它砍掉了 2026-06-30 之后本该继续
#   走的大牛（即 +15,418 是拟合出来的一次性运气）。
#   运行：**聚宽回测 2024-01-01 ~ 2026-09-30、初始资金 10 万、天频**——与
#   v3_1_1 实跑窗口逐字对齐（注意 v3_1 的 06-01 窗口不是本版对照基准）。
#
# 结果头注（回填区）：
#   策略收益
# 76.93%
# 策略年化收益
# 23.89%
# 超额收益
# 39.31%
# 基准收益
# 27.00%
# 阿尔法
# 0.129
# 贝塔
# 1.296
# 夏普比率
# 0.636
# 胜率
# 0.431
# 盈亏比
# 1.417
# 最大回撤 
# 28.31%
# 索提诺比率
# 0.909
# 日均超额收益
# 0.06%
# 超额收益最大回撤
# 21.42%
# 超额收益夏普比率
# 0.448
# 日胜率
# 0.503
# 盈利次数
# 25
# 亏损次数
# 33
# 信息比率
# 0.700
# 策略波动率
# 0.313
# 基准波动率
# 0.186
# 最大回撤区间
# 2026/06/30,2026/08/03
#   对照基准 v3_1_1（2024-01-02 ~ 2026-09-30，10 万，实测）：总 +69.45% /
#     年化 21.20% / maxDD -31.06%（2026-06-30→09-15，未修复）/ 夏普 0.784 /
#     59 笔（24盈35亏）；完整面板/台账/切点对照见 lM_v3_1_1.py 头注
#     （v3_1 留档 +82.67% 系 06-01 窗，非本版对照基准）。
#
# 规则口径：池（含 v3_1_1 命名补口径）/打分器/排名闸/执行与成本 = v3_1_1 一字
#   未改，本版唯一变更见顶部；v3_1_1 结案与 v3_1/v3/v2_1 实验史见各自文件
#   头注；正式规格见 strategy/industry_momentum.md。

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

    # 先卖：持仓跌出前 40% 或 从浮盈最高点回撤 TRAIL_DD（停牌/未计分/已出池 → 持有）
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
