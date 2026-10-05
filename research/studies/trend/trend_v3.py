# -*- coding: utf-8 -*-
# trend_v3 = trend_v2（硬止损+仓位上限消融版）+ 趋势自洽改造，版本链 v1→v2→v3：
#   ①门控放宽：指数严格多头排列 → close>MA20 且 MA20 不降（趋势中段放行）
#   ②个股排列放宽：ma5>ma10>ma20 → ma10>ma20 且价>MA20 且 MA20 不降
#   ③卖侧唯一规则：Chandelier 追踪线（收盘 < 入场以来最高收盘 − 2.5×ATR20
#     清仓）。删除：入场锚定硬止损（被 Chandelier 建仓瞬间的形态包含）、放量
#     破5/10日线、阶段高点回撤、板块走弱/退潮（板块暴跌而个股未破线=相对强势，
#     趋势逻辑该留不该砍；且极端踩踏板块齐跌停，任何卖法都卖不出）、平台破位、
#     16日时间止损。仓位距离同源：stop% = 2.5×ATR20/price。
#   ④宇宙注释修正：不限市值、换手只有上限 12% 是设计选择（趋势两端市值都有
#     右尾；换手下限会排掉启动期低换手票），v1/v2 docstring 为过期注释。
#   ⑤滑点：v1 的 FixedSlippage(0) 取消，改用引擎默认价格比例滑点（约千1.25/边；
#     本引擎不导出比例滑点类名，只能靠默认，跑完在回测详情里核对滑点设置）
#   ⑥当日卖出冷却：被清仓的票同日不再买回（v1 起先卖后买流程的漏洞）。
#   ⑦提速包（零语义改动）：估值表服务端过滤换手上限（31 次/日 → 1 次）；
#     卖出优先复用宇宙 bars；删退潮后 get_industry/申万指数依赖整块移除。
#     （history 多字段合一本引擎不支持：field 只收单字段名，实测 AssertionError）
# 已知风险：无时间止损 → 不死不活的仓位可能长期占资金，先看回测持仓天数
#   分布再决定是否打"死钱清仓"补丁，保持归因干净。
# 验收（对照 v1）：盈亏比≥2.0、胜率≥28%、最大回撤<30%、2016-18 >-20%、
#   2019 与 2024-25 趋势年显著为正。证伪线：盈亏比仍 ≈1 → 问题在票池。
# v1 结果存档（2016-01~2026-09，基准沪深300）：
#   收益 -13.72% / 年化 -1.41% / 超额 -30.19% / 夏普 -0.305
#   胜率 0.342 / 盈亏比 1.041 / 最大回撤 47.59% / 盈利790 亏损1522

# 策略收益
# 15.58%
# 策略年化收益
# 7.77%
# 超额收益
# -16.75%
# 基准收益
# 38.84%
# 阿尔法
# -0.044
# 贝塔
# 0.564
# 夏普比率
# 0.171
# 胜率
# 0.347
# 盈亏比
# 1.221
# 最大回撤 
# 26.89%
# 索提诺比率
# 0.217
# 日均超额收益
# -0.03%
# 超额收益最大回撤
# 34.50%
# 超额收益夏普比率
# -0.627
# 日胜率
# 0.488
# 盈利次数
# 109
# 亏损次数
# 205
# 信息比率
# -0.516
# 策略波动率
# 0.220
# 基准波动率
# 0.201
# 最大回撤区间
# 2026/01/29,2026/07/21
import math
from collections import defaultdict

from jqdata import *

# ── 对拍开关 ──
LOG_UNIVERSE = False   # True = 逐日输出完整宇宙名单（单日 MX 对拍用）
DEBUG_REASON = True    # True = 逐日输出信号落选原因分布（定位用，定位后关）

# ── 宇宙条件（换手只有上限、不限市值 = 设计选择，见头注④）──
TURNOVER_MAX = 12.0                    # 当日换手率上限（%）
FROM_60D_LOW_MAX = 80.0                # 60 日位置上限

# ── 信号核（冻结 signal_detector.py；排列条件 v3 放宽见②）──
MA5_HOLD_TOLERANCE = 0.995
PULLBACK_TOUCH_TOL = 1.005

# ── Cycle C1/D1（冻结 cycle_overlay.py）──
ATR_EXP_THR = 1.3

# ── 买侧（亏损预算为净值比例制；止损距离 = Chandelier 距离，同源）──
RISK_PCT = 0.01             # 单笔亏损预算 = 净值 × 1%
MAX_POSITION_PCT = 0.10     # 单仓市值上限 = 净值 × 10%

# ── 卖侧（v3：唯一 Chandelier 追踪线）──
ATR_N = 20                  # ATR 均值窗口
K_ATR = 2.5                 # Chandelier 倍数（结构值，非拟合）

