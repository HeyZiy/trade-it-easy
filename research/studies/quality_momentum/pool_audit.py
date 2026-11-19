# -*- coding: utf-8 -*-
"""质量池 v6 审计版（聚宽单文件）。

策略逻辑与 pool_rotation.py（v6）逐行等价，只增加埋点与对照开关。
改 MODE 一个字母就能跑对照组，其余代码不动。

MODE:
  base     —— v6 原样。先跑它，校验累计收益是否仍为 283.8% 左右。
  bottom6  —— 反向排名（取强弱分数最差的 6 名）。若它也跑赢基准很多，
              说明 alpha 来自质量池本身，而不是 12-1 强弱分数。
  cap30    —— 单票权重超过 32% 时削回 30%。测风控成本。
  nogrowth —— 关闭 10% 增速门槛（仍要求同比数据已知）。测该门槛是否必要。
  pb8      —— PB 上限从 5 放宽到 8。base 里唯一 100% 胜率的出场渠道就是 PB 触顶，
              这一组直接拧动兑现阀门，看赢家能不能跑更久。

比较区间：日频，2016-01-04 至 2026-09-01，100 万元，中证 800。

日志里所有证据行以 [AUDIT] 开头，跑完把日志整段粘给我。
"""
from jqdata import *
from jqfactor import get_factor_values
import builtins as _b
import math as _math

import numpy as np
import pandas as pd


# ───────────── 实验开关（只改这一段） ─────────────
# MODE: base      原策略
#       bottom6   动量取最差 6 名（反向对照）
#       cap30     单票权重上限 30%
#       nogrowth  关掉 10% 增速门槛
#       pb8       PB 上限放到 8（已由 POST 数据否决，勿跑）
#       hold8     持仓从 6 只放到 8 只（组合宽度）
MODE = 'base'
POOL_MIN = 0              # >0 时池内可用标的不足则暂停补位
CAP_WEIGHT = 0.30         # cap30 模式的目标权重上限
CAP_TRIGGER = 0.02        # 超过 CAP_WEIGHT + 该值才动手，降低换手
FULL_DEPTH_FILL = False   # True 时按全池排名补位，不提前截断到 6
AUDIT = True
COMPACT = True            # True 时月度序列压成少数几行，逐日/逐笔噪音全部收敛
# 聚宽日志有行数上限，早期版本 130 个月 × 4 条序列 + 逐笔成交价探针会超出上限，
# 导致结尾的汇总行被截掉。COMPACT 把 1775 行压到约 30 行。
CHUNK = 40                # 每条压缩行容纳的记录数
VERBOSE_EXITS = False     # True 时逐只打印出池/恢复，逐笔噪音会占满日志面板
POST_MARKS = (20, 60, 120)  # 卖出后跟踪的时间点（交易日），用来判断卖出规则是否切早了


MAX_HOLDINGS = 8 if MODE == 'hold8' else 6
INDUSTRY_CAP = 2
ROE_MIN = 12.0
GROWTH_MIN = 10.0
PE_MAX = 30.0
PB_MAX = 5.0
VOLATILITY_MAX = 35.0
VOLATILITY_CLOSES = 61
MOMENTUM_CLOSES = 253
SKIP_RECENT_CLOSES = 21
COMMISSION = 0.00025
MIN_COMMISSION = 5.0
SLIPPAGE_SPREAD = 0.002
LIMIT_PAD = 0.001


def initialize(context):
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark('000906.XSHG')
    set_slippage(PriceRelatedSlippage(SLIPPAGE_SPREAD))
    log.set_level('order', 'error')
    g.exit_pending = {}
    g.entry_plan = None
    g.last_signal_day = g.last_execution_day = g.sell_tax = None
    # 审计状态
    g.audit_done = False
    g.daily = []
    g.monthly = []
    g.wgt_rows = []
    g.pool_month = {}
    g.trades = []
    g.ledger = {}
    g.intents = {}
    g.prev_amount = {}
    g.last_px = {}
    g.td = 0
    g.peak = 0.0
    g.month_key = None
    g.last_snap = None
    g.probe_month = None
    g.start_cash = _b.float(context.portfolio.starting_cash)
    g.fill_dev = {'open': [], 'close': [], 'quote': []}
    g.snap_vals = None    # 当日全池门槛快照，建仓时用来记录入场垫有多厚
    g.snap_day = None     # 快照属于哪一天，跨日就作废
    g.mae = {}            # (code, 建仓日) -> 持有期内的最大不利偏移
    g.mae_rows = []       # 平仓时落盘的 MAE
    g.entries = []        # 建仓当时的 growth/roe/pe/pb
    g.watch = []          # 卖出后待跟踪的标的
    g.post = []           # 卖出后 20/60/120 个交易日的涨跌幅
    _set_costs(context)
    run_daily(on_signal, time='08:00')
    run_daily(on_open, time='09:31')
    _emit('[AUDIT]INIT %.2f' % g.start_cash)
    _emit('[AUDIT]CFG mode=%s poolmin=%d cap=%.2f trigger=%.2f depth=%s holdings=%d pbmax=%.1f growth=%.1f roe=%.1f'
          % (MODE, POOL_MIN, CAP_WEIGHT, CAP_WEIGHT + CAP_TRIGGER,
             str(FULL_DEPTH_FILL), MAX_HOLDINGS, _pb_max(), _growth_floor(), ROE_MIN))


