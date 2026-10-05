# -*- coding: utf-8 -*-
# trend_v4 = v3 基建（门控/宇宙/Chandelier/仓位/冷却全保留）+ 信号核替换。
# 版本链 v1→v2→v3→v4；v3 证伪线触发后做方案A尸检+截面研究（见下证据），
# 结论是"何时买（回踩形态择时）"无信息，改为"同一天买哪只（截面排序）"。
# v4 改动：
#   ①删除 detect_signal 全套（回踩形态、C1/D1、评分）——尸检证明信号组 MFE20
#     全面低于结构内随机对照（p90 28.68 vs 31.58），score≥60 子集更差。
#   ②入选条件只剩趋势结构（ma10>ma20、价>ma20、MA20 不降）——截面研究显示
#     结构内池才有右尾（对照-结构外 MFE p90 22.52 < 结构内 31.58）。
#   ③排序=双因子百分位和：离 60 日新高近（dd_hi60 强）+ 低波动（atr%小）。
#     证据：atrpct IC=-0.133(t=-80，前后半段同号)、dd_hi60 IC=+0.051(t=+34)；
#     高ATR 的 MFE 尾大是幻觉（MAE -11.3pp 抵消，净期望最差），低ATR 强组
#     是双排序里唯一 R20 为正的格子。
#   ④买法：gate 放行日按 ③分数降序逐只买（仓位算法与 v3 完全相同：
#     2.5ATR 距离、1% 风险预算、10% 单仓上限、现金约束自然限仓）。
#   ⑤卖侧/门控/宇宙/滑点/冷却不动，与 v3 可比。
# 已知口径差：因子研究用 400 只抽样+真实当日换手；本回测沿用 v3 的 T-1 估值表
#   外推换手（JQ 引擎侧无法逐日免费取全市场实际换手，接受该近似）。
# 验收（重要：研究窗结构内池平均 R20≈0，因子给的是相对优势）：
#   核心看**超额收益为正 + 盈亏比≥1.9**（相对 v3 的 1.221 有台阶式改善）、
#   胜率不设硬线（低波强票可能胜率>40%）、回撤<30%、趋势月显著跑赢、
#   失血段（2025-12~2026-05 类）不再靠 924 式单日脉冲撑收益。
# 证伪线：盈亏比仍 ≤1.3 且超额 ≤0 → 截面排序也救不了，问题回到"日线主板
#   长仓"这个载体本身，不再在 JQ 此骨架迭代。

# 策略收益
# 6.74%
# 策略年化收益
# 3.42%
# 超额收益
# -23.12%
# 基准收益
# 38.84%
# 阿尔法
# -0.068
# 贝塔
# 0.431
# 夏普比率
# -0.036
# 胜率
# 0.369
# 盈亏比
# 1.084
# 最大回撤 
# 13.92%
# 索提诺比率
# -0.043
# 日均超额收益
# -0.05%
# 超额收益最大回撤
# 32.74%
# 超额收益夏普比率
# -0.943
# 日胜率
# 0.477
# 盈利次数
# 118
# 亏损次数
# 202
# 信息比率
# -0.852
# 策略波动率
# 0.160
# 基准波动率
# 0.201
# 最大回撤区间
# 2024/10/08,2025/04/14
# 判定（2026-10-02）：证伪线双条触发（盈亏比 1.084≤1.3、超额 -23.12%≤0）。
#   与离线预判一致：回撤 13.92% 减半=减震器成立，但趋势段弹性被低波排序让掉，
#   右尾引擎不成立。结论=日线主板长仓个股载体无超额，此线收尾，不出 v5。
import math
from collections import defaultdict

from jqdata import *

# ── 宇宙（换手只有上限、不限市值 = 设计选择）──
TURNOVER_MAX = 12.0

# ── 结构/排序（v4②③）──
DD_N = 60                   # 离 60 日收盘新高

