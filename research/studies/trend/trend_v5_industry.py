# -*- coding: utf-8 -*-
# 历史行业分支 v5（原 trend_v5.py）：已中止的实验，当前目录索引见 ../README.md。
# trend_v5 = v4.1 修正骨架去门控 + 行业动量信号层（v4→v4.1→v5）。
# 回测记录（2026-10-02）：运行较久且阶段表现差，用户主动中止，未跑完。
#   已见图表悬停日 2021-03-25：策略收益 -27.57%，沪深300基准 +63.63%；
#   仅为该日累计收益，不是终止日成绩。最终收益、回撤、夏普等未完整留存。
#   本轮暂停个股 trend，不补跑 v5/v4.1；原生产回踩仍停用，先验证行业 ETF 池。
#   这是当前研究骨架的取舍，不据此证伪所有日线主板多头策略。


# v4 判定（2026-10-02）：证伪线双触发（盈亏比 1.084、超额 -23.12%），
#   归因"排序信号在载体内无右尾"。v5 换假设：个股截面排序救不了的，
#   行业维度可能有——先判行业趋势，再在向上行业里选结构强个股。
# v5 改动（相对 v4.1）：
#   ①删总市场门控，改由行业趋势过滤新开仓。所有行业不满足时停止新增
#     买入，已有仓位仍由 14:55 价格追踪止损退出；不能视为立即空仓。
#   ②新增行业趋势层。申万一级 31 行业（get_industries PIT），自建等权
#     行业指数：PIT 成分（get_industry_stocks，限主板宇宙内）45 根收盘
#     →逐日等权成分收益→累积成指数。趋势向上 = 指数 ma10>ma20、
#     点位>ma20、ma20 不降（与个股结构条件同口径，不引入新参数）。
#     每 5 交易日重建一次；一次获取 PIT 成分同时建立个股行业映射，
#     主板成员并集批量取价后按行业切片，避免重复成分与价格请求。
#   ③个股排序换回截至 T-1 的 20 日收益降序——v4 的 dd_hi60+低ATR 已证伪。
#     入选仍是 structure-in（ma10>ma20、价>ma20、MA20 不降），尸检证明
#     只有结构内池有右尾（MFE p90 31.58 vs 结构外 22.52）。
#   ④继承 v4.1 执行修正：T-1 实际换手≤12%，不再乘历史量比；信号/ATR
#     用截至 T-1 的日线，股数/涨停判断/初始峰值用 14:55 的 last_price。
#     2.5ATR 追踪止损、1% 风险预算、10% 单仓上限、当日冷却和滑点沿用。
#   ⑤卖出只看 14:55 价格追踪止损，不看行业转弱（与 v1 ETF 轮动的排名卖出不同；
#     避免信号层与出场层互相污染归因，行业动弱留给下一版单变量验证）。
# 已知近似：自建行业指数只含主板成分（宇宙约束），与全市场申万指数有
#   口径差；行业归属每 5 日取一次 PIT，期内变动忽略。峰值是每日 14:55
#   的价格峰值，并非日内最高价；下单参考价也不保证等于实际成交价。
# 原定验收（历史设计，当前已暂停）：超额收益>0 且盈亏比≥1.9；
#   完整对照需同区间 v4.1 执行基线，但本轮不再要求补跑。
# 原 v4 的 6.74% / -23.12% / 1.084 仅作历史参考，不是 v4.1 的成绩。
# 原定停止线：仍不达标则关闭本骨架；不能推广为所有日线主板长仓均无效。
# 运行：聚宽回测 2019-01-01 至今，初始资金 100 万，天频率。

import math
from collections import defaultdict

from jqdata import *

# ── 宇宙（与 v4.1 相同）──
TURNOVER_MAX = 12.0

# ── 行业趋势层（v5②）──
REBUILD_EVERY = 5           # 行业指数和映射重建间隔（交易日）
IND_BARS = 45               # 成分收盘取数根数（够算 20 日收益链 + 双均线）

