# -*- coding: utf-8 -*-
# lM_v3_4_1 —— v3_4 + 买入侧 amom>0 闸（R1 修正：对称化出场/入场，2026-10-06）
# 本版唯一规则变更 = 买入侧加"250 日绝对动量 amom>0 才买"的准入闸，把绝对动量
#   从"单向出场条件"变成"双向持仓条件"。其余（卖出侧 amom≤0 / -8% 停损 / 池六关 /
#   打分器 / 量价热度闸常开 / 执行 / 成本）与 v3_4 一字未改。
# 动机（v3_4 实测崩盘 +2.68%，根因已定位）：v3_4 出场用 250 日动量（amom≤0 卖）、
#   入场仍用 25 日 score（score>0 买），两把尺错配——熊市/震荡市里绝大多数票
#   amom<0 但 score>0，于是每天"卖出→空槽→当日买回同只"，养殖ETF 2025-04 连续
#   约 15 个交易日绞肉。本版把入场同步要求 amom>0：amom<0 的票既不持有也不买回，
#   空槽变现金——回到 Antonacci 绝对动量的原意（12 个月趋势向下 → 持币，而不是
#   换到下一只风险资产）。
# 连带后果（设计使然）：这是逐票的市场门控，2024 无主线年大概率长时间空仓/半仓，
#   持仓集中在少数 amom>0 的防御票（城投/银行/金），不再靠截面硬轮动；顺带治了
#   strategy/industry_momentum.md 风险提示里的"无市场门控：弱市满仓暴露"老账。
#   已知代价：入场门槛抬高后，趋势初段的票会被挡在门外（amom 刚转正前买不进），
#   可能错过主线的启动段——这是"慢信号"一贯的滞后代价。
#   判读：vs v3_4（+2.68% / maxDD 未记 / 70 笔）差值 = 入场闸的净效应。核心看三点：
#   ① 绞肉机是否消失（养殖/军工/证券/游戏/中药/酒等 15 连换手应归零，交易笔数
#   应大幅下降）；② 银行ETF +12891 那类"amom>0 拿住 16 个月"的趋势是否仍被拿住；
#   ③ 现金闲置是否如 v3_4 头注原预期那样真正出现。这是 R1 主假设第一次被干净检验。
#   运行：聚宽回测 2024-01-01 ~ 2026-09-30、初始资金 10 万、天频（与 v3_3/v3_4 一致）。
#
# 结果头注（回填区）：
#   v3_4_1 本轮（2024-01-01 ~ 2026-09-30，10 万，待回填）：
#     总 __% / 年化 __% / maxDD __% / 夏普 __ / β __ / 波动 __% /
#     胜率 __% / 盈亏比 __ / __ 笔（__盈__亏）/ 超额 __pp / 基准 +27.00%。
#     vs v3_4（唯一差异=买入侧 amom>0 闸）：待填。
#     vs v3_3（+53.99% / -23.72% / 0.486 / 106 笔）：待填。
#
# 规则口径：池六关 / 打分器 / 执行与成本 = v3_3 一字未改；唯一变量（相对 v3_4）=
#   买入侧加 amom>0 闸；相对 v3_3 = 卖出侧 25 根 score → 250 日绝对动量 + 买入侧
#   同步 amom>0。正式规格见 strategy/industry_momentum.md。

import math

import numpy as np

from jqdata import *

TOPN = 3
STOP_COST_PCT = 0.08                 # 限亏：跌破成本 -8% 清仓（MAE 重做：0 赢单误伤边界，Σ+7,189）
CROWD_LOOKBACK, CROWD_MIN_OBS, CROWD_MAX = 250, 60, 101.0   # 量价热度准入闸常开（沿用 v3_1_3），量价热度仍计算入日志
MIN_BARS = 250
LIQ_AMT20_MIN = 50_000_000.0
REBUILD_EVERY = 20
CORR_DEDUPE = 0.90
MATURE_DAYS = 365
SCORE_DAYS = 25                      # new.py m_days：25 根收盘 + 当日价（买入侧打分仍用）
ABS_MOM_BARS = 250                   # R1：出场/入场共用的 250 交易日绝对动量（≈12 个月 Antonacci 慢信号）

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