# ── 买侧（亏损预算比例制；止损距离 = Chandelier 距离，同源）──
RISK_PCT = 0.01             # 单笔亏损预算 = 净值 × 1%
MAX_POSITION_PCT = 0.10     # 单仓市值上限 = 净值 × 10%

# ── 卖侧（与 v3 相同：唯一 Chandelier 追踪线）──
ATR_N = 20
K_ATR = 2.5

# ── 门控（与 v3 相同：趋势中段）──
GATE_INDEX = '399317.XSHE'
GATE_LOOKBACK = 25

BARS_NEEDED = 61
CHUNK = 400
FIELDS = ('open', 'high', 'low', 'close', 'volume')


def initialize(context):
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark("000300.XSHG")
    # 沿用引擎默认比例滑点（本引擎不导出比例滑点类名，见 v3⑤教训）
    set_order_cost(OrderCost(open_tax=0, close_tax=0.0005,
                             open_commission=0.00025, close_commission=0.00025,
                             min_commission=5, close_today_commission=0),
                   type="stock")
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.entry_date = {}
    g.entry_peak = {}
    g.sold_today = set()
    run_daily(trade, time='14:55')


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


# ── 门控（与 v3①相同）────────────────────────────────────────────

def gate_open_today():
    h = attribute_history(GATE_INDEX, GATE_LOOKBACK, '1d', ['close'],
                          skip_paused=False)
    closes = [float(v) for v in h['close'].values if v == v]
    cur = get_current_data()[GATE_INDEX].last_price
    if cur and cur > 0:
        closes.append(float(cur))
    if len(closes) < GATE_LOOKBACK + 1:
        return False
    ma20 = sum(closes[-20:]) / 20.0
    ma20_prev = sum(closes[-25:-5]) / 20.0
    c = closes[-1]
    return c > ma20 and ma20 >= ma20_prev


# ── 宇宙（与 v3 相同）────────────────────────────────────────────

def mainboard_universe(dt):
    """主板 + 非 ST。"""
    df = get_all_securities('stock', date=dt)
    out = []
    for code, row in df.iterrows():
        ok = (code.startswith('60') and code.endswith('XSHG')) or \
             (code[:3] in ('000', '001', '002', '003') and code.endswith('XSHE'))
        if ok and 'ST' not in row['display_name']:
            out.append((code, row['display_name']))
    return out


def screen_universe(codes, prev_day):
    """换手 ≤ TURNOVER_MAX（估值表 T-1 服务端过滤）。异常显式告警不静默。"""
    main_set = set(codes)
    try:
        df = get_fundamentals(
            query(valuation.code, valuation.turnover_ratio)
            .filter(valuation.turnover_ratio <= TURNOVER_MAX),
            date=prev_day)
    except Exception as e:  # noqa: BLE001
        log.warning("[SCREEN] get_fundamentals 异常 %s: %s"
                    % (type(e).__name__, str(e)[:150]))
        return []
    if df is None or df.empty:
        log.warning("[SCREEN] 估值表返回空 | prev_day=%s" % prev_day)
        return []
    surv = []
    for _, r in df.iterrows():
        tr = r['turnover_ratio']
        if r['code'] in main_set and tr == tr and tr <= TURNOVER_MAX:
            surv.append((r['code'], float(tr)))
    return surv


def fetch_bars(codes):
    """批量取 61 根 OHLCV → {code: {field: list}}（引擎 history 只收单字段）。"""
    collected = defaultdict(dict)
    warned = [False]
    for f in FIELDS:
        for i in range(0, len(codes), CHUNK):
            chunk = codes[i:i + CHUNK]
            try:
                df = history(BARS_NEEDED, '1d', f, security_list=chunk)
            except Exception as e:  # noqa: BLE001
                if not warned[0]:
                    warned[0] = True
                    log.warning("[BARS] 字段 %s 批量取数失败 %s: %s"
                                % (f, type(e).__name__, str(e)[:150]))
                continue
            for code in chunk:
                if code in df.columns:
                    collected[code][f] = list(df[code].values)
    return {c: b for c, b in collected.items() if len(b) == len(FIELDS)}


