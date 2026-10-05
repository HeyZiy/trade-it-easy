# -*- coding: utf-8 -*-
# 历史 v1（原 trend.py）：严格市场门控、回踩形态信号及旧卖出规则。
# 对应逐日收益见 ../result_v1.csv；当前实验入口见 ../README.md。
# 策略收益
# -13.72%
# 策略年化收益
# -1.41%
# 超额收益
# -30.19%
# 基准收益
# 23.60%
# 阿尔法
# -0.047
# 贝塔
# 0.361
# 夏普比率
# -0.305
# 胜率
# 0.342
# 盈亏比
# 1.041
# 最大回撤 
# 47.59%
# 索提诺比率
# -0.320
# 日均超额收益
# -0.01%
# 超额收益最大回撤
# 73.97%
# 超额收益夏普比率
# -0.362
# 日胜率
# 0.489
# 盈利次数
# 790
# 亏损次数
# 1522
# 信息比率
# -0.171
# 策略波动率
# 0.177
# 基准波动率
# 0.189
# 最大回撤区间
# 2016/02/22,2021/01/28

import math
from collections import defaultdict

from jqdata import *

# ── 对拍开关 ──
LOG_UNIVERSE = False   # True = 逐日输出完整宇宙名单（单日 MX 对拍用）
DEBUG_REASON = True    # True = 逐日输出信号落选原因分布（定位用，定位后关）

# ── 宇宙条件（冻结 DEFAULT_SCREEN_KEYWORD，阈值单源镜像）──
TURNOVER_MAX = 12.0                    # 当日换手率上限（%）
FROM_60D_LOW_MAX = 80.0                # 60 日位置上限

# ── 信号核（冻结 signal_detector.py）──
MA5_HOLD_TOLERANCE = 0.995
PULLBACK_TOUCH_TOL = 1.005

# ── Cycle C1/D1（冻结 cycle_overlay.py）──
ATR_EXP_THR = 1.3

# ── 买侧（止损冻结 buy_pipeline.py；亏损预算为净值比例制）──
RISK_PCT = 0.01             # 单笔亏损预算 = 净值 × 1%（10 万 ≈ 1000 元/笔）
MIN_STOP_LOSS_PCT = 3.0

# ── 卖侧（冻结 sell_rules.py）──
VOL_RATIO_NORMAL = 2.0
DRAWDOWN_NORMAL = 5.0
SECTOR_WEAK_NORMAL = -2.0
SECTOR_TIDE_OUT = -3.0
PEAK_WINDOW = 20
MAX_HOLD_TRADING_DAYS = 16

# ── 门控（镜像 market_gate._gate_state_label，冻结）──
GATE_INDEX = '399317.XSHE'
OPEN_STATE = 'trending_up'
SIDEWAYS_BIAS = 0.015

BARS_NEEDED = 61          # 60 日回看 + 当日
CHUNK = 400               # 批量取数分片（行情）
VAL_CHUNK = 100           # 批量取数分片（估值表）


def initialize(context):
    set_option("use_real_price", True)
    set_option("avoid_future_data", True)
    set_benchmark("000300.XSHG")
    set_slippage(FixedSlippage(0))                  # 盘后按收盘价成交形态
    set_order_cost(OrderCost(open_tax=0, close_tax=0.0005,
                             open_commission=0.00025, close_commission=0.00025,
                             min_commission=5, close_today_commission=0),
                   type="stock")
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    g.hold_days = {}      # code -> 持有交易日数（停牌日不计数）
    g.entry_date = {}
    g.sector_pct = {}     # (date, sw_l1_code) -> 当日涨跌幅
    run_daily(trade, time='14:55')


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


# ── 门控（与 jq_gate_retest 同一镜像）────────────────────────────────

def gate_state_today():
    h = attribute_history(GATE_INDEX, 20, '1d', ['close'], skip_paused=False)
    closes = [float(v) for v in h['close'].values if v == v]
    cur = get_current_data()[GATE_INDEX].last_price
    if cur and cur > 0:
        closes.append(float(cur))
    if len(closes) < 20:
        return 'chaos'
    ma5 = sum(closes[-5:]) / 5.0
    ma10 = sum(closes[-10:]) / 10.0
    ma20 = sum(closes[-20:]) / 20.0
    c = closes[-1]
    if ma5 < ma10 < ma20 and c < ma10:
        return 'trending_down'
    if ma5 > ma10 > ma20 and c > ma10:
        return 'trending_up'
    if abs(c - ma20) / ma20 < SIDEWAYS_BIAS:
        return 'sideways'
    if c > ma20:
        return 'weak_up'
    return 'chaos'


