# -*- coding: utf-8 -*-
# l2_etfself_v2 —— V2 规则动态池 · 实单版（第二次尝试，2026-10-03 重建）
#
# 结果头注（回填区）：
#   V0 基线（同窗口 2019-01-02 ~ 2026-06-01）：总 +158.0% / 年化 13.6% /
#     回撤 -46.2% / 843 笔（2021 起跑 +38.4%、2023 起跑 +123.1%）。
#   V2 本轮：待跑，结果回填此处。
#
# 历史：上一版 v2r 首跑"成熟 0"空池，当时误诊为"平台粘贴损坏中文字面量"，
#   实验被放弃、文件在 84b638b 删除。2026-10-03 用户用 probe_platform_getall.py
#   亲测：粘贴后 EXCLUDE_KW 65 词 0 空串、repr 与本地一字不差，
#   全表 867→名称过 577→成熟过 500，探针过滤链正常——"平台必坏中文"被排除。
#   但从 git 原样恢复的本文件再跑，**同样复现 全表 1069+ | 成熟 0**（日志中文
#   打印正常）→ 问题跟着本文件走，非随机粘贴事故。
#   第二轮单日复跑（内嵌自报）：EXCLUDE_KW 完好、样例名称正常，但"名称过 0"
#   → 名称剔除关全灭。真因锁定：本文件独有的裸 any(生成器)——本地实证
#   np.any(生成器)≡True（内置 any 为 False），疑引擎命名空间覆盖 any，每行
#   恒判"命中剔除词"；探针/v1/new.py 均无裸 any() 故无恙（new.py 作者调
#   builtins.sum 是同类坑旁证）。修复：改列表推导式（探针平台实证写法）。
#   本版改动：rebuild_pool 内嵌自报 + any→hits 列表推导，其余引擎一字未改。
#
# 设计：只跑 V2 一个口径、全部真实 order()，平台收益曲线/仓位/交易明细即策略
#   本身。V0 基线无需另写：同窗口再跑一遍 l2_etfself.py，两份平台报表直接
#   对照，差异全部归因池规则。
# 池规则（V2）：每 20 交易日重建——全体行业/主题 ETF（名称剔除宽基/债券/商品/
#   跨境/货币/风格），上市 ≥365 自然日、近 20 日均额 ≥5000 万（时点值）、
#   250 日收益两两相关 ≥0.90 去重（贪心留流动性最高者）。
# 引擎与 l2_etfself 逐条一致：20 日收益（含当日）从强到弱、拥挤度代理 <90 放行
#   None、前 3 等权、跌出前 40% 卖、停牌持有、万一单边、无滑点、整手 100。
# 唯一配套差异：入截面需 ≥250 根（v1 为 ≥61 根，配合 V2 成熟度门槛）。
# 运行：聚宽回测 2024-01-01 ~ 2026-06-01（2024 窗，同 v2_1/v3/v3_1 线、10 万），
#   天频率。下方结果块即本窗重跑回填——年化 29.87% / 回撤区间 2024-03~2024-09 /
#   72 笔（33盈39亏）均为本窗特征。初跑口径 2019-01-01 起、100 万（对齐 V0 基线
#   窗口 +158.0%）随长窗废止，仅留档不再对照。

# 策略收益
# 83.56%
# 策略年化收益
# 29.87%
# 超额收益
# 30.02%
# 基准收益
# 41.19%
# 阿尔法
# 0.123
# 贝塔
# 1.130
# 夏普比率
# 0.866
# 胜率
# 0.458
# 盈亏比
# 2.196
# 最大回撤 
# 21.18%
# 索提诺比率
# 1.239
# 日均超额收益
# 0.05%
# 超额收益最大回撤
# 17.81%
# 超额收益夏普比率
# 0.364
# 日胜率
# 0.520
# 盈利次数
# 33
# 亏损次数
# 39
# 信息比率
# 0.632
# 策略波动率
# 0.299
# 基准波动率
# 0.181
# 最大回撤区间
# 2024/03/18,2024/09/09

import math

from jqdata import *

TOPN, EXIT_PCT = 3, 0.40
CROWD_LOOKBACK, CROWD_MIN_OBS, CROWD_MAX = 250, 60, 90.0
MIN_BARS = 250
LIQ_AMT20_MIN = 50_000_000.0
REBUILD_EVERY = 20
CORR_DEDUPE = 0.90
MATURE_DAYS = 365

# 名称剔除词（候选）：宽基/规模/风格/债券/货币/商品/跨境——行业与主题保留
EXCLUDE_KW = ('沪深300', '中证500', '中证1000', '中证800', '中证全指', '中证2000',
              '中证A500', 'A500', 'A100', '上证50', '上证180', '上证380', '科创50',
              '科创100', '科创综', '创业板50', '创业板综', '创业板指', '创业板',
              '双创', '北证', '深证100', '基本面50', '红利', '股息', '国债', '政金',
              '信用', '债券', '转债', '货币', '现金', '黄金', '白银', '原油', '豆粕',
              '商品', '纳斯达克', '纳指', '标普', '道琼', '日经', '德国', '法国',
              '亚太', '东南亚', '恒生', '香港', 'H股', '港股', '央企', '国企',
              '龙头', 'ESG', '养老', 'FOF', '联动', '增强', '价值', '成长',
              '质量', '低波', '动量', '多因子', '自由现金流')


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
    """规则动态池：时点候选 → 成熟度/流动性/相关性去重（全部用当时数据）。

    内嵌探针（2026-10-03）：上一版平台实跑复现"全表 1069+ | 成熟 0"空池，
    而独立探针文件同日粘贴过滤链正常——故在本函数内自报 EXCLUDE_KW 完整性
    与逐关计数（名称过/成熟过分开），定位是哪一关杀光、字面量运行时形态。
    """
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
    n_name, shown = 0, 0
    for code, row in secs.iterrows():
        nm = row['display_name']
        if shown < 3:
            log.info("样例[%s] name=%r(%s) start=%r(%s)"
                     % (code, nm, type(nm).__name__,
                        row['start_date'], type(row['start_date']).__name__))
            shown += 1
        # 不用裸 any()：疑平台引擎命名空间将其覆盖为 np.any（对生成器恒真，
        # 致名称过 0 空池）；列表推导式为探针平台实证写法。
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
        log.info("池重建 %s: 全表 %d | 名称过 %d | 成熟 %d | 流动 %d（去重跳过）"
                 % (dt.strftime('%Y-%m-%d'), n_all, n_name, n_mature, len(liq)))
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
    log.info("池重建 %s: 全表 %d | 名称过 %d | 成熟 %d | 流动 %d | 去重后 %d"
             % (dt.strftime('%Y-%m-%d'), n_all, n_name, n_mature, len(liq),
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
