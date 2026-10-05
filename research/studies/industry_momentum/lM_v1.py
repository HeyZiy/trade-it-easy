# -*- coding: utf-8 -*-
# rotation_env 平台重测——l2_etfself 已定版 + gate 三态门控（2026-09-30）
# 引擎 = research/rotation_l2_JQ/l2_etfself.py 已定版（2026-09-29 平台跑分通过：
# +247.07%/年化 18.9%），一字未改；本脚本只叠加环境门控层，用于重验
# rotation_env 的方向性裁决（gate 轴优于 baseline，gate_down_flat 最优）。
# 旧 rotation_env 底稿为不复权 ETF bars，绝对数字已作废（见该线 README）。
#
# 镜像纪律（冻结，不跟生产）：
#   五态判定 _gate_state_label 镜像 src/market_state/market_gate.py（2026-09-30 冻结）；
#   放行/清仓 policy 镜像 src/etf/industry_momentum.py::satellite_gate_policy（同日冻结）。
#   对账件 research/rotation_env/check_gate_parity.py：平台 GATE 日志贴入后与
#   生产 gate_state_series 逐日比对，不许带病采信跑分。
#
# 跑分方式：改 GATE_MODE 常量，同一文件分 4 次回测（其余参数一律不动）——
#   baseline / gate_block / gate_flat / gate_down_flat
# 已知近似：
#   - 指数当日收盘用 14:55 的 last_price 近似（与生产快照补当日 bar 的尾盘口径一致）；
#   - 门控清仓先于常规卖出、block_new 跳过买入（与旧 rotation_env 挂点语义一致）。

import math
from jqdata import *

# ── 跑分开关：每次回测只改这一个常量 ──
GATE_MODE = 'baseline'   # baseline / gate_block / gate_flat / gate_down_flat

GATE_INDEX = '399317.XSHE'     # 国证A指（生产 gate 底层指数）
OPEN_STATE = 'trending_up'     # 唯一放行态（生产 satellite_gate_policy / CAN_OPEN_STATES）

UNIVERSE = [
    ('159309.XSHE', '油气ETF汇添富'),
    ('159530.XSHE', '机器人ETF易方达'),
    ('159611.XSHE', '电力ETF广发'),
    ('159667.XSHE', '工业母机ETF国泰'),
    ('159707.XSHE', '地产ETF华宝'),
    ('159732.XSHE', '消费电子ETF华夏'),
    ('159755.XSHE', '电池ETF广发'),
    ('159766.XSHE', '旅游ETF富国'),
    ('159819.XSHE', '人工智能ETF易方达'),
    ('159852.XSHE', '软件ETF嘉实'),
    ('159865.XSHE', '养殖ETF国泰'),
    ('159869.XSHE', '游戏ETF华夏'),
    ('159870.XSHE', '化工ETF鹏华'),
    ('159883.XSHE', '医疗器械ETF永赢'),
    ('159992.XSHE', '创新药ETF银华'),
    ('159996.XSHE', '家电ETF国泰'),
    ('512200.XSHG', '房地产ETF南方'),
    ('512400.XSHG', '有色金属ETF南方'),
    ('512480.XSHG', '半导体ETF国联安'),
    ('512660.XSHG', '军工ETF国泰'),
    ('512690.XSHG', '酒ETF鹏华'),
    ('512880.XSHG', '证券ETF国泰'),
    ('515020.XSHG', '银行ETF华夏'),
    ('515210.XSHG', '钢铁ETF国泰'),
    ('515220.XSHG', '煤炭ETF国泰'),
    ('515260.XSHG', '电子ETF华宝'),
    ('515790.XSHG', '光伏ETF华泰柏瑞'),
    ('515880.XSHG', '通信ETF国泰'),
    ('516620.XSHG', '影视ETF国泰'),
    ('517520.XSHG', '黄金股ETF永赢'),
    ('560280.XSHG', '工程机械ETF广发'),
    ('562800.XSHG', '稀有金属ETF嘉实'),
    ('588160.XSHG', '科创新材料ETF南方'),
    ('588830.XSHG', '科创新能源ETF鹏华'),
]

TOPN = 3
EXIT_PCT = 0.40
CROWD_LOOKBACK = 250
CROWD_MIN_OBS = 60
CROWD_MAX = 90.0


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
    g.codes = [c for c, _ in UNIVERSE]
    run_daily(run_rotation, time='14:55')


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