# ── 宇宙（妙想条件 point-in-time 重建）──────────────────────────────

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
    """总市值 30~500 亿 + 换手 3~12%（估值表 T-1 point-in-time 粗筛，日内外推见头注 4）。

    回测取估值走 get_fundamentals(query(valuation...), date=T-1)。
    Returns: [(code, turnover_prev)]；首例异常/空表显式告警不静默。
    """
    surv = []
    warned = [False]

    def warn_once(msg):
        if not warned[0]:
            warned[0] = True
            log.warning("[SCREEN] %s" % msg)

    for i in range(0, len(codes), VAL_CHUNK):
        chunk = codes[i:i + VAL_CHUNK]
        try:
            df = get_fundamentals(
                query(valuation.code, valuation.turnover_ratio)
                .filter(valuation.code.in_(chunk)),
                date=prev_day)
        except Exception as e:  # noqa: BLE001 首例显式告警，其余静默
            warn_once("get_fundamentals 异常 %s: %s | chunk 示例: %s"
                      % (type(e).__name__, str(e)[:150], chunk[:3]))
            continue
        if df is None or df.empty:
            warn_once("估值表返回空 | prev_day=%s | chunk 示例: %s"
                      % (prev_day, chunk[:3]))
            continue
        if i == 0:
            log.info("[SCREEN] 估值表首片 %d 行，列: %s" % (len(df), list(df.columns)))
        for _, r in df.iterrows():
            tr = r['turnover_ratio']
            if tr == tr and tr <= TURNOVER_MAX:
                surv.append((r['code'], float(tr)))
    return surv


def fetch_bars(codes):
    """批量取 61 根 OHLCV → {code: {field: list}}。"""
    out = {}
    for i in range(0, len(codes), CHUNK):
        chunk = codes[i:i + CHUNK]
        try:
            o = history(BARS_NEEDED, '1d', 'open', security_list=chunk)
            h = history(BARS_NEEDED, '1d', 'high', security_list=chunk)
            l = history(BARS_NEEDED, '1d', 'low', security_list=chunk)
            c = history(BARS_NEEDED, '1d', 'close', security_list=chunk)
            v = history(BARS_NEEDED, '1d', 'volume', security_list=chunk)
        except Exception:
            continue
        for code in chunk:
            if code not in c.columns:
                continue
            out[code] = {'open': list(o[code].values), 'high': list(h[code].values),
                         'low': list(l[code].values), 'close': list(c[code].values),
                         'volume': list(v[code].values)}
    return out


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
    if not (ma5 > ma10 > ma20):
        if tally is not None:
            tally['非多头排列'] += 1
        return None
    if tally is not None:
        tally['多头排列'] += 1

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


# ── 卖侧（镜像 sell_rules；板块两条用申万一级指数近似）────────────────

def sector_pct_today(code, dt_key):
    """所属申万一级行业指数当日涨跌幅（%）；取不到返回 None（规则跳过）。"""
    try:
        ind = get_industry(code)
        sw = ind.get('sw_l1') if isinstance(ind, dict) else None
        if not sw:
            return None
        idx = sw['industry_code'] + '.XSHG'
    except Exception:
        return None
    key = (dt_key, idx)
    if key in g.sector_pct:
        return g.sector_pct[key]
    try:
        h = history(2, '1d', 'close', security_list=[idx])
        prev, cur = float(h[idx].values[-2]), float(h[idx].values[-1])
        pct = (cur / prev - 1) * 100 if prev > 0 else None
    except Exception:
        pct = None
    g.sector_pct[key] = pct
    return pct