# ── 买侧（与 v4.1 相同）──
RISK_PCT = 0.01
MAX_POSITION_PCT = 0.10

# ── 卖侧（与 v4.1 相同）──
ATR_N = 20
K_ATR = 2.5

BARS_NEEDED = 61
CHUNK = 400
FIELDS = ('high', 'low', 'close')


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
    g.day = 0
    g.up_inds = set()
    g.ind_map = {}
    g.ind_rebuild_pending = False
    run_daily(trade, time='14:55')


def get_security_name(security):
    try:
        return get_security_info(security).display_name
    except Exception:
        return security


# ── 宇宙（与 v4.1 相同）──────────────────────────────────────────

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
    """批量取截至 T-1 的 61 根 HLC；省去未使用的 open/volume。"""
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


# ── 行业趋势层（v5②：自建申万一级等权指数，判趋势向上）──────────────

def _is_mainboard(code):
    return (code.startswith('60') and code.endswith('XSHG')) or \
           (code[:3] in ('000', '001', '002', '003') and code.endswith('XSHE'))


def rebuild_industry_trend(dt_key):
    """同一 PIT 成分快照生成趋势和映射；完整成功后一次性替换状态。"""
    try:
        inds = get_industries(name='sw_l1', date=dt_key)
    except Exception as e:  # noqa: BLE001
        log.warning("[IND] %s 行业列表获取失败 %s: %s；保留旧快照，次日重试"
                    % (dt_key, type(e).__name__, str(e)[:150]))
        return False
    if inds is None or inds.empty:
        log.warning("[IND] %s 行业列表为空；保留旧快照，次日重试" % dt_key)
        return False

    members_by_ind = {}
    ind_map = {}
    all_members = set()
    for ind_code in inds.index:
        try:
            stocks = get_industry_stocks(ind_code, date=dt_key)
        except Exception as e:  # noqa: BLE001
            log.warning("[IND] %s 成分获取失败 %s: %s；保留旧快照，次日重试"
                        % (ind_code, type(e).__name__, str(e)[:100]))
            return False
        members = sorted(set(s for s in stocks if _is_mainboard(s)))
        for code in members:
            ind_map[code] = ind_code
        if len(members) < 5:
            continue
        members_by_ind[ind_code] = members
        all_members.update(members)

    closes = None
    all_members = sorted(all_members)
    for i in range(0, len(all_members), CHUNK):
        chunk = all_members[i:i + CHUNK]
        try:
            df = history(IND_BARS, '1d', 'close', security_list=chunk)
        except Exception as e:  # noqa: BLE001
            log.warning("[IND] %s 成分价格获取失败 %s: %s；保留旧快照，次日重试"
                        % (dt_key, type(e).__name__, str(e)[:100]))
            return False
        if df is None or df.empty:
            log.warning("[IND] %s 成分价格返回空；保留旧快照，次日重试" % dt_key)
            return False
        closes = df if closes is None else closes.join(df)

    up = set()
    n_valid = 0
    for ind_code, members in members_by_ind.items():
        if closes is None:
            break
        columns = [code for code in members if code in closes.columns]
        if len(columns) < 5:
            continue
        vals = closes[columns].values              # (T, N)，同一批价格按行业切片
        T = vals.shape[0]
        if T < 25:
            continue
        rets = []                                 # 逐日等权成分收益
        for t in range(1, T):
            acc, cnt = 0.0, 0
            for j in range(vals.shape[1]):
                p0, p1 = vals[t - 1][j], vals[t][j]
                if p0 == p0 and p1 == p1 and p0 > 0:
                    acc += p1 / p0 - 1.0
                    cnt += 1
            rets.append(acc / cnt if cnt >= 5 else 0.0)
        idx = [1.0]
        for r in rets:
            idx.append(idx[-1] * (1.0 + r))
        ma10 = sum(idx[-10:]) / 10.0
        ma20 = sum(idx[-20:]) / 20.0
        ma20_prev = sum(idx[-21:-1]) / 20.0
        if idx[-1] > ma20 and ma10 > ma20 and ma20 >= ma20_prev:
            up.add(ind_code)
        n_valid += 1
    if n_valid == 0:
        log.warning("[IND] %s 无有效行业指数；保留旧快照，次日重试" % dt_key)
        return False
    g.up_inds = up
    g.ind_map = ind_map
    log.info("[IND] %s 行业重建：%d/%d 有效，向上 %d 个：%s"
             % (dt_key, n_valid, len(inds), len(up), sorted(up)))
    return True


