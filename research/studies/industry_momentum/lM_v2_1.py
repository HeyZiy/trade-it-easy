# -*- coding: utf-8 -*-
# lM_v2_1 —— V2 规则动态池 + 跨境硬剔除（2026-10-03，基于 lM_v2 复制修改）
#
# 结果头注（回填区）：
#   v2（2024-01-01 ~ 2026-06-01，本金 10 万）：总 +83.56% / 年化 29.87% /
#     回撤 -21.18% / 夏普 0.866 / 盈亏比 2.196 / 胜率 45.8% / 74 笔平仓。
#     利润结构：前 3 笔 = 净利 97.7%（通信 +35.1k、人工智能 +16.5k、油气 +12.2k），
#     其余 71 笔合计 ≈ +1.6k；2024 全年 -2.9k。
#     跨境漏网 17 笔 = +13.4k（占 20.6%）：油气/美国50/恒指科技/HK创新药/225ETF
#     等绕过 EXCLUDE_KW（'恒生'不盖'恒指'、'纳指/标普'不盖'美国50'、
#     '原油'不盖'油气'、'港股'不盖'HK'）。
#   v2_1 本轮（2024-01-01 ~ 2026-06-01，10 万，同窗口同本金）：
#     总 +52.63% / 年化 19.96% / 回撤 -25.73% / 夏普 0.542 / 盈亏比 1.634 /
#     胜率 37.0% / 81 笔（30盈51亏）/ β 1.217 / 超额 +8.11% /
#     超额夏普 -0.030 / IR 0.198 / 最大回撤区间 2024-03-18~09-09。
#     vs v2：收益 -31pp、回撤反而深 4.6pp、超额夏普 0.364→-0.030——
#     跨境剔除砍掉的不只是漏网利润（直接贡献仅 20.6%），还砍掉了
#     "境内无主线时段的趋势互补"（2024 年 v2 靠跨境浪回血，v2_1 纯亏）。
#     结论：纯 A 股行业动量本体在本窗口 ≈ β 放大（超额≈0），单立不合格；
#     跨境暴露实证有效（收益↑回撤↓），正确形态是显式分 sleeve 而非混池。
#
# 变更（v2 → v2_1，仅池口径，引擎其余一字未改）：
#   命题定为"纯 A 股行业/主题动量轮动"，跨境资产剔除——理由：①收益源异质
#   （油价/美股β/汇率/QDII溢价），ret20 排名失去同质可比性；②QDII 场内溢价
#   使 14:55 中间价成交假设最不可靠（油气 +12.2k 是第 3 大利润）；③拥挤度
#   分母与 gate 语境均为 A 股。跨境暴露如需要，另立大类资产 sleeve 单独验证。
#   实现：513 前缀（沪市跨境段）代码级硬剔除 + 名称补词 'HK','225','东证',
#   '中韩','美国','恒指','油气'（堵深市跨境与变体命名）。
#
# v2 沿革（详见 lM_v2.py 头注）：空池事故真因 = 引擎 any 被 numpy 覆盖对
#   生成器恒真，已改列表推导并单日终验通过（名称过 577|成熟 500|流动 103|
#   去重 44）。EXCLUDE_KW/内置函数自报行保留作哨兵。
#
# 池规则：每 20 交易日重建——全表 ETF → 513 前缀剔除 → 名称剔除（宽基/债券/
#   货币/商品/跨境/风格）→ 上市 ≥365 自然日 → 近 20 日均额 ≥5000 万 →
#   250 日收益相关 ≥0.90 去重（贪心留流动性最高者）。
# 引擎与 v1/v2 逐条一致：20 日收益从强到弱、拥挤度 <90 放行 None、前 3 等权、
#   跌出前 40% 卖、停牌持有、万一单边、无滑点、整手 100。
# 运行：聚宽回测 2024-01-01 ~ 2026-06-01（对齐 v2 窗口），初始资金 10 万，
#   天频率。

import math

from jqdata import *

TOPN, EXIT_PCT = 3, 0.40
CROWD_LOOKBACK, CROWD_MIN_OBS, CROWD_MAX = 250, 60, 90.0
MIN_BARS = 250
LIQ_AMT20_MIN = 50_000_000.0
REBUILD_EVERY = 20
CORR_DEDUPE = 0.90
MATURE_DAYS = 365

