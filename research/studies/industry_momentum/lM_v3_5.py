# -*- coding: utf-8 -*-
# lM_v3_5 —— v3_3 + 出场换成"持仓期高点回撤"追踪止损（让利润奔跑，2026-10-06）
# 本版唯一规则变更 = 卖出侧第二次换血：废除"自身快信号 score 转负（含跳水清零）
#   清仓"，换成"从持仓期高点回撤 TRAIL_PCT 清仓"；保留 v3_3 的 -8% 成本限亏。
#   买入侧（score>0、rank 降序补槽、量价热度闸常开 CROWD_MAX=101）、池六关、打分器、
#   执行与成本 = v3_3 一字未改。
# 动机（三轮出场实验 + 含洞版本实证）：出场信号换来换去，诚实版本全卡在 ~53%
#   —— v3_1_2 +52.82%（截面排名）/ v3_3 +53.99%（自身快信号）/ v3_4 +2.68% /
#   v3_4_1 -5.70%（自身慢信号，β 0.36、胜率 17.6%，完全错过主线）。而**含洞版本**
#   （v3_1_1 +69.45% / v3_1_3 +70.37% / v3_2 +76.93%）反而大幅更高——"池洞"的本质
#   是**持仓一出池，截面排名退出就管不到它 → 一直拿着**，吃到主线后半段。
#   含洞 vs 诚实差值 = "拿住"的标价：v3_1_1−v3_1_2 = +16.6pp；v3_2−v3_2_1 = +26.2pp。
#   本版把这个"意外机制"（幽灵仓）显性化成规则：不再用趋势形态信号（快/慢都会过早
#   砍掉爆发右尾），改用"给利润多少回撤额度"的追踪止损。问题原型：通信 2025-09-02
#   在 score=0.0000 被切在 2.548，之后继续涨到 3.033——趋势形态信号卖在半山腰。
# 规则：peak = 建仓日起累计最高价（g.peaks 逐票维护，卖出/清仓时清除）；
#   出场 = 现价 ≤ peak×(1−TRAIL_PCT)（高点回撤）或 现价 ≤ 成本×(1−0.08)（成本限亏）。
#   双闸互补：新仓 peak≈成本 → -8% 更紧、兜急跌；盈利仓 peak≫成本 → TRAIL 更紧、
#   锁利润（例：涨到 +50% 后回撤 20% 在 +20% 出场）。
# 连带后果（设计使然）：持仓出池照样受管（峰值/成本都只看自身），池洞结构性消失，
#   且这回洞是"故意关掉"的——本版就是要把幽灵仓的收益用规则拿回来。已知代价：
#   震荡市里 TRAIL 给了更多回吐额度（比快信号松）；同 v3_3，-8% 兜底后若 score>0
#   仍可能当日买回，但峰值会重置，不构成 v3_4 那种 amom 绞肉机。
#   判读：vs v3_3（+53.99% / maxDD -23.72% / 夏普 0.486 / 106 笔）差值 = 出场机制
#   从"趋势形态信号"→"高点回撤"的净效应。核心看三点：① 收益能否补回含洞版本那
#   +16~26pp（若能，说明 70%+ 是规则能做到的、不是运气）；② 通信/半导体那类爆发
#   右尾的持有期与退出价是否明显拉长/抬高；③ maxDD 是否因给利润更多回撤额度而变深。
#   TRAIL_PCT 是本版唯一新自由参数，建议扫 0.15 / 0.20 / 0.25（默认 0.20）。
#   运行：聚宽回测 2024-01-01 ~ 2026-09-30、初始资金 10 万、天频（与 v3_3 一致）。
#
# 结果头注（回填区，2026-10-06）：
#   v3_5 本轮（TRAIL_PCT=0.20，2024-01-01 ~ 2026-09-30，10 万）：
#     总 +17.37% / 年化 6.20% / maxDD 9.09%（2026-06-03→07-13）/ 夏普 0.201 /
#     β 0.327（α 0.004）/ 波动 10.9%（基准 18.6%）/
#     胜率 50.0% / 盈亏比 0.947 / 2 笔（1盈1亏）/ 超额 -7.59% / 基准 +27.00%。
#     次要口径：索提诺 0.264 / 信息比率 -0.206 / 超额收益最大回撤 28.58% /
#     超额收益夏普 -0.450 / 日均超额 -0.01% / 日胜率 50.2%。
#     vs v3_3（唯一差异=出场快信号→高点回撤）：+17.37% vs +53.99% = **-36.6pp**，
#     夏普 0.201 vs 0.486 = -0.285，maxDD 9.09% vs 23.72%（浅 14.6pp），
#     **笔数 2 vs 106**——出场几乎全程不触发，3 个槽长期占满、买入侧再无补槽，
#     本版实际退化为"建仓日一次性买定 + 长持"，截面轮动名存实亡。
#     vs 含洞版本（v3_1_1 +69.45% / v3_2 +76.93%）：-52.1pp / -59.6pp。
#     交易日志实貌（聚宽运行日志已核）：2024-01-02 建仓城投ETF(score 0.0534 rank1)
#     / 电力ETF(rank2) / 煤炭ETF(rank3)，此后 33 个月只有 2 笔平仓（煤炭 04-12、
#     有色 04-23），**2024-04-23 之后零交易**；期末持仓 = 城投 + 电力 + 有色大成，
#     期末未实现 +17,550，权益 117,368（总收益 17.37%），已实现仅 -165（占利润 -0.9%）
#     —— 全部收益来自 3 只 2024 年初买入的票长持到期末。
#     本地前复权核证（_cache/etf_daily_full.pkl，自建仓日累计峰）：城投 最大距峰
#     **-3.0%**、有色大成 **-16.0%**（2025-04-09）、电力 **-20.0%**（2026-07-13，
#     差 0.04pp 未触发，与本版 maxDD 区间终点同日）→ TRAIL_PCT=0.20 对这三只
#     结构性够不到，不是运气。
#     已知缺陷（煤炭那笔平仓是**份额折算假象**，非真实回撤）：日志自证 峰值 2.741 /
#     现价 1.297 = 距峰 52.7%，而成本 1.196 恰为建仓价 2.391 的一半、平仓盈亏 +2818
#     需约 27,800 股 = 13,900×2 —— 平台在折算时把 last_price/avg_cost/股数同步折半，
#     本版的 g.peaks 留在折算前口径，于是把"折算"当成 -52.7% 崩盘强制出场（按真实
#     峰谷本应到 2024-07-30 才首次触及 -20%）。凡自维护的峰值/高水位状态都必须随
#     除权除息折算调整，否则触发时点无意义。
#     三点判读：① 收益不但没补回 +16~26pp，反而低于诚实版本 36.6pp → 头注原假设
#       "幽灵仓收益可用规则拿回来"**证伪**：含洞版本的超额来自"部分持仓长持 + 其余
#       照常轮动"的混合，纯粹长持拿不到那笔钱；② 死因比"纯自身规则杀死轮动"更具体
#       = **统一绝对回撤额度 × 异质波动**：25 根加权回归打分器天然把低波慢牛排前面
#       （城投 rank1），固定 20% 额度让这些票永生，3 个槽被债券型/公用事业 ETF 长期
#       占死，主线（通信/半导体）全程无槽可进——β 0.327、波动 10.9%、maxDD 9.09% 全
#       是"慢牛低波长持组合"的画像，不是风控变好；③ 由此 TRAIL 参数扫描不必再投：
#       0.15 只会更早砍掉右尾、0.25 让永生更彻底，都治不了"低波票永不退出"；要治得
#       让额度按票自身波动缩放（如 n×ATR）或对持仓强制定期再评估，那是另一个版本。
#     结论：出场侧第三次换血失败（v3_4 +2.68% / v3_4_1 -5.70% / v3_5 +17.37% 全在
#     v3_3 之下），v3_3（+53.99% / 夏普 0.486）仍是本线最优诚实版本。
#
# 规则口径：池六关 / 打分器 / 买入侧 / 执行与成本 = v3_3 一字未改；唯一变量 =
#   卖出侧 25 根 score 转负 → 持仓期高点回撤 TRAIL_PCT。正式规格见
#   strategy/industry_momentum.md。