def _emit(line):
    if AUDIT:
        log.info(line)


def _emit_rows(tag, items, per_line=None):
    """把多条记录压进少数几行，用 ~ 分隔（出池原因里含 |，不能拿它当分隔符），避免每月一条把日志刷爆。"""
    if not AUDIT:
        return
    size = per_line or CHUNK
    chunk = []
    for item in items:
        chunk.append(item)
        if _b.len(chunk) >= size:
            _emit('[AUDIT]%s %s' % (tag, '~'.join(chunk)))
            chunk = []
    if chunk:
        _emit('[AUDIT]%s %s' % (tag, '~'.join(chunk)))


def _set_costs(context):
    tax = 0.0005 if pd.Timestamp(context.current_dt) >= pd.Timestamp('2023-08-28') else 0.001
    if tax != g.sell_tax:
        set_order_cost(OrderCost(open_tax=0, close_tax=tax, open_commission=COMMISSION,
                                 close_commission=COMMISSION, min_commission=MIN_COMMISSION), type='stock')
        g.sell_tax = tax


def _held(context):
    return {code for code, pos in context.portfolio.positions.items()
            if pos.total_amount + _b.getattr(pos, 'locked_amount', 0) > 0}


def _report_date(value):
    if pd.isna(value):
        return pd.NaT
    text = _b.str(value).strip().lower()
    try:
        if _b.len(text) == 6 and text[:4].isdigit() and text[4] == 'q' and text[5] in '1234':
            return pd.Period(text, freq='Q').end_time.normalize()
        date = pd.Timestamp(value).normalize()
        return date if date.is_quarter_end else pd.NaT
    except (TypeError, ValueError, OverflowError):
        return pd.NaT


def _indexed(frame, fields, source):
    if (not _b.isinstance(frame, pd.DataFrame) or not {'code', *fields} <= set(frame.columns)
            or frame['code'].duplicated().any()):
        raise ValueError('Invalid %s snapshot' % source)
    return frame.set_index('code')


def _financials(asof, codes):
    fields = ('inc_net_profit_year_on_year', 'statDate', 'pubDate')
    q = query(indicator.code, indicator.inc_net_profit_year_on_year, indicator.statDate, indicator.pubDate)
    financial = get_fundamentals(q.filter(indicator.code.in_(codes)) if _b.len(codes) <= 4500 else q,
                                 date=asof)
    if _b.isinstance(financial, pd.DataFrame) and financial.empty and _b.len(codes) <= 4500:
        financial = get_fundamentals(q, date=asof)
    financial = _indexed(financial, fields, 'financial').reindex(codes)
    visible = ((financial['statDate'].map(_report_date) <= asof)
               & (pd.to_datetime(financial['pubDate'], errors='coerce') <= asof))
    factors = get_factor_values(securities=codes, factors=['roe_ttm'], end_date=asof.date(), count=1)
    roe = factors['roe_ttm']
    if roe.empty or pd.Timestamp(roe.index[-1]).normalize() != asof:
        raise ValueError('ROE cutoff mismatch')
    valuations = get_fundamentals(query(valuation.code, valuation.pe_ratio, valuation.pb_ratio)
                                  .filter(valuation.code.in_(codes)), date=asof)
    valuations = _indexed(valuations, ('pe_ratio', 'pb_ratio'), 'valuation').reindex(codes)
    values = valuations[['pe_ratio', 'pb_ratio']].apply(pd.to_numeric, errors='coerce')
    values['roe'] = (pd.to_numeric(roe.iloc[-1], errors='coerce').reindex(codes) * 100).where(visible)
    values['growth'] = pd.to_numeric(financial[fields[0]], errors='coerce').where(visible)
    return values.replace([np.inf, -np.inf], np.nan)


def _growth_floor():
    return -1e9 if MODE == 'nogrowth' else GROWTH_MIN


def _pb_max():
    return 8.0 if MODE == 'pb8' else PB_MAX


def _quality(asof, universe, requested, snap=False):
    failed = {code: 'not_in_mainboard_universe' for code in requested - universe}
    codes = _b.sorted(requested & universe)
    if not codes:
        return set(), failed
    values = _financials(asof, codes)
    quotes = get_current_data()
    flags = {}
    for code in codes:
        try:
            flags[code] = _b.getattr(quotes[code], 'is_st', None)
        except KeyError:
            flags[code] = None
    st = pd.Series(flags)
    violations = pd.DataFrame({
        'roe_low': values.roe.le(ROE_MIN),
        'profit_growth_low': values.growth.le(_growth_floor()),
        'pe_nonpos': values.pe_ratio.le(0), 'pe_high': values.pe_ratio.ge(PE_MAX),
        'pb_nonpos': values.pb_ratio.le(0), 'pb_high': values.pb_ratio.ge(_pb_max()),
        'st': st.eq(True),
    })
    out = violations.any(axis=1)
    for code, row in violations.loc[out].iterrows():
        failed[code] = '|'.join(row.index[row])
    passed = set(values.index[~out & values.notna().all(axis=1) & st.notna()])
    if snap:
        # 只缓存"全池快照"这一次，建仓时用来记录入场当时的四道门槛距离
        g.snap_vals = values
    return passed, failed