def pct_in_window(vals, lookback):
    """自身近 N 日窗口内分位（0-100），不足 60 个观测返回 None（镜像 _pct_in_window）。"""
    w = vals[-lookback:]
    if len(w) < CROWD_MIN_OBS:
        return None
    last = w[-1]
    return round(sum(1 for v in w if v < last) / float(len(w)) * 100, 1)


# ── gate 门控层（唯一新增）──────────────────────────────────────────

def gate_state_today():
    """当日五态判定（尾盘口径，镜像 market_gate._gate_state_label，判定优先级同序）。

    序列 = 前 20 根日线收盘 + 当日 last_price（≈收盘）；不足 20 根判 chaos
    （与生产 fail 语义一致）。
    """
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
    if abs(c - ma20) / ma20 < 0.015:                # SIDEWAYS_BIAS（生产常量）
        return 'sideways'
    if c > ma20:
        return 'weak_up'
    return 'chaos'


def gate_policy(state):
    """GATE_MODE + gate_state → (block_new, force_flat)，镜像 satellite_gate_policy。"""
    st = state or 'chaos'
    if GATE_MODE == 'baseline':
        return False, False
    if GATE_MODE == 'gate_block':                   # 旧臂语义：非放行态仅禁新开
        return st != OPEN_STATE, False
    if GATE_MODE == 'gate_flat':                    # 旧臂语义：非放行态禁新开+清仓
        return st != OPEN_STATE, True
    # gate_down_flat = 生产 satellite_gate_policy（trending_down 清仓，其余仅禁开）
    if st == 'trending_down':
        return True, True
    return st != OPEN_STATE, False


# ── 引擎（= l2_etfself 已定版，未改）────────────────────────────────

def run_rotation(context):
    dt = context.current_dt.strftime('%Y-%m-%d')
    cd = get_current_data()

    state = gate_state_today()
    block_new, force_flat = gate_policy(state)
    log.info("GATE %s mode=%s state=%s block_new=%s force_flat=%s"
             % (dt, GATE_MODE, state, block_new, force_flat))

    # 门控清仓先于常规卖出（旧 rotation_env 挂点语义）
    if force_flat:
        for sec in list(context.portfolio.positions.keys()):
            if context.portfolio.positions[sec].total_amount > 0:
                order_target(sec, 0)
                log.info("门控清仓 %s %s state=%s"
                         % (sec, get_security_name(sec), state))

    # 250 日宽表（含当日；上市前 NaN，停牌日 money=0、close 为停牌前值）
    closes = history(CROWD_LOOKBACK, '1d', 'close', security_list=g.codes)
    money = history(CROWD_LOOKBACK, '1d', 'money', security_list=g.codes)
    rowsum = money.sum(axis=1)

    rows = []
    for code, name in UNIVERSE:
        if cd[code].paused:
            continue                                # 当日停牌不可计分
        h = attribute_history(code, 60, '1d', ['close'], skip_paused=True)
        if h is None or len(h) < 60:
            continue                                # 上市未满 61 根
        price = float(cd[code].last_price)
        if price <= 0:
            continue
        ret20 = round((price / list(h['close'].values)[-21] - 1) * 100, 3)
        # ETF 自身拥挤度代理
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
        rows.append({'code': code, 'name': name, 'price': price,
                     'ret20': ret20, 'crowd': crowd})
    rows.sort(key=lambda r: -r['ret20'])            # 稳定排序，宇宙序解并列
    n = len(rows)
    rank = {r['code']: i + 1 for i, r in enumerate(rows)}

    # 先卖：持仓跌出前 40%（与拥挤度无关）
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0:
            continue
        r = rank.get(sec)
        if r is None:
            continue                                # 停牌/未计分 → 持有
        if r > math.ceil(EXIT_PCT * n):
            order_target(sec, 0)
            log.info("卖出 %s %s rank %d/%d" % (sec, get_security_name(sec), r, n))

    # 后买：拥挤度<90 的前 3 等权（block_new 时跳过）
    if not block_new:
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
    log.info("%s | 截面 %d | 持仓 %s" % (dt, n,
             [s for s, p in context.portfolio.positions.items()
              if p.total_amount > 0]))