import math

import numpy as np

from jqdata import *

TOPN = 3
STOP_COST_PCT = 0.08                 # 限亏：跌破成本 -8% 清仓（MAE 重做：0 赢单误伤边界，Σ+7,189）
TRAIL_PCT = 0.20                     # 本版唯一新参数：从持仓期高点回撤 20% 清仓（让利润奔跑）
CROWD_LOOKBACK, CROWD_MIN_OBS, CROWD_MAX = 250, 60, 101.0   # 量价热度准入闸常开（沿用 v3_1_3），量价热度仍计算入日志
MIN_BARS = 250
LIQ_AMT20_MIN = 50_000_000.0
REBUILD_EVERY = 20
CORR_DEDUPE = 0.90
MATURE_DAYS = 365
SCORE_DAYS = 25                      # new.py m_days：25 根收盘 + 当日价（买入侧打分仍用）

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
    g.peaks = {}                          # v3_5：逐票持仓期最高价 {code: peak}
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

    # 先卖：两条自身规则，不依赖池——① 从持仓期高点回撤 TRAIL_PCT（让利润奔跑，
    # 替代 v3_3 的趋势形态信号 score 转负）；② 现价跌破成本 -8%（新仓急跌兜底）。
    # 峰值/成本都只看自身，持仓出池照样受管，池洞结构性消失
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0:
            g.peaks.pop(sec, None)                  # 已清仓，清峰值
            continue
        if cd[sec].paused:
            continue                                # 停牌不评估，仍持有
        px = float(cd[sec].last_price)
        if px <= 0:
            continue
        peak = g.peaks.get(sec)
        if peak is None or px > peak:               # 建仓日起累计最高价（含兜底初始化）
            peak = px
            g.peaks[sec] = peak
        below_cost = px <= pos.avg_cost * (1.0 - STOP_COST_PCT)
        trail_hit = px <= peak * (1.0 - TRAIL_PCT)
        if not below_cost and not trail_hit:
            continue
        reason = "跌破成本-8%" if below_cost else "高点回撤%.0f%%" % (TRAIL_PCT * 100)
        pnl = ((px - pos.avg_cost) * pos.total_amount
               - px * pos.total_amount * 0.0001)   # 卖出万一佣金
        order_target(sec, 0)
        g.trades.append({'date': today, 'code': sec,
                         'name': get_security_name(sec), 'pnl': pnl})
        g.peaks.pop(sec, None)                      # 清峰值，防下一轮误用
        drop = (1.0 - px / peak) * 100 if peak > 0 else 0.0
        log.info("卖出 %s %s 触发[%s] 峰值%.3f 距峰%.1f%% 成本%.3f 现价%.3f 平仓盈亏 %+.0f"
                 % (sec, get_security_name(sec), reason, peak, drop,
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
            g.peaks[code] = px                      # 建仓即起点：峰值从买入价开始累计
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