# 名称剔除词：宽基/规模/风格/债券/货币/商品/跨境——行业与主题保留
# v2_1 补充（末尾 7 词）：堵 v2 实测漏网的跨境变体命名
EXCLUDE_KW = ('沪深300', '中证500', '中证1000', '中证800', '中证全指', '中证2000',
              '中证A500', 'A500', 'A100', '上证50', '上证180', '上证380', '科创50',
              '科创100', '科创综', '创业板50', '创业板综', '创业板指', '创业板',
              '双创', '北证', '深证100', '基本面50', '红利', '股息', '国债', '政金',
              '信用', '债券', '转债', '货币', '现金', '黄金', '白银', '原油', '豆粕',
              '商品', '纳斯达克', '纳指', '标普', '道琼', '日经', '德国', '法国',
              '亚太', '东南亚', '恒生', '香港', 'H股', '港股', '央企', '国企',
              '龙头', 'ESG', '养老', 'FOF', '联动', '增强', '价值', '成长',
              '质量', '低波', '动量', '多因子', '自由现金流',
              'HK', '225', '东证', '中韩', '美国', '恒指', '油气')

# 沪市跨境 ETF 代码段（513xxx），代码级硬剔除，不依赖命名
CROSS_BORDER_PREFIX = '513'


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
    if not getattr(g, '_kw_reported', False):
        n_empty = sum(1 for k in EXCLUDE_KW if not k or not isinstance(k, str))
        log.info("EXCLUDE_KW自报: %d 个 | 空串/非str %d | 前8个=%r"
                 % (len(EXCLUDE_KW), n_empty, list(EXCLUDE_KW[:8])))
        log.info("内置函数自报: any=%s.%s | sum=%s.%s"
                 % (getattr(any, '__module__', '?'), getattr(any, '__name__', repr(any)),
                    getattr(sum, '__module__', '?'), getattr(sum, '__name__', repr(sum))))
        g._kw_reported = True
    cands = []
    n_xb, n_name, shown = 0, 0, 0
    for code, row in secs.iterrows():
        if code.startswith(CROSS_BORDER_PREFIX):    # 沪市跨境段硬剔除
            n_xb += 1
            continue
        nm = row['display_name']
        if shown < 3:
            log.info("样例[%s] name=%r(%s) start=%r(%s)"
                     % (code, nm, type(nm).__name__,
                        row['start_date'], type(row['start_date']).__name__))
            shown += 1
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
        ret20 = round((price / list(h['close'].values)[-21] - 1) * 100, 3)
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
                     'price': price, 'ret20': ret20, 'crowd': crowd})

    rows.sort(key=lambda r: -r['ret20'])            # 稳定排序，池序解并列
    n = len(rows)
    rank = {r['code']: i + 1 for i, r in enumerate(rows)}

    # 先卖：持仓跌出前 40%（停牌/未计分/已出池 → 持有）
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0:
            continue
        r = rank.get(sec)
        if r is None:
            continue
        if r > math.ceil(EXIT_PCT * n):
            order_target(sec, 0)
            log.info("卖出 %s %s rank %d/%d" % (sec, get_security_name(sec), r, n))

    # 后买：拥挤度<90（None 放行）的前空槽数等权
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
            if code in held or (r['crowd'] is not None and r['crowd'] >= CROWD_MAX):
                continue
            px = r['price']
            amount = min(int(per / (px * 1.0001) // 100) * 100,
                         int(cash / (px * 1.0001) // 100) * 100)
            if amount < 100:
                continue
            order(code, amount)
            log.info("买入 %s %s %d份 @%.3f ret20=%.2f crowd=%s rank %d/%d"
                     % (code, r['name'], amount, px, r['ret20'],
                        r['crowd'], rank[code], n))
            bought += 1
    log.info("%s | 池 %d | 截面 %d | 持仓 %s" % (today, len(g.pool), n,
             [s for s, p in context.portfolio.positions.items()
              if p.total_amount > 0]))