def abs_momentum_250(code, px):
    """R1 慢信号出场：250 交易日（≈12 个月）绝对动量 = 现价 / 250 交易日前收盘 - 1。

    替代 v3_3 的 25 根快信号（score 转负）作为持仓生死线——Antonacci 绝对动量
    的慢信号量级，拿趋势用慢信号。数据不足（<250 根）返回 None（不评估仍持有）。
    attribute_history 默认前复权，历史价锚定今日口径，平滑份额折算/分红跳变。
    """
    h = attribute_history(code, ABS_MOM_BARS, '1d', ['close'], skip_paused=True)
    if h is None or len(h) < ABS_MOM_BARS:
        return None
    base = float(h['close'].values[0])
    if base <= 0:
        return None
    return px / base - 1.0


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
        h = attribute_history(code, ABS_MOM_BARS, '1d', ['close'],
                              skip_paused=True)
        if h is None or len(h) < ABS_MOM_BARS:
            continue                                # 上市未满 250 根（含绝对动量窗口）
        price = float(cd[code].last_price)
        if price <= 0:
            continue
        close_vals = list(h['close'].values)
        score = momentum_score(close_vals, price)
        base = float(close_vals[0])
        amom = price / base - 1.0 if base > 0 else None   # 250 日绝对动量（入场闸共用）
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
                     'price': price, 'score': score, 'crowd': crowd, 'amom': amom})

    rows.sort(key=lambda r: -r['score'])            # 稳定排序，池序解并列
    n = len(rows)
    rank = {r['code']: i + 1 for i, r in enumerate(rows)}

    # 先卖：两条自身规则，不依赖池——① 250 日绝对动量 ≤0（Antonacci 慢信号）；
    # ② 现价跌破成本 -8%。持仓出池直接 attribute_history 评 250 日动量，池洞结构性消失
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0:
            continue
        if cd[sec].paused:
            continue                                # 停牌不评估，仍持有
        px = float(cd[sec].last_price)
        if px <= 0:
            continue
        amom = abs_momentum_250(sec, px)            # None=数据不足不评估
        below_cost = px <= pos.avg_cost * (1.0 - STOP_COST_PCT)
        if not below_cost and (amom is None or amom > 0):
            continue
        reason = "跌破成本-8%" if below_cost else "绝对动量≤0"
        pnl = ((px - pos.avg_cost) * pos.total_amount
               - px * pos.total_amount * 0.0001)   # 卖出万一佣金
        order_target(sec, 0)
        g.trades.append({'date': today, 'code': sec,
                         'name': get_security_name(sec), 'pnl': pnl})
        amom_s = "None" if amom is None else "%.4f" % amom
        log.info("卖出 %s %s 触发[%s] amom=%s 成本%.3f 现价%.3f 平仓盈亏 %+.0f"
                 % (sec, get_security_name(sec), reason, amom_s,
                    pos.avg_cost, px, pnl))

    # 后买：score>0 且 amom>0（250 日绝对动量为正，对称化入场闸）且量价热度闸常开
    # （CROWD_MAX=101）的前空槽数等权
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
               or (r['amom'] is None or r['amom'] <= 0) \
               or (r['crowd'] is not None and r['crowd'] >= CROWD_MAX):
                continue
            px = r['price']
            amount = min(int(per / (px * 1.0001) // 100) * 100,
                         int(cash / (px * 1.0001) // 100) * 100)
            if amount < 100:
                continue
            order(code, amount)
            log.info("买入 %s %s %d份 @%.3f score=%.4f amom=%.4f 量价热度=%s分 rank %d/%d"
                     % (code, r['name'], amount, px, r['score'], r['amom'],
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