# ── 门控（v3：趋势中段）──
GATE_INDEX = '399317.XSHE'
GATE_LOOKBACK = 25          # MA20 + 5 日前的 MA20

BARS_NEEDED = 61          # 60 日回看 + 当日
CHUNK = 400               # 批量取数分片（行情）
FIELDS = ('open', 'high', 'low', 'close', 'volume')


def initialize(context):
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark("000300.XSHG")
    # v3⑤：不显式设滑点，沿用引擎默认（股票价格比例制，约千1.245/边）。
    # 本引擎只导出 FixedSlippage，比例类类名不可用，故用默认值替代。
    set_order_cost(OrderCost(open_tax=0, close_tax=0.0005,
                             open_commission=0.00025, close_commission=0.00025,
                             min_commission=5, close_today_commission=0),
                   type="stock")
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.entry_date = {}       # code -> 建仓日（当日不评卖出）
    g.entry_peak = {}       # code -> 入场以来最高收盘（Chandelier 锚）
    g.sold_today = set()    # v3⑥：当日被清仓 → 冷却不买回
    run_daily(trade, time='14:55')


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


# ── 门控（v3①：close>MA20 且 MA20 较 5 日前不降）────────────────────

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


# ── 宇宙（妙想条件 point-in-time 重建；注释已按实际语义修正，见头注④）──

def mainboard_universe(dt):
    """主板 + 非 ST（冻结选股语句末句）。"""
    df = get_all_securities('stock', date=dt)
    out = []
    for code, row in df.iterrows():
        ok = (code.startswith('60') and code.endswith('XSHG')) or \
             (code[:3] in ('000', '001', '002', '003') and code.endswith('XSHE'))
        if ok and 'ST' not in row['display_name']:
            out.append((code, row['display_name']))
    return out


def screen_universe(codes, prev_day):
    """换手 ≤ TURNOVER_MAX（估值表 T-1 point-in-time；v3⑦服务端过滤替代逐片查询）。

    不限市值、不设换手下限：趋势策略两端市值都有右尾，30~500 亿带会两头剪掉；
    换手下限会排掉启动期低换手票。上限只防高位爆量出货。
    服务端 filter 后与主板集合本地取交集，语义与原逐 code 分片查询等价。
    Returns: [(code, turnover_prev)]；异常/空表显式告警不静默。
    """
    main_set = set(codes)
    try:
        df = get_fundamentals(
            query(valuation.code, valuation.turnover_ratio)
            .filter(valuation.turnover_ratio <= TURNOVER_MAX),
            date=prev_day)
    except Exception as e:  # noqa: BLE001 显式告警，返回空宇宙（当日不买入）
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
    """批量取 61 根 OHLCV → {code: {field: list}}。

    本引擎 history 的 field 只收单个字段名（多字段列表会 AssertionError），
    只能逐字段调用；security_list 按 CHUNK 分片防超限。失败显式告警一次，
    不静默（v3 首轮 bars=0 的教训）。
    """
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
    """近 ATR_N 日 TR 均值；不足或含 NaN → None。"""
    n = ATR_N + 1
    if len(c) < n or _has_nan(h[-n:]) or _has_nan(l[-n:]) or _has_nan(c[-n:]):
        return None
    atr = sum(_tr_series(h[-n:], l[-n:], c[-n:])) / float(ATR_N)
    return atr if atr > 0 else None


# ── 信号评分（镜像 _compute_signal_score，只移植买侧用到的两支）───────

def _position_penalty(pos):
    if pos <= 25:
        return 0.0
    if pos >= 75:
        return 20.0
    return (pos - 25) * 0.4


def _score_ma5_family(bias, vr, pct, tr, cp, ls, above_count, pos, cap):
    if 0.0 <= bias <= 2.0:
        bias_score = 25 - abs(bias - 0.5) * 10
    elif -1.0 <= bias < 0.0:
        bias_score = 20 - abs(bias) * 5
    else:
        bias_score = max(0, 10 - abs(bias - 2.0) * 5)
    if 0.5 <= vr <= 0.9:
        vol_score = 20 - abs(vr - 0.7) * 25
    elif 0.4 <= vr < 0.5:
        vol_score = 12
    elif 0.9 < vr <= 1.05:
        vol_score = 10 - (vr - 0.9) * 66
    else:
        vol_score = max(0, 5)
    if -2.0 <= pct <= 1.0:
        pct_score = 15 - abs(pct) * 3
    elif -4.0 <= pct < -2.0 or 1.0 < pct <= 4.0:
        pct_score = 10 - abs(pct - 1.0) * 2
    else:
        pct_score = max(0, 5)
    if 4.0 <= tr <= 7.0:
        tr_score = 15
    elif 3.0 <= tr < 4.0 or 7.0 < tr <= 10.0:
        tr_score = 10
    else:
        tr_score = max(0, 5)
    id_score = 15 if (cp > 0.6 and ls > 0.2) else (10 if (cp > 0.4 and ls > 0.1) else 5)
    score = bias_score + vol_score + pct_score + tr_score + id_score + min(10, above_count * 2)
    score -= _position_penalty(pos)
    return min(cap, max(50, score))