def _rank_entries(asof, codes, limit=None):
    limit = MAX_HOLDINGS if limit is None else limit
    if not codes:
        return []
    ordered = _b.sorted(codes)
    closes = history(MOMENTUM_CLOSES, '1d', 'close', security_list=ordered,
                     skip_paused=False, df=True)
    expected = pd.DatetimeIndex(get_trade_days(end_date=asof.date(), count=MOMENTUM_CLOSES)).normalize()
    if (_b.len(expected) != MOMENTUM_CLOSES or not _b.isinstance(closes, pd.DataFrame)
            or not codes <= set(closes.columns)
            or not pd.DatetimeIndex(closes.index).normalize().equals(expected)):
        raise ValueError('Daily close matrix cutoff does not match T-1')
    closes = closes[ordered]
    prices = closes.apply(pd.to_numeric, errors='coerce').astype(_b.float).values
    # 新股/长期停牌会让窗口内出现 NaN，比较 NaN 会触发 RuntimeWarning；
    # 语义上已由 isfinite 排除，这里只静默这类噪声，不影响判定结果。
    with np.errstate(invalid='ignore'):
        vol_window = prices[-VOLATILITY_CLOSES:]
        valid_vol = (np.isfinite(vol_window) & (vol_window > 0)).all(axis=0)
        vol_window = vol_window[:, valid_vol]
        returns = vol_window[1:] / vol_window[:-1] - 1.0
        volatility = returns.std(axis=0, ddof=1) * _math.sqrt(250) * 100.0
        eligible = valid_vol.copy()
        eligible[valid_vol] = volatility < VOLATILITY_MAX
        columns = closes.columns[eligible]
        momentum = prices[:-SKIP_RECENT_CLOSES, eligible]
        valid = (np.isfinite(momentum) & (momentum > 0)).all(axis=0)
        scores = _b.dict(_b.zip(columns[valid], momentum[-1, valid] / momentum[0, valid] - 1.0))
    if MODE == 'bottom6':
        return _b.sorted(scores, key=lambda code: (scores[code], code))[:limit]
    return _b.sorted(scores, key=lambda code: (-scores[code], code))[:limit]


def _update_exits(held, passed, failed):
    for code in held & passed:
        if g.exit_pending.pop(code, None) is not None:
            if VERBOSE_EXITS:
                log.info('%s 恢复持仓资格，撤销退出' % code)
    for code in _b.sorted(held & set(failed)):
        if code not in g.exit_pending:
            g.exit_pending[code] = failed[code]
            if VERBOSE_EXITS:
                log.info('%s 确认出池：%s' % (code, failed[code]))


def _review_holdings(asof, universe, held):
    try:
        passed, failed = _quality(asof, universe, held)
    except Exception as exc:
        log.info('持仓快照异常：%s' % exc)
        return False
    _update_exits(held, passed, failed)
    return True


def _industry_available(code, held, industries):
    industry = industries.get(code)
    return (industry and _b.all(industries.get(other) for other in held)
            and _b.sum(industries[other] == industry for other in held) < INDUSTRY_CAP)


