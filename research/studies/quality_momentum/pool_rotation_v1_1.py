# -*- coding: utf-8 -*-
"""
确认出池就清仓，从通过全部准入条件的股票中按 12-1 强弱分数取前六名补位。60 日波动率只筛新仓，
旧仓权重随价格漂移。每只新仓的预算为执行开始时总权益的六分之一。
新仓和留存仓都要求短期趋势分严格为正。用截至昨收的 26 根复权收盘，
按 ETF rotation 的加权回归、R² 和近三日急跌置零规则打分。
比较区间：日频，2016-01-04 至 2026-09-01，100 万元，中证 800。


策略收益
-0.16%
策略年化收益
-0.02%
超额收益
-14.75%
基准收益
17.11%
阿尔法
-0.025
贝塔
0.619
夏普比率
-0.194
胜率
0.356
盈亏比
1.059
最大回撤 
56.91%
索提诺比率
-0.276
日均超额收益
0.00%
超额收益最大回撤
57.60%
超额收益夏普比率
-0.300
日胜率
0.494
盈利次数
386
亏损次数
697
信息比率
-0.084
策略波动率
0.207
基准波动率
0.194
最大回撤区间
2021/09/09,2025/03/03
"""
from jqdata import *
from jqfactor import get_factor_values
import builtins as _b
import math as _math

import numpy as np
import pandas as pd


MAX_HOLDINGS = 6
INDUSTRY_CAP = 2
ROE_MIN = 12.0
GROWTH_MIN = 10.0
PE_MAX = 30.0
PB_MAX = 5.0
VOLATILITY_MAX = 35.0
VOLATILITY_CLOSES = 61
MOMENTUM_CLOSES = 253
SKIP_RECENT_CLOSES = 21
TREND_CLOSES = 26
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
    _set_costs(context)
    run_daily(on_signal, time='08:00')
    run_daily(on_open, time='09:31')


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


def _quality(asof, universe, requested):
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
        'profit_growth_low': values.growth.le(GROWTH_MIN),
        'pe_nonpos': values.pe_ratio.le(0), 'pe_high': values.pe_ratio.ge(PE_MAX),
        'pb_nonpos': values.pb_ratio.le(0), 'pb_high': values.pb_ratio.ge(PB_MAX),
        'st': st.eq(True),
    })
    out = violations.any(axis=1)
    for code, row in violations.loc[out].iterrows():
        failed[code] = '|'.join(row.index[row])
    passed = set(values.index[~out & values.notna().all(axis=1) & st.notna()])
    return passed, failed


def _price_history(asof, codes, count):
    ordered = _b.sorted(codes)
    closes = history(count, '1d', 'close', security_list=ordered, skip_paused=False, df=True)
    expected = pd.DatetimeIndex(get_trade_days(end_date=asof.date(), count=count)).normalize()
    if (_b.len(expected) != count or not _b.isinstance(closes, pd.DataFrame)
            or not codes <= set(closes.columns)
            or not pd.DatetimeIndex(closes.index).normalize().equals(expected)):
        raise ValueError('Daily close matrix cutoff does not match T-1')
    return closes[ordered].apply(pd.to_numeric, errors='coerce').astype(_b.float)


def calculate_momentum_score(prices):
    """取 26 根已完成收盘，计算年化加权趋势乘以加权 R²。"""
    if _b.len(prices) != TREND_CLOSES or not (np.isfinite(prices) & (prices > 0)).all():
        return np.nan
    if (prices == prices[0]).all() or (prices[-3:] / prices[-4:-1] < .95).any():
        return 0.0
    y = np.log(prices)
    x = np.arange(TREND_CLOSES)
    w = np.linspace(1.0, 2.0, TREND_CLOSES)
    slope, intercept = np.polyfit(x, y, 1, w=w)
    effective_w = w ** 2
    residual = y - (slope * x + intercept)
    ss_res = np.sum(effective_w * residual ** 2)
    ss_tot = np.sum(effective_w * (y - np.average(y, weights=effective_w)) ** 2)
    if ss_tot == 0:
        return 0.0
    return _math.expm1(slope * 250) * np.clip(1 - ss_res / ss_tot, 0.0, 1.0)


def _rank_entries(asof, codes):
    if not codes:
        return []
    closes = _price_history(asof, codes, MOMENTUM_CLOSES)
    prices = closes.values
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
    recent = closes.iloc[-TREND_CLOSES:]
    scores = {code: score for code, score in _b.zip(columns[valid], momentum[-1, valid] / momentum[0, valid] - 1.0)
              if calculate_momentum_score(recent[code].values) > 0}
    return _b.sorted(scores, key=lambda code: (-scores[code], code))[:MAX_HOLDINGS]


def _holding_scores(asof, codes):
    if not codes:
        return {}
    try:
        closes = _price_history(asof, codes, TREND_CLOSES)
    except Exception as exc:
        log.info('持仓趋势数据异常：%s' % exc)
        return {}
    return {code: calculate_momentum_score(closes[code].values) for code in closes.columns}


def _update_exits(held, passed, failed, scores):
    for code in held & passed:
        if not scores.get(code, np.nan) > 0:
            continue
        if g.exit_pending.pop(code, None) is not None:
            log.info('%s 恢复持仓资格，撤销退出' % code)
    failed = _b.dict(failed)
    for code, score in scores.items():
        if score <= 0:
            failed.setdefault(code, 'momentum_nonpositive')
    for code in _b.sorted(held & set(failed)):
        if code not in g.exit_pending:
            g.exit_pending[code] = failed[code]
            log.info('%s 确认出池：%s' % (code, failed[code]))


def _review_holdings(asof, universe, held, scores):
    try:
        passed, failed = _quality(asof, universe, held)
    except Exception as exc:
        log.info('持仓快照异常：%s' % exc)
        _update_exits(held, set(), {}, scores)
        return False
    _update_exits(held, passed, failed, scores)
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

    scores = _holding_scores(asof, held & universe)
    reviewed = _b.len(held) >= MAX_HOLDINGS and _review_holdings(asof, universe, held, scores)
    if reviewed and _b.len(held - set(g.exit_pending)) >= MAX_HOLDINGS:
        return

    try:
        passed, failed = _quality(asof, universe, universe | held)
    except Exception as exc:
        if held and not reviewed:
            _review_holdings(asof, universe, held, scores)
        log.info('全市场快照异常：%s' % exc)
        return
    _update_exits(held, passed, failed, scores)
    kept = held - set(g.exit_pending)
    try:
        if _b.len(kept) >= MAX_HOLDINGS:
            return
        ranked = _rank_entries(asof, passed)
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


def on_open(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_execution_day == today:
        return
    g.last_execution_day = today
    _set_costs(context)
    plan, g.entry_plan = g.entry_plan, None
    if plan is not None and plan[0] != today:
        plan = None
    if plan is None and not g.exit_pending:
        return
    try:
        quotes = get_current_data()
    except Exception as exc:
        log.info('执行行情暂停：%s' % exc)
        return
    portfolio = context.portfolio
    budget = portfolio.total_value / MAX_HOLDINGS
    open_orders = get_open_orders()
    open_codes = {order.security for order in open_orders.values()}
    for code in _b.sorted((_held(context) & set(g.exit_pending)) - open_codes):
        pos = portfolio.positions[code]
        if pos.closeable_amount > 0 and _tradable_price(quotes, code, buying=False) is not None:
            _order_target(code, _b.max(0, pos.total_amount - pos.closeable_amount))
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