def _score_ma10(bias, vr, pct, tr, cp, ls, touches, pos):
    if -3.0 <= bias <= 0:
        bias_score = 15 - abs(bias + 1.0) * 3
    else:
        bias_score = max(0, 8 - abs(bias) * 2)
    vol_score = (20 - abs(vr - 0.6) * 20) if 0.4 <= vr <= 0.9 else max(0, 8)
    if -4.0 <= pct <= 1.0:
        pct_score = 15 - abs(pct + 1.0) * 2
    else:
        pct_score = max(0, 5)
    if 3.0 <= tr <= 8.0:
        tr_score = 20 - abs(tr - 5.0) * 3
    else:
        tr_score = max(0, 8)
    id_score = 10 if (cp > 0.4 and ls > 0.1) else 5
    score = bias_score + vol_score + pct_score + tr_score + id_score + (15 if touches else 0)
    score -= _position_penalty(pos)
    return min(85, max(30, score))


def detect_signal(code, bars, turnover, tally=None):
    """单只信号判定（镜像 detect_pullback_signals + C1/D1，返回 (type, score) 或 None）。

    v3②：趋势结构 = ma10>ma20 且 价>ma20 且 MA20 不降（原严格 ma5>ma10>ma20
    只允许浅回撤入场，中段深回调被排除）。评分与 C1/D1 不动。
    tally 传入 dict 时逐条件落选计数（DEBUG_REASON 诊断用），判定逻辑不受影响。
    """
    o, h, l, c, v = (bars['open'], bars['high'], bars['low'],
                     bars['close'], bars['volume'])
    c = [float(x) for x in c]
    h = [float(x) for x in h]
    l = [float(x) for x in l]
    if len(c) < BARS_NEEDED or _has_nan(c[-BARS_NEEDED:]):
        if tally is not None:
            if tally['_样本'] < 3:
                tail = c[-BARS_NEEDED:]
                log.info("[BARS] %s fired: len=%d BARS_NEEDED=%r lenfail=%s anyfail=%s "
                         "c[:3]=%s nan_total=%d nan_tail61=%d last_nan=%s "
                         "h_nan_tail=%d l_nan_tail=%d v_nan_tail=%d"
                         % (code, len(c), BARS_NEEDED,
                            len(c) < BARS_NEEDED,
                            _has_nan(c[-BARS_NEEDED:]),
                            [c[0], c[1], c[2]],
                            sum(1 for x in c if x != x),
                            sum(1 for x in tail if x != x), c[-1] != c[-1],
                            sum(1 for x in h[-BARS_NEEDED:] if x != x),
                            sum(1 for x in l[-BARS_NEEDED:] if x != x),
                            sum(1 for x in [float(x) for x in v[-BARS_NEEDED:]]
                                if x != x)))
            tally['_样本'] += 1
            tally['bars不足/NaN'] += 1
        return None
    ma5, ma10, ma20 = _sma(c, 5), _sma(c, 10), _sma(c, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        if tally is not None:
            tally['bars不足/NaN'] += 1
        return None
    price = c[-1]
    if price <= 0:
        if tally is not None:
            tally['bars不足/NaN'] += 1
        return None
    ma20_prev = sum(c[-21:-1]) / 20.0
    if not (ma10 > ma20 and price > ma20 and ma20 >= ma20_prev):
        if tally is not None:
            tally['非趋势结构'] += 1
        return None
    if tally is not None:
        tally['趋势结构'] += 1

    bias5 = (price - ma5) / ma5 * 100
    bias20 = (price - ma20) / ma20 * 100
    pct = (price / c[-2] - 1) * 100 if c[-2] > 0 else 0.0
    holds = price >= ma5 * MA5_HOLD_TOLERANCE
    rng = h[-1] - l[-1]
    cp = (price - l[-1]) / rng if rng > 0 else 0.5
    ls = (min(o[-1], price) - l[-1]) / rng if rng > 0 else 0.1
    low60 = min(c[-60:])
    pos_gain = (price - low60) / low60 * 100 if low60 > 0 else 0.0

    # 量比 = 当日量 ÷ 前 5 日均量（不含当日；indicators.volume_ratio 口径）
    p5 = v[-6:-1]
    p5 = [float(x) for x in p5 if x == x]
    vr = (float(v[-1]) / (sum(p5) / len(p5))) if p5 and sum(p5) > 0 else 1.0

    above = 0
    for i in range(len(c) - 6, len(c) - 1):
        m5i = sum(c[i - 4:i + 1]) / 5.0
        if c[i] > m5i:
            above += 1

    # C1：ATR 扩张（简单滚动均值，与 indicators.atr 同口径）
    trs = _tr_series(h, l, c)
    if len(trs) >= 20:
        a5 = sum(trs[-5:]) / 5.0
        a20 = sum(trs[-20:]) / 20.0
        if a20 > 0 and a5 / a20 > ATR_EXP_THR:
            if tally is not None:
                tally['C1剔除'] += 1
            return None                      # C1 剔除
    # D1：MA5 较昨日向下
    ma5_prev = _sma(c[:-1], 5)
    if ma5_prev is not None and ma5 < ma5_prev:
        if tally is not None:
            tally['D1剔除'] += 1
        return None                          # D1 剔除

    overext = pos_gain >= FROM_60D_LOW_MAX
    if tally is not None:
        for name, ok in (('未守MA5', holds or None),
                         ('60日位置过深', not overext or None)):
            if not ok:
                tally[name] += 1

    sig = None
    setup = holds and not overext
    if setup:
        shape = pct < 0 or l[-1] <= ma5 * PULLBACK_TOUCH_TOL
        if shape:
            sig = ('pullback_ma5', _score_ma5_family(
                bias5, vr, pct, turnover, cp, ls, above, pos_gain, 95))
        else:
            sig = ('near_ma5', _score_ma5_family(
                bias5, vr, pct, turnover, cp, ls, above, pos_gain, 79))
    elif (l[-1] <= ma10 * 1.01 and price >= ma10
          and not overext and not holds):
        sig = ('pullback_ma10', _score_ma10(
            bias5, vr, pct, turnover, cp, ls, True, pos_gain))
    if tally is not None:
        tally['信号'] += 1 if sig else 0
    return sig


# ── 卖侧（v3③：唯一 Chandelier 追踪线，退潮/板块依赖已删）────────────

def evaluate_exit(code, amount, bars):
    """v3 卖出判定（单规则）→ (shares, reasons) 或 None。

    峰值 = 入场以来最高收盘（含当日，先升峰值再比线）；
    收盘 < 峰值 − 2.5×ATR20 → 清仓。跌停顺延。
    """
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
    g.sold_today = set()                     # v3⑥：每日重置卖出冷却

    gate_open = gate_open_today()
    log.info("GATE %s open=%s" % (dt_key, gate_open))

    # 清理已离场持仓的跟踪状态
    for code in list(g.entry_date):
        pos = context.portfolio.positions.get(code)
        if pos is None or pos.total_amount <= 0:
            g.entry_date.pop(code, None)
            g.entry_peak.pop(code, None)

    # 宇宙筛选：估值表 T-1 粗筛 → 价/量比外推到当日复验两个条件
    # （只在 gate 放行态做——信号只在放行态被消费，省一半算力）
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

        if LOG_UNIVERSE:
            log.info("UNIVERSE %s n=%d %s" % (dt_key, len(turn_map),
                                              ",".join(sorted(turn_map))))

    # 卖出（先卖后买；当日买入不评卖出；v3⑦优先复用宇宙 bars）
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

    # 买入（gate 放行 + score≥60 + 涨停跳过 + 卖出冷却 + 现金约束）
    if not gate_open:
        log.info("[SCREEN] %s 未扫描（gate 关闭）" % dt_key)
        return
    signals = []
    tally = defaultdict(int) if DEBUG_REASON else None
    for code, tr in turn_map.items():
        b = bars.get(code)
        if b is None or cd[code].paused:
            if tally is not None and cd[code].paused:
                tally['停牌'] += 1
            continue
        sig = detect_signal(code, b, tr, tally)
        if sig:
            signals.append((sig[1], code, sig[0]))
    if tally is not None:
        dist = " ".join("%s=%d" % (k, n) for k, n in sorted(tally.items()))
        log.info("[REASON] %s %s" % (dt_key, dist))
    signals.sort(key=lambda x: -x[0])
    n_bars = len(bars)
    for score, code, sig_type in signals:
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
        stop_pct = K_ATR * atr / price * 100  # 与 Chandelier 出场线同源
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
        log.info("买入 %s %s %d份 @%.2f %s score=%d stop=%.1f%%"
                 % (code, get_security_name(code), shares, price,
                    sig_type, score, stop_pct))
    log.info("[SCREEN] %s 主板=%d 粗筛=%d 有效bars=%d 宇宙=%d 信号=%d 持仓=%d"
             % (dt_key, len(codes_names), len(screened), n_bars,
                len(turn_map), len(signals),
                len(context.portfolio.positions)))