def on_signal(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_signal_day == today:
        return
    g.last_signal_day, g.entry_plan = today, None
    held = _held(context)
    g.exit_pending = {code: reason for code, reason in g.exit_pending.items() if code in held}
    try:
        days = get_trade_days(end_date=today.date(), count=2)
        if _b.len(days) != 2 or pd.Timestamp(days[-1]).normalize() != today:
            raise ValueError('Missing previous trading day')
        asof = pd.Timestamp(days[0]).normalize()
        securities = get_all_securities(['stock'], date=today)
        if not _b.isinstance(securities, pd.DataFrame) or securities.empty:
            raise ValueError('Missing historical stock universe')
        universe = {code for code in securities.index if code[:2] in ('60', '00')}
    except Exception as exc:
        log.info('资格数据暂停：%s' % exc)
        return

    # 审计：每月首个交易日强制测一次池规模（纯测量，不改状态）
    mkey = today.strftime('%Y%m')
    if AUDIT and g.probe_month != mkey:
        g.probe_month = mkey
        try:
            probe, _ = _quality(asof, universe, universe)
            g.pool_month[mkey] = _b.len(probe)
        except Exception as exc:
            log.info('池规模探针失败：%s' % exc)

    reviewed = _b.len(held) >= MAX_HOLDINGS and _review_holdings(asof, universe, held)
    if reviewed and _b.len(held - set(g.exit_pending)) >= MAX_HOLDINGS:
        return

    try:
        passed, failed = _quality(asof, universe, universe | held, snap=True)
        g.snap_day = today
    except Exception as exc:
        if held and not reviewed:
            _review_holdings(asof, universe, held)
        log.info('全市场快照异常：%s' % exc)
        return
    _update_exits(held, passed, failed)
    kept = held - set(g.exit_pending)
    try:
        if _b.len(kept) >= MAX_HOLDINGS:
            return
        if POOL_MIN and _b.len(passed) < POOL_MIN:
            log.info('池内可用 %d 只，低于下限 %d，暂停补位' % (_b.len(passed), POOL_MIN))
            return
        ranked = _rank_entries(asof, passed, MAX_HOLDINGS * 5 if FULL_DEPTH_FILL else None)
        if not ranked:
            return
        codes = set(ranked) | held
        data = get_industry(_b.sorted(codes), date=asof)
        industries = {code: ((data.get(code) or {}).get('sw_l1') or {}).get('industry_code')
                      for code in codes}
    except Exception as exc:
        log.info('补仓数据暂停：%s' % exc)
        return

    entries = []
    selected = set(kept)
    for code in ranked:
        if _b.len(selected) >= MAX_HOLDINGS:
            break
        if code not in held and _industry_available(code, selected, industries):
            entries.append(code)
            selected.add(code)
    g.entry_plan = (today, entries, industries)


def _tradable_price(quotes, code, buying):
    try:
        quote = quotes[code]
        price, low, high = _b.float(quote.last_price), _b.float(quote.low_limit), _b.float(quote.high_limit)
        if pd.isna(quote.paused) or quote.paused or pd.isna(quote.is_st) or (buying and quote.is_st):
            return None
        if not (_b.all(_math.isfinite(value) for value in (price, low, high)) and high > low > 0 and price > 0):
            return None
        if (buying and price >= high - LIMIT_PAD) or (not buying and price <= low + LIMIT_PAD):
            return None
        return price
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _order_target(code, quantity):
    try:
        return order_target(code, quantity)
    except Exception as exc:
        log.info('%s 委托失败：%s' % (code, exc))
        return None


def _intent(code, side, px, qty, reason):
    if not AUDIT:
        return
    g.intents[code] = {'side': side, 'px': _b.float(px), 'qty': _b.int(qty), 'reason': reason}


def _trim_overweight(context, quotes, total):
    for code in _b.sorted(_held(context)):
        if code in g.exit_pending:
            continue
        pos = context.portfolio.positions[code]
        px = _tradable_price(quotes, code, buying=False)
        if px is None:
            continue
        weight = pos.total_amount * px / total
        if weight <= CAP_WEIGHT + CAP_TRIGGER:
            continue
        target = _b.int((CAP_WEIGHT * total / px) // 100) * 100
        cut = _b.int((pos.total_amount - target) // 100) * 100
        cut = _b.min(cut, pos.closeable_amount)
        if cut < 100:
            continue
        _order_target(code, pos.total_amount - cut)
        _intent(code, 'sell', px, cut, 'weight_cap')


def on_open(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_execution_day == today:
        return
    g.last_execution_day = today
    _set_costs(context)
    g.intents = {}
    plan, g.entry_plan = g.entry_plan, None
    if plan is not None and plan[0] != today:
        plan = None
    if MODE != 'cap30' and plan is None and not g.exit_pending:
        return
    try:
        quotes = get_current_data()
    except Exception as exc:
        log.info('执行行情暂停：%s' % exc)
        return
    portfolio = context.portfolio
    if MODE == 'cap30':
        _trim_overweight(context, quotes, _b.float(portfolio.total_value))
    budget = portfolio.total_value / MAX_HOLDINGS
    open_orders = get_open_orders()
    open_codes = {order.security for order in open_orders.values()}
    for code in _b.sorted((_held(context) & set(g.exit_pending)) - open_codes):
        pos = portfolio.positions[code]
        if pos.closeable_amount > 0:
            px = _tradable_price(quotes, code, buying=False)
            if px is not None:
                _order_target(code, _b.max(0, pos.total_amount - pos.closeable_amount))
                _intent(code, 'sell', px, _b.min(pos.closeable_amount, pos.total_amount),
                        g.exit_pending.get(code, 'out_of_pool'))
    g.exit_pending = {code: reason for code, reason in g.exit_pending.items() if code in _held(context)}
    if plan is None:
        return

    _, entries, industries = plan
    for code in entries:
        open_orders = get_open_orders().values()
        reserved = _held(context) | {order.security for order in open_orders if order.is_buy}
        if _b.len(reserved) >= MAX_HOLDINGS:
            break
        if code in reserved or code in open_codes or not _industry_available(code, reserved, industries):
            continue
        price = _tradable_price(quotes, code, buying=True)
        if price is None:
            continue
        cash = portfolio.available_cash
        estimated = price * (1 + SLIPPAGE_SPREAD / 2)
        affordable = _b.min(cash / (estimated * (1 + COMMISSION)), (cash - MIN_COMMISSION) / estimated)
        shares = _b.int(_b.min(budget / price, affordable) // 100) * 100
        if shares >= 100:
            _order_target(code, shares)
            _intent(code, 'buy', price, shares, 'entry')


# ───────────── 以下全部是埋点，不参与交易决策 ─────────────

def _weight_row(context, mkey):
    total = _b.float(context.portfolio.total_value)
    weights = []
    for code, pos in context.portfolio.positions.items():
        if pos.total_amount <= 0:
            continue
        price = _b.float(_b.getattr(pos, 'price', np.nan))
        if not np.isfinite(price) or price <= 0:
            price = g.last_px.get(code, np.nan)
        if np.isfinite(price) and total > 0:
            weights.append(pos.total_amount * price / total)
    invested = _b.sum(weights)
    max_w = _b.max(weights) if weights else 0.0
    hhi = _b.sum(w * w for w in weights)
    return '%s,%d,%.4f,%.4f,%.4f' % (mkey, _b.len(weights), invested, max_w, hhi)


def _ledger_buy(code, qty, px, today, corp=False):
    """登记一笔买入。

    corp=True 表示这不是成交，而是送转/拆股带来的股数自然增加：
    公司一股没买，现金一分没出 → **成本必须保持不变**，只让每股成本被摊薄。
    如果把这种股数增加当成买入记成本，每股成本会被抬高，平仓盈亏和 MAE 一起失真
    （实测 13 只送转票因此把 113 万真实利润记成了 -29 万亏损）。
    """
    lot = g.ledger.get(code)
    if lot is None or lot['qty'] <= 0:
        lot = {'qty': 0, 'bought': 0, 'cost': 0.0, 'sold': 0, 'proceeds': 0.0,
               'entry': today, 'entry_td': g.td, 'corp': 0, 'entry_px': 0.0, 'avg': 0.0}
        g.ledger[code] = lot
    lot['qty'] += qty
    lot['bought'] += qty
    if corp:
        lot['corp'] += qty
        _emit('[AUDIT]CORP %s,%s,%d,%.4f'
              % (code, today.strftime('%Y%m%d'), qty,
                 lot['cost'] / _b.max(1, lot['bought'] - lot['corp'])))
    else:
        lot['cost'] += qty * px
        if lot['entry_px'] <= 0:
            lot['entry_px'] = px   # 记建仓成交价，供「价格止损」口径的 MAE 使用


def _mae_update(context, today):
    """记录每笔持仓的最不利偏移（MAE）：建仓以来最低价跌了多少。

    止损能不能做，取决于赢家在启动前有没有先跌破阈值——这个数据只有路径里才有。
    用最终收益去估算止损收益会得到"误杀 0 笔"的假象，所以必须逐日跟踪。

    基准用**建仓成交价**：止损是一条价格规则，触发条件是"股价比买价低 X%",
    不能用分红摊薄后的 avg_cost 当基准（否则分红越多的票越难触发，止损形同虚设）。
    顺带把平台 avg_cost 缓存进 lot['avg']，供平仓时按含分红口径结算。
    """
    try:
        for code, lot in list(g.ledger.items()):
            if lot['qty'] <= 0 or lot['bought'] <= 0:
                continue
            pos = context.portfolio.positions.get(code)
            if pos is None:
                continue
            px = _b.float(_b.getattr(pos, 'price', np.nan))
            avg = _b.float(_b.getattr(pos, 'avg_cost', np.nan))
            if np.isfinite(avg) and avg > 0:
                lot['avg'] = avg          # 聚宽的 avg_cost 已按送转/派息调整，与官方平仓盈亏同源
            base = lot['entry_px'] if lot['entry_px'] > 0 else lot['cost'] / _b.max(1, lot['bought'])
            if not (np.isfinite(px) and px > 0 and base > 0):
                continue
            dev = px / base - 1.0
            key = (code, lot['entry'])
            if dev < g.mae.get(key, 0.0):
                g.mae[key] = dev
    except Exception as exc:
        _emit('[AUDIT]ERR mae %s' % exc)


def _ledger_sell(code, qty, px, today, reason):
    lot = g.ledger.get(code)
    if lot is None or lot['qty'] <= 0:
        return
    qty = _b.min(qty, lot['qty'])
    lot['qty'] -= qty
    lot['sold'] += qty
    lot['proceeds'] += qty * px
    if lot['qty'] <= 0:
        # 结算口径：成本用平台 avg_cost（每股价，已按分红送转调整）。
        # 分红是现金流入、不产生成交，只把 avg_cost 摊低，所以 proceeds - qty*avg_cost
        # 自动把分红计进收益，与聚宽导出的「平仓盈亏」同源。
        # 送转同理：股数变化由 avg_cost 与 qty 同步调整吸收，不需要单独复权。
        avg = lot['avg']
        if avg > 0:
            pnl = lot['proceeds'] - lot['sold'] * avg
        else:
            pnl = lot['proceeds'] - lot['cost']
        entry_px = lot['entry_px'] if lot['entry_px'] > 0 else lot['cost'] / _b.max(1, lot['bought'])
        exit_px = lot['proceeds'] / _b.max(1, lot['sold'])
        ret = pnl / lot['cost'] if lot['cost'] > 0 else 0.0   # 分母是实际投入现金，含分红总收益
        g.mae_rows.append((code, lot['entry'], g.mae.get((code, lot['entry']), 0.0)))
        g.trades.append((code, lot['entry'], today, entry_px, exit_px, lot['sold'], pnl,
                         g.td - lot['entry_td'], reason, ret))
        # 记进卖出后跟踪队列：卖出之后还涨不涨，是判断这条规则是否切早了的唯一直接证据
        if exit_px > 0:
            g.watch.append((code, today, exit_px, g.td, reason))
        g.ledger.pop(code, None)


def _fill_probe(code, today, quote, fill, pos_price):
    """记录平台实际成交价与当日开盘/收盘价的距离，用于判断日频回测的成交口径。

    埋点是为了取证，绝不能因为它出错把十年回测打断，所以整体兜住异常。
    """
    try:
        try:
            bar = attribute_history(code, 1, '1d', ['open'], skip_paused=False, df=True)
            bar_open = _b.float(bar['open'].iloc[-1])
        except Exception:
            bar_open = np.nan
        if COMPACT:
            # 只累计偏离，收尾时输出统计，不再逐笔打印
            for key, ref in (('open', bar_open), ('close', pos_price), ('quote', quote)):
                if ref is None or not np.isfinite(ref) or ref <= 0:
                    continue
                dev = (fill / ref - 1.0) * 10000
                g.fill_dev.setdefault(key, []).append(dev)
            return
        _emit('[AUDIT]FILL %s,%s,%.4f,%.4f,%.4f,%.4f'
              % (code, today.strftime('%Y%m%d'),
                 quote if (quote is not None and np.isfinite(quote)) else np.nan,
                 fill, bar_open, pos_price if np.isfinite(pos_price) else np.nan))
    except Exception as exc:
        _emit('[AUDIT]ERR fill %s' % exc)


def _post_exit_probe(today):
    """卖出后跟踪：记录出场后 20/60/120 个交易日的涨跌幅。

    按出场原因分组只能说明"触发时这票是赚是亏"，不能说明这条规则切早还是切晚。
    唯一能回答后者的，是卖出之后它继续涨了多少——这就是本探针的用途。
    埋点不参与交易决策，出错只记一行，绝不打断回测。
    """
    try:
        if not g.watch:
            return
        due = [w for w in g.watch if g.td - w[3] in POST_MARKS]
        if not due:
            return
        quotes = get_current_data()
        for code, exit_day, exit_px, entry_td, reason in due:
            try:
                px = _b.float(quotes[code].last_price)
            except Exception:
                continue
            if not (np.isfinite(px) and px > 0) or not (np.isfinite(exit_px) and exit_px > 0):
                continue
            g.post.append((code, reason, pd.Timestamp(exit_day).strftime('%Y%m%d'),
                           g.td - entry_td, px / exit_px - 1.0))
        keep = g.td - _b.min(POST_MARKS)
        g.watch = [w for w in g.watch if g.td - w[3] < keep]
    except Exception as exc:
        _emit('[AUDIT]ERR post %s' % exc)


def _entry_probe(code, today):
    """建仓时记下四道门槛的距离，用来回答"买进来的票离门槛有多近、多久后被杀"。

    出场端已经证明没有切早，剩下的问题只能在入场端：如果被杀掉的票当年入场时
    增速就贴着 10% 的线，那问题在"买得太将就"；如果入场时高增长、后来才掉，
    那问题在市场环境，改门槛没用。
    """
    try:
        values = _b.getattr(g, 'snap_vals', None)
        if values is None or _b.getattr(g, 'snap_day', None) != today:
            return
        row = values.loc[code]
        growth = _b.float(row['growth']) if np.isfinite(row['growth']) else np.nan
        g.entries.append((code, today.strftime('%Y%m%d'),
                          growth if np.isfinite(growth) else -999.0,
                          _b.float(row['roe']) if np.isfinite(row['roe']) else -999.0,
                          _b.float(row['pe_ratio']) if np.isfinite(row['pe_ratio']) else -999.0,
                          _b.float(row['pb_ratio']) if np.isfinite(row['pb_ratio']) else -999.0))
    except Exception as exc:
        _emit('[AUDIT]ERR entry %s' % exc)


def _reconcile(context, today):
    positions = context.portfolio.positions
    cur = {code: _b.int(pos.total_amount) for code, pos in positions.items() if pos.total_amount > 0}
    for code in _b.sorted(set(cur) | set(g.prev_amount)):
        delta = cur.get(code, 0) - g.prev_amount.get(code, 0)
        if delta == 0:
            continue
        held_before = g.prev_amount.get(code, 0)
        intent = g.intents.get(code) or {}
        want = _b.int(intent.get('qty') or 0)
        # 送转/拆股：持仓股数凭空增加（有成交记录、无委托意图）→ 不是买入，不能记成本
        corp = delta > 0 and held_before > 0 and want <= 0
        px = intent.get('px')
        if delta > 0 and not corp and held_before == 0 and code in positions:
            fill = _b.float(_b.getattr(positions[code], 'avg_cost', np.nan))
            if np.isfinite(fill) and fill > 0:
                _fill_probe(code, today, px, fill, _b.float(_b.getattr(positions[code], 'price', np.nan)))
                px = fill
            _entry_probe(code, today)
        if corp:
            # 送转不需要价格参与记账（只是股数变多），价格拿不到也必须登记，否则股数会永久少记
            _ledger_buy(code, delta, 0.0, today, corp=True)
            continue
        if px is None or not np.isfinite(px) or px <= 0:
            px = _b.float(positions[code].price) if code in positions else g.last_px.get(code, np.nan)
        if not (np.isfinite(px) and px > 0):
            continue
        if delta > 0:
            _ledger_buy(code, delta, px, today, corp=False)
        else:
            _ledger_sell(code, -delta, px, today, intent.get('reason') or 'unknown')
    g.prev_amount = cur
    g.intents = {}


def after_trading_end(context):
    today = pd.Timestamp(context.current_dt).normalize()
    g.td += 1
    value = _b.float(context.portfolio.total_value)
    g.daily.append((today, value))
    if value > g.peak:
        g.peak = value
    _reconcile(context, today)
    _post_exit_probe(today)
    _mae_update(context, today)
    mkey = today.strftime('%Y%m')
    if g.month_key != mkey:
        if g.last_snap is not None:
            g.monthly.append((g.last_snap[0], g.last_snap[1]))
            g.wgt_rows.append(g.last_snap[2])
        g.month_key = mkey
    g.last_snap = (mkey, value, _weight_row(context, mkey))
    for code, pos in context.portfolio.positions.items():
        if pos.total_amount > 0:
            price = _b.float(_b.getattr(pos, 'price', np.nan))
            if np.isfinite(price) and price > 0:
                g.last_px[code] = price


def _year_rows(daily, trades):
    if not daily:
        return []
    frame = pd.DataFrame(daily, columns=['date', 'value'])
    frame['year'] = pd.to_datetime(frame['date']).dt.year
    frame['dd'] = frame['value'] / frame['value'].cummax() - 1.0
    rows = []
    prev_last = None
    for year, part in frame.groupby('year'):
        # 首年必须用初始资金作基准，否则会把第一天的亏损吞掉（2016-01-04 熔断当天就有回撤）
        base = prev_last if prev_last is not None else g.start_cash
        ret = _b.float(part['value'].iloc[-1]) / base - 1.0
        prev_last = _b.float(part['value'].iloc[-1])
        worst = _b.float(part['dd'].min())
        worst_day = pd.Timestamp(part['date'].iloc[_b.int(part['dd'].values.argmin())]).strftime('%Y%m%d')
        closed = _b.sum(1 for t in trades if pd.Timestamp(t[2]).year == year)
        rows.append((_b.int(year), ret, worst, worst_day, closed))
    return rows


def _benchmark_rows():
    rows = []
    if not g.daily:
        return rows
    try:
        first = pd.Timestamp(g.daily[0][0])
        last = pd.Timestamp(g.daily[-1][0])
        frame = get_price('000906.XSHG', start_date=first.date(), end_date=last.date(),
                          frequency='daily', fields=['close'], skip_paused=True, panel=False)
        if 'time' in frame.columns:
            series = pd.Series(frame['close'].values, index=pd.to_datetime(frame['time']))
        else:
            series = pd.Series(frame['close'].values, index=pd.to_datetime(frame.index))
        series = series.dropna()
        if series.empty:
            return rows
        _emit('[AUDIT]BMK0 %s,%.4f' % (series.index[0].strftime('%Y%m%d'), _b.float(series.iloc[0])))
        monthly = series.groupby(series.index.strftime('%Y%m')).last()
        for key, value in monthly.items():
            rows.append((key, _b.float(value)))
    except Exception as exc:
        _emit('[AUDIT]ERR benchmark %s' % exc)
    return rows


def _close_open_lots(today):
    """回测收尾时把仍持仓的仓位按最后价格结转成一笔未平仓交易，避免漏掉最大赢家的贡献。"""
    for code in _b.sorted(list(g.ledger.keys())):
        lot = g.ledger[code]
        px = g.last_px.get(code, np.nan)
        if lot['qty'] <= 0 or not (np.isfinite(px) and px > 0):
            continue
        held_cost = lot['cost'] * (float(lot['qty']) / _b.max(1, lot['bought']))
        pnl = lot['qty'] * px - held_cost
        g.trades.append((code, lot['entry'], today,
                         lot['entry_px'] if lot['entry_px'] > 0 else lot['cost'] / _b.max(1, lot['bought']),
                         px, lot['qty'], pnl, g.td - lot['entry_td'], 'open_at_end',
                         pnl / lot['cost'] if lot['cost'] > 0 else 0.0))
        g.ledger.pop(code, None)


def on_strategy_end(context):
    if not AUDIT or g.audit_done:
        return
    g.audit_done = True
    try:
        _close_open_lots(pd.Timestamp(context.current_dt).normalize())
        if g.daily:
            _emit('[AUDIT]EQ0 %s,%.2f'
                  % (pd.Timestamp(g.daily[0][0]).strftime('%Y%m%d'), g.daily[0][1]))
        if g.last_snap is not None and (not g.monthly or g.monthly[-1][0] != g.last_snap[0]):
            g.monthly.append((g.last_snap[0], g.last_snap[1]))
            g.wgt_rows.append(g.last_snap[2])
        _emit_rows('EQ', ['%s,%.2f' % (key, value) for key, value in g.monthly])
        _emit_rows('WGT', list(g.wgt_rows))
        _emit_rows('POOL', ['%s,%d' % (key, g.pool_month[key]) for key in _b.sorted(g.pool_month)])
        _emit_rows('BMK', ['%s,%.4f' % (key, value) for key, value in _benchmark_rows()])
        reasons = {}
        rows = []
        for trade in g.trades:
            rows.append('%s,%s,%s,%.4f,%.4f,%d,%.2f,%d,%s,%.4f'
                        % (trade[0], pd.Timestamp(trade[1]).strftime('%Y%m%d'),
                           pd.Timestamp(trade[2]).strftime('%Y%m%d'), trade[3], trade[4],
                           trade[5], trade[6], trade[7], trade[8],
                           trade[9] if _b.len(trade) > 9 else 0.0))
            reasons[trade[8]] = reasons.get(trade[8], 0) + 1
        _emit_rows('TRD', rows)
        _emit_rows('EXR', ['%s,%d' % (reason, reasons[reason]) for reason in _b.sorted(reasons)])
        _emit_rows('POST', ['%s,%s,%s,%d,%.4f' % (code, reason, day, age, ret)
                          for code, reason, day, age, ret in g.post])
        _emit_rows('ENTRY', ['%s,%s,%.2f,%.2f,%.2f,%.2f' % row
                          for row in _b.getattr(g, 'entries', [])])
        _emit_rows('MAE', ['%s,%s,%.4f' % (code, pd.Timestamp(entry).strftime('%Y%m%d'), mae)
                           for code, entry, mae in _b.getattr(g, 'mae_rows', [])])
        _emit_rows('YR', ['%d,%.4f,%.4f,%s,%d' % (year, ret, worst, worst_day, closed)
                          for year, ret, worst, worst_day, closed in _year_rows(g.daily, g.trades)])
        for key in ('quote', 'open', 'close'):
            vals = g.fill_dev.get(key) or []
            if vals:
                absmed = _b.float(np.median(np.abs(np.array(vals, dtype=float))))
                signmed = _b.float(np.median(np.array(vals, dtype=float)))
                pos = _b.sum(1 for v in vals if v > 0) / _b.float(_b.len(vals))
                _emit('[AUDIT]FILLS %s,n=%d,abs_med=%.0f,sign_med=%.1f,pos=%.3f'
                      % (key, _b.len(vals), absmed, signmed, pos))
        values = [value for _, value in g.daily]
        if values:
            frame = pd.DataFrame(g.daily, columns=['date', 'value'])
            returns = frame['value'].pct_change().dropna()
            # 聚宽按交易日数/250 折算年化，这里跟随同一口径，否则和平台数字对不上
            span = _b.max(1.0, _b.float(_b.len(g.daily)) / 250.0)
            # 基准必须是初始资金：首日权益已经含当日盈亏，用它当分母会吞掉第一天的回撤
            start = g.start_cash if g.start_cash > 0 else _b.float(values[0])
            total = values[-1] / start - 1.0
            cagr = (values[-1] / start) ** (1.0 / span) - 1.0
            sharpe = returns.mean() / returns.std(ddof=1) * _b.float(np.sqrt(250)) if returns.std(ddof=1) > 0 else 0.0
            path = np.concatenate([[start], frame['value'].values])
            maxdd = -_b.max(1.0 - path[1:] / np.maximum.accumulate(path)[1:])
            wins = _b.sum(1 for t in g.trades if t[6] > 0)
            _emit('[AUDIT]SUM init=%.2f total=%.4f cagr=%.4f sharpe=%.4f maxdd=%.4f days=%d trades=%d wins=%d span=%.2f'
                  % (start, total, cagr, sharpe, maxdd, _b.len(g.daily), _b.len(g.trades),
                     wins, span))
        _emit('[AUDIT]END ok')
    except Exception as exc:
        _emit('[AUDIT]ERR summary %s' % exc)