def _has_nan(vals):
    for x in vals:
        if x != x:
            return True
    return False


def _sma(vals, n):
    w = vals[-n:]
    if len(w) < n or _has_nan(w):
        return None
    return sum(w) / float(n)


def _tr_series(h, l, c):
    trs = []
    for i in range(1, len(c)):
        pc = c[i - 1]
        trs.append(max(h[i] - l[i], abs(h[i] - pc), abs(l[i] - pc)))
    return trs


def _atr20(h, l, c):
    n = ATR_N + 1
    if len(c) < n or _has_nan(h[-n:]) or _has_nan(l[-n:]) or _has_nan(c[-n:]):
        return None
    atr = sum(_tr_series(h[-n:], l[-n:], c[-n:])) / float(ATR_N)
    return atr if atr > 0 else None


# ── 候选（v4②③：趋势结构内算双因子，返回 (dd_hi60, atrpct) 或 None）──

def candidate_metrics(bars):
    o = [float(x) for x in bars['open']]  # noqa: F841 保持字段齐校验语义
    h = [float(x) for x in bars['high']]
    l = [float(x) for x in bars['low']]
    c = [float(x) for x in bars['close']]
    if len(c) < BARS_NEEDED or _has_nan(c[-BARS_NEEDED:]):
        return None
    if _has_nan(h[-BARS_NEEDED:]) or _has_nan(l[-BARS_NEEDED:]):
        return None
    ma10, ma20 = _sma(c, 10), _sma(c, 20)
    if ma10 is None or ma20 is None:
        return None
    price = c[-1]
    if price <= 0:
        return None
    ma20_prev = sum(c[-21:-1]) / 20.0
    if not (ma10 > ma20 and price > ma20 and ma20 >= ma20_prev):
        return None                        # v4②：结构是唯一门槛
    hi60 = max(c[-DD_N:])
    dd_hi60 = price / hi60 - 1.0 if hi60 > 0 else None
    atr = _atr20(h, l, c)
    if dd_hi60 is None or atr is None:
        return None
    return (dd_hi60, atr / price * 100)


def rank_scores(cands):
    """v4③：双因子百分位和。dd 高者优、atr% 低者优，等权（结构值，无拟合权重）。

    cands: [(code, dd_hi60, atrpct)] → [(score, code)] 降序。
    """
    n = len(cands)
    if n == 0:
        return []
    if n == 1:
        return [(1.0, cands[0][0])]
    dd_order = {code: i for i, (code, _, _)
                in enumerate(sorted(cands, key=lambda x: -x[1]))}
    atr_order = {code: i for i, (code, _, _)
                 in enumerate(sorted(cands, key=lambda x: x[2]))}
    out = []
    for code, _, _ in cands:
        pct_dd = 1.0 - dd_order[code] / float(n - 1)
        pct_atr = 1.0 - atr_order[code] / float(n - 1)
        out.append((pct_dd + pct_atr, code))
    out.sort(key=lambda x: -x[0])
    return out


# ── 卖侧（与 v3③相同：唯一 Chandelier 追踪线）──────────────────────

def evaluate_exit(code, amount, bars):
    cd = get_current_data()
    price = float(cd[code].last_price)
    if price <= 0 or price <= cd[code].low_limit:
        return None                          # 跌停顺延
    h = [float(x) for x in bars['high']]
    l = [float(x) for x in bars['low']]
    c = [float(x) for x in bars['close']]
    atr = _atr20(h, l, c)
    if atr is None:
        return None
    peak = max(g.entry_peak.get(code, price), price)
    g.entry_peak[code] = peak
    line = peak - K_ATR * atr
    if price < line:
        return (amount,
                ["Chandelier破位（收%.2f < 峰%.2f−%.1fATR=%.2f）"
                 % (price, peak, K_ATR, line)])
    return None