# ── 候选（v5③：结构内 + 20 日收益排序）────────────────────────────

def candidate_metrics(bars):
    """结构内返回 (ret20, atr)，否则 None。"""
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
        return None                        # 结构是唯一门槛（v4②沿用）
    base = c[-21]
    if base != base or base <= 0:
        return None
    atr = _atr20(h, l, c)
    if atr is None:
        return None
    return (price / base - 1.0, atr)


# ── 卖侧（与 v4.1 相同：每日 14:55 价格峰值减 2.5ATR）──────────────

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
                ["14:55追踪止损（现价%.2f < 峰%.2f−%.1fATR=%.2f）"
                 % (price, peak, K_ATR, line)])
    return None


# ── 主流程 ──────────────────────────────────────────────────────────

def trade(context):
    dt = context.current_dt
    dt_key = dt.strftime('%Y-%m-%d')
    cd = get_current_data()
    g.sold_today = set()
    g.day += 1

    for code in list(g.entry_date):
        pos = context.portfolio.positions.get(code)
        if pos is None or pos.total_amount <= 0:
            g.entry_date.pop(code, None)
            g.entry_peak.pop(code, None)

    codes_names = mainboard_universe(dt.date())

    if (g.day - 1) % REBUILD_EVERY == 0 or getattr(g, 'ind_rebuild_pending', False):
        g.ind_rebuild_pending = not rebuild_industry_trend(dt_key)
    ind_map = getattr(g, 'ind_map', {})

    screened = screen_universe([c for c, _ in codes_names],
                               context.previous_date)
    bars = fetch_bars(sorted(c for c, _ in screened))
    # 估值表与 history 都截至 T-1；不再用 T-1/T-2 的量比二次外推换手。
    turn_map = {code: tr_prev for code, tr_prev in screened if code in bars}

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

    # 买入（v5③：向上行业内按 T-1 的 ret20 排序，执行价格沿用 v4.1 修正）
    cands = []
    for code in turn_map:
        if ind_map.get(code) not in g.up_inds:
            continue
        b = bars.get(code)
        if b is None or cd[code].paused:
            continue
        m = candidate_metrics(b)
        if m is None:
            continue
        cands.append((code, m[0], m[1]))
    cands.sort(key=lambda x: -x[1])
    holdings = len([p for p in context.portfolio.positions.values()
                    if p.total_amount > 0])
    n_buy = 0
    for code, ret20, atr in cands:
        if code in context.portfolio.positions or code in g.sold_today:
            continue
        price = float(cd[code].last_price)
        if not math.isfinite(price) or price <= 0 or price >= cd[code].high_limit:
            continue                          # 涨停无法成交
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
        log.info("买入 %s %s %d份 @%.2f ind=%s ret20=%.1f%% stop=%.1f%%"
                 % (code, get_security_name(code), shares, price,
                    ind_map.get(code), ret20 * 100, stop_pct))
    log.info("[SCREEN] %s 主板=%d 粗筛=%d 有效bars=%d 宇宙=%d 行业内候选=%d "
             "买=%d 持仓=%d 向上行业=%d"
             % (dt_key, len(codes_names), len(screened), len(bars),
                len(turn_map), len(cands), n_buy, holdings, len(g.up_inds)))
