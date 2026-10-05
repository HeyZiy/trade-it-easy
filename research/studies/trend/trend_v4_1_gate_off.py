# -*- coding: utf-8 -*-
# trend_v4.1_gate_off：以同目录 trend_v4_1_gate_on.py 为基线的市场门控消融实验。
# 研究状态（2026-10-03）：暂停；运行慢且阶段明显跑输，当前不要求补跑或新增对照。
#   截图悬停日 2019-09-20：策略累计收益 -46.31%，沪深300基准 +5.48%，
#   累计收益差 -51.79 个百分点。仅为该日阶段值，不是完整回测最终成绩。
#   实际回测终止日及最终指标未确认；尚无对应逐日收益 CSV。
# 整文件粘贴聚宽，分钟回测；原计划区间 2016-01-04 至 2026-09-01。
# 唯一策略变量：USE_MARKET_GATE=False，所有交易日执行原宇宙扫描和买入流程。
# 个股趋势结构、双因子排名、T-1换手/日线、14:55执行参考价、仓位算法、
# 追踪止损、同日卖出冷却、基准及费率全部沿用v4.1。
# 关闭时不请求门控指数行情；USE_MARKET_GATE=True可恢复原门控作对照。
# 若恢复对照研究，固定相同区间、初始资金、频率和平台设置；本文件尚无完整成绩。
# 届时可导出日收益曲线到 result_v4_1_gate_off.csv，用 research.tools.market_regimes 诊断。
# 以下为基线说明（修正点和预设验收标准沿用）：
# trend_v4.1：v4 的基线修正版；若恢复研究，须重新回测，不能沿用原 v4 历史成绩。
# 原 v4 代码、研究依据与历史回测结果保存在同目录 trend_v4.py。
# 策略逻辑沿用 v4：主板非 ST、市场 gate、个股趋势结构、双因子百分位和
# （离 60 日收盘新高近 + 低 ATR%）、2.5ATR 距离、1% 风险预算、
# 10% 单仓上限、现金约束、14:55 价格追踪止损及当日卖出冷却。
# 本版修正：
#   ①直接使用 T-1 估值表 turnover_ratio（≤12%）筛选，不再乘历史成交量比。
#     日频 history 截至 T-1，因此历史量比无法外推得到 T 日换手。
#   ②信号与 ATR 仍使用截至 T-1 的日线；涨停判断、仓位股数、entry_peak
#     初值与买入日志统一使用 14:55 的 last_price，跳过非有限或非正价格。
#   ③退出日志称“14:55追踪止损”，峰值仍按每日 14:55 价格更新，卖侧公式不变。
#     last_price 是下单时参考价，实际成交由引擎处理。
# 验收继续采用 v4 预设标准：超额收益 > 0、盈亏比 ≥1.9、回撤 <30%；
#   盈亏比 ≤1.3 且超额 ≤0 时触发该基线的证伪线。
# 本版尚无完整回测结果；原 v4 的历史表现不代表本版结果。
import math
from collections import defaultdict

from jqdata import *

# ── 宇宙（换手只有上限、不限市值 = 设计选择）──
TURNOVER_MAX = 12.0

# ── 结构/排序（v4②③）──
DD_N = 60                   # 离 60 日收盘新高

# ── 买侧（亏损预算比例制；止损距离 = 追踪线 ATR 距离，同源）──
RISK_PCT = 0.01             # 单笔亏损预算 = 净值 × 1%
MAX_POSITION_PCT = 0.10     # 单仓市值上限 = 净值 × 10%

# ── 卖侧（沿用 v4：每日 14:55 价格峰值减 2.5ATR）──
ATR_N = 20
K_ATR = 2.5

# ── 门控（与 v3 相同：趋势中段）──
USE_MARKET_GATE = False      # 本次实验：关闭总市场门控
GATE_INDEX = '399317.XSHE'
GATE_LOOKBACK = 25

BARS_NEEDED = 61
CHUNK = 400
FIELDS = ('open', 'high', 'low', 'close')


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
    if not USE_MARKET_GATE:
        return True
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
    """T-1 实际换手 ≤ TURNOVER_MAX；该换手值直接用于 turn_map。"""
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
    """批量取截至 T-1 的 61 根 OHLC → {code: {field: list}}。"""
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


# ── 卖侧（沿用 v4：每日 14:55 价格峰值减 2.5ATR）──────────────────

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
                ["14:55追踪止损（价%.2f < 峰%.2f−%.1fATR=%.2f）"
                 % (price, peak, K_ATR, line)])
    return None


# ── 主流程 ──────────────────────────────────────────────────────────

def trade(context):
    dt = context.current_dt
    dt_key = dt.strftime('%Y-%m-%d')
    cd = get_current_data()
    g.sold_today = set()

    gate_open = gate_open_today()
    log.info("GATE %s enabled=%s open=%s" % (dt_key, USE_MARKET_GATE, gate_open))

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
            turn_map[code] = tr_prev

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
        price = float(cd[code].last_price)
        if not math.isfinite(price) or price <= 0 or price >= cd[code].high_limit:
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