# ── 主流程 ──────────────────────────────────────────────────────────

def trade(context):
    dt = context.current_dt
    dt_key = dt.strftime('%Y-%m-%d')
    cd = get_current_data()
    g.sold_today = set()

    gate_open = gate_open_today()
    log.info("GATE %s open=%s" % (dt_key, gate_open))

    for code in list(g.entry_date):
        pos = context.portfolio.positions.get(code)
        if pos is None or pos.total_amount <= 0:
            g.entry_date.pop(code, None)
            g.entry_peak.pop(code, None)

    codes_names = mainboard_universe(dt.date())
    turn_map, bars = {}, {}
    if gate_open:
        screened = screen_universe([c for c, _ in codes_names],
                                   context.previous_date)
        bars = fetch_bars(sorted(c for c, _ in screened))
        for code, tr_prev in screened:
            b = bars.get(code)
            if b is None:
                continue
            c, v = b['close'], b['volume']
            if len(c) < 2 or float(c[-2]) <= 0 or float(v[-2]) <= 0:
                continue
            tr_t = tr_prev * float(v[-1]) / float(v[-2])
            if tr_t <= TURNOVER_MAX:
                turn_map[code] = round(tr_t, 2)

    # 卖出（先卖后买；当日买入不评卖出；复用宇宙 bars）
    for code, pos in list(context.portfolio.positions.items()):
        if pos.total_amount <= 0 or g.entry_date.get(code) == dt_key:
            continue
        if cd[code].paused:
            continue
        hb = bars.get(code) or fetch_bars([code]).get(code)
        if hb is None:
            continue
        plan = evaluate_exit(code, pos.total_amount, hb)
        if not plan:
            continue
        shares, reasons = plan
        order_target(code, 0)
        g.sold_today.add(code)
        log.info("清仓 %s %s | %s" % (code, get_security_name(code),
                                      "；".join(reasons)))

    if not gate_open:
        log.info("[SCREEN] %s 未扫描（gate 关闭）" % dt_key)
        return

    # 买入（v4④：结构内候选按双因子分数降序，仓位算法与 v3 相同）
    cands = []
    for code in turn_map:
        b = bars.get(code)
        if b is None or cd[code].paused:
            continue
        m = candidate_metrics(b)
        if m is None:
            continue
        cands.append((code, m[0], m[1]))
    holdings = len([p for p in context.portfolio.positions.values()
                    if p.total_amount > 0])
    ranked = rank_scores(cands)
    n_buy = 0
    for score, code in ranked:
        if code in context.portfolio.positions or code in g.sold_today:
            continue
        b = bars[code]
        price = float(b['close'][-1])
        if price <= 0 or price >= cd[code].high_limit:
            continue                          # 涨停无法成交
        atr = _atr20([float(x) for x in b['high']],
                     [float(x) for x in b['low']],
                     [float(x) for x in b['close']])
        if atr is None:
            continue
        stop_pct = K_ATR * atr / price * 100
        value = min(context.portfolio.total_value * RISK_PCT / stop_pct * 100,
                    context.portfolio.total_value * MAX_POSITION_PCT)
        cash = context.portfolio.available_cash
        shares = min(int(value / price // 100) * 100,
                     int(cash / (price * 1.00025) // 100) * 100)
        if shares < 100:
            continue
        order(code, shares)
        g.entry_date[code] = dt_key
        g.entry_peak[code] = price
        n_buy += 1
        log.info("买入 %s %s %d份 @%.2f rank=%.2f stop=%.1f%%"
                 % (code, get_security_name(code), shares, price,
                    score, stop_pct))
    log.info("[SCREEN] %s 主板=%d 粗筛=%d 有效bars=%d 宇宙=%d 候选=%d "
             "买=%d 持仓=%d"
             % (dt_key, len(codes_names), len(screened), len(bars),
                len(turn_map), len(cands), n_buy, holdings))