def evaluate_exit(code, name, amount, bars, hold_days, dt_key):
    """卖出判定 → (action, shares, reasons)：清仓 > 减半，先到先出。"""
    cd = get_current_data()
    price = float(cd[code].last_price)
    if price <= 0 or price <= cd[code].low_limit:
        return None                          # 跌停顺延
    c = [float(x) for x in bars['close']]
    v = [float(x) for x in bars['volume']]
    if len(c) < PEAK_WINDOW + 1:
        return None
    ma5, ma10 = _sma(c, 5), _sma(c, 10)
    if ma5 is None or ma10 is None:
        return None
    p5 = [x for x in v[-6:-1] if x == x]
    vr = (v[-1] / (sum(p5) / len(p5))) if p5 and sum(p5) > 0 else 1.0
    stage_high = max(c[-PEAK_WINDOW - 1:-1])
    platform_low = min(c[-PEAK_WINDOW - 1:-1])
    spct = sector_pct_today(code, dt_key)

    reduce_r, clear_r = [], []
    if vr >= VOL_RATIO_NORMAL and price < ma5:
        reduce_r.append(f"放量跌破5日线（量比{vr:.2f}）")
    if (price - stage_high) / stage_high * 100 <= -DRAWDOWN_NORMAL:
        reduce_r.append(f"阶段高点回撤≥{DRAWDOWN_NORMAL:g}%")
    if spct is not None and spct <= SECTOR_WEAK_NORMAL:
        reduce_r.append(f"板块走弱（申万一级 {spct:+.1f}%）")
    if vr >= VOL_RATIO_NORMAL and price < ma10:
        clear_r.append(f"放量跌破10日线（量比{vr:.2f}）")
    if price < platform_low:
        clear_r.append("跌破关键平台（近20日最低收盘）")
    if spct is not None and spct <= SECTOR_TIDE_OUT:
        clear_r.append(f"主线退潮（申万一级 {spct:+.1f}%）")
    if hold_days >= MAX_HOLD_TRADING_DAYS:
        clear_r.append(f"持满{MAX_HOLD_TRADING_DAYS}个交易日到期")

    if clear_r:
        return ('clear', amount, clear_r)
    if reduce_r:
        half = int(amount // 2 // 100) * 100
        if half >= 100:
            return ('reduce_half', half, reduce_r)
    return None


# ── 主流程 ──────────────────────────────────────────────────────────

def trade(context):
    dt = context.current_dt
    dt_key = dt.strftime('%Y-%m-%d')
    cd = get_current_data()

    state = gate_state_today()
    log.info("GATE %s state=%s" % (dt_key, state))

    # 持有天数推进（停牌日不计数）
    for code in list(g.hold_days):
        pos = context.portfolio.positions.get(code)
        if pos is None or pos.total_amount <= 0:
            g.hold_days.pop(code, None)
            g.entry_date.pop(code, None)
        elif not cd[code].paused and g.entry_date.get(code) != dt_key:
            g.hold_days[code] += 1

    # 宇宙筛选：估值表 T-1 粗筛 → 价/量比外推到当日复验两个条件
    # （只在 gate 放行态做——信号只在 trending_up 被消费，省一半算力）
    codes_names = mainboard_universe(dt.date())
    turn_map, bars = {}, {}
    if state == OPEN_STATE:
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

    # 卖出（先卖后买；当日买入不评卖出）
    for code, pos in list(context.portfolio.positions.items()):
        if pos.total_amount <= 0 or g.entry_date.get(code) == dt_key:
            continue
        if cd[code].paused:
            continue
        hb = fetch_bars([code]).get(code)
        if hb is None:
            continue
        plan = evaluate_exit(code, get_security_name(code),
                             pos.total_amount, hb,
                             g.hold_days.get(code, 0), dt_key)
        if not plan:
            continue
        action, shares, reasons = plan
        if action == 'clear':
            order_target(code, 0)
            log.info("清仓 %s %s | %s" % (code, get_security_name(code),
                                          "；".join(reasons)))
        else:
            order(code, -shares)
            log.info("减半 %s %s %d股 | %s" % (code, get_security_name(code),
                                               shares, "；".join(reasons)))

    # 买入（gate 放行 + score≥60 + 涨停跳过 + 现金约束）
    if state != OPEN_STATE:
        log.info("[SCREEN] %s 未扫描（gate=%s）" % (dt_key, state))
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
        if code in context.portfolio.positions:
            continue
        b = bars[code]
        price = float(b['close'][-1])
        if price <= 0 or price >= cd[code].high_limit:
            continue                          # 涨停无法成交
        ma10 = _sma([float(x) for x in b['close']], 10)
        stop = max((price - ma10) / price * 100, MIN_STOP_LOSS_PCT)
        value = context.portfolio.total_value * RISK_PCT / stop * 100
        cash = context.portfolio.available_cash
        shares = min(int(value / price // 100) * 100,
                     int(cash / (price * 1.00025) // 100) * 100)
        if shares < 100:
            continue
        order(code, shares)
        g.hold_days[code] = 0
        g.entry_date[code] = dt_key
        log.info("买入 %s %s %d份 @%.2f %s score=%d"
                 % (code, get_security_name(code), shares, price,
                    sig_type, score))
    log.info("[SCREEN] %s 主板=%d 粗筛=%d 有效bars=%d 宇宙=%d 信号=%d 持仓=%d"
             % (dt_key, len(codes_names), len(screened), n_bars,
                len(turn_map), len(signals),
                len(context.portfolio.positions)))
