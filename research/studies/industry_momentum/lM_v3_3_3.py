# -*- coding: utf-8 -*-
# lM_v3_3_3 —— v3_3_2 + 跳水清零 MA20 门控（2026-10-07，基于 lM_v3_3_2 复制）
# 本版唯一规则变更 = momentum_score 的跳水否决加价格结构门：近 3 个日环比
#   min<0.95 时，仅当现价 < MA20×0.985 才清零；站上 MA20×0.985 的大跌视为
#   假摔，保留回归分。其余（保本损、score≤0 出场 3 日确认、-8% 限亏、买入侧、
#   池六关、执行与成本）与 v3_3_2 一字未改。MA20 用 19 根历史收盘 + 当日现价。
# 动机（dive_ma20_census_v1，受管路径 2061 持仓日 / 62 个清零触发日，判线事先
#   约定）：清零触发日分 MA20 上下两组的事后走势有区分度——上方组 fwd20 中位
#   +4.63%、胜率 62.9%（n=35）vs 下方组 +1.08%（n=27），差 +3.55pp 过判线；
#   且两组都比非触发日（+0.11%）好——0.95 清零在持仓期平均是"卖掉继续涨的票"，
#   与 v3_3 实测（通信被切在 0.0000 后涨到 3.033）同象。0.95 阈值本身是 new.py
#   抄来的未消融先验，本版第一次动它。
#   连带效应（设计使然）：gated score 同时改买入侧——跳水后回归分保留为正的
#   候选不再被清零除名，可能更早回补；这也算在单变量里。
#   已知局限：census 样本 n=35/27 偏小、条件于 v3_2_1 持仓期；门控缓冲 0.985
#   与 MA20 窗口 20 都是先验值，本轮不扫参。
#   判读：vs v3_3_2（+64.86% / maxDD -21.72% / 夏普 0.611 / 80 笔）差值 = 门控
#   净效应。看三点：①收益与回撤至少一项改善、另一项不劣化 → 并入现行口径；
#   ②交易数应不升（清零减少 → score 转负出场减少；若笔数大增说明买入侧被
#   改坏）；③若两项目标全无改善 → 门控就地收线，0.95 维持原样。
#   运行：聚宽回测 2024-01-01 ~ 2026-09-30、初始资金 10 万、天频（与 v3_3_2 一致）。
#
# 结果头注（回填区）：
#   v3_3_3 本轮（2024-01-01 ~ 2026-09-30，10 万，实测已回填）：
#     总 +58.75% / 年化 18.95% / maxDD **-28.55%**（2026-06-30→09-23）/
#     夏普 0.518 / 索提诺 0.719 / β 1.072 / α 0.092 / IR 0.457 / 波动 28.8% /
#     胜率 44.1% / 盈亏比 1.647 / 68 笔（30盈38亏）/ 超额 +25.00% /
#     超额回撤 **-26.23%** / 超额夏普 0.227 / 日胜率 51.1% / 基准 +27.00%。
#     vs v3_3_2（唯一差异=跳水清零 MA20 门控）：收益 **-6.11pp**、maxDD 深
#     **6.83pp**、夏普 0.611→0.518、超额回撤 -16.98%→-26.23% 恶化 9.25pp、
#     交易 80→68（笔数下降符合方向预期，但省下的往返远抵不过持仓劣化）
#     ——收益与回撤双劣化，判读预案第③条命中。
#   裁决：**门控就地收线，0.95 跳水清零维持原样**，v3_3_2 维持现行候选。
#     census 信号为何没兑现：census 看的是清零日的事后**中位**（+4.63%，
#     假摔占多数），代价却集中在**尾部**——不清零意味着真崩盘的票留仓更久，
#     maxDD 窗口（2026-06→09）正是被门控放进去的。方法学教训存档：出场侧
#     census 判线不能只看中位数，必须同看尾部（最差情形/CVaR）；"中位数好看
#     + 尾部出血"是出场规则最贵的失败形态，与峰回闸（v3_5）的教训同族。
#
# 规则口径：池六关 / 打分器其余 / 买入侧 / 执行与成本 = v3_3_2 一字未改；唯一
#   变量 = 跳水清零的 MA20 门控。正式规格见 strategy/industry_momentum.md。

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
DIVE_MA_N = 20                       # 本版唯一新参数：跳水门控的均线窗口（19 收盘+现价）
DIVE_MA_BUF = 0.985                  # 门控缓冲：现价 < MA20×0.985 才执行跳水清零

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
    """new.py 动量评分器镜像 + 本版唯一变更：跳水否决加 MA20 门。

    close_tail 末 SCORE_DAYS 根收盘 + 当日 last_price 共 26 点；
    对数价格加权回归（w=linspace(1,2)），score=年化×加权R²；
    近 3 个日环比 min<0.95 时，仅当现价 < MA20×0.985 才清零（假摔豁免：
    census 上方组 fwd20 +4.63%/62.9% vs 下方组 +1.08%）。失败返回 0。
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
        ma20 = (np.sum(prices[-DIVE_MA_N:-1]) + prices[-1]) / DIVE_MA_N
        if min(prices[-1] / prices[-2], prices[-2] / prices[-3],
               prices[-3] / prices[-4]) < 0.95 \
                and prices[-1] < ma20 * DIVE_MA_BUF:
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
