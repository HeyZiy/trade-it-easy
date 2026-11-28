# -*- coding: utf-8 -*-
"""聚宽单文件回测。对照组为同目录 pool_rotation_ma20.py。
两组均用日频、2016-01-04 至 2026-09-01、100 万元、中证 800。
持仓后新披露的滚动基本 EPS 同比持平或下降，在该报告有效期内才允许强者替换。
普通空位优先。只有满仓、原前六名中有更强且可买的新股时，才尝试成对卖买。

策略收益
332.26%
策略年化收益
15.17%
超额收益
269.10%
基准收益
17.11%
阿尔法
0.130
贝塔
0.726
夏普比率
0.554
胜率
0.615
盈亏比
2.305
最大回撤 
29.75%
索提诺比率
0.803
日均超额收益
0.06%
超额收益最大回撤
34.33%
超额收益夏普比率
0.613
日胜率
0.512
盈利次数
72
亏损次数
45
信息比率
0.886
策略波动率
0.202
基准波动率
0.194
最大回撤区间
2024/05/14,2025/04/09
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
TREND_CLOSES = 20
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
    g.entry_dates = {}
    g.eps_cache = {}
    g.reports = {}
    g.fill_pairs = []
    g.unconfirmed_buys = {}
    g.eps_counts = {'reviewed': 0, 'unknown': 0, 'weak': 0, 'data_errors': 0,
                    'sale_incomplete': 0, 'buy_unfilled': 0}
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
    for code in g.entry_dates.keys() & set(codes):
        if visible.at[code]:
            g.reports[code] = (_report_date(financial.at[code, 'statDate']),
                               pd.Timestamp(financial.at[code, 'pubDate']).normalize())
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


def _rank_entries(asof, codes):
    if not codes:
        return [], {}
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
    vol_window = prices[-VOLATILITY_CLOSES:]
    valid_vol = (np.isfinite(vol_window) & (vol_window > 0)).all(axis=0)
    vol_window = vol_window[:, valid_vol]
    returns = vol_window[1:] / vol_window[:-1] - 1.0
    volatility = returns.std(axis=0, ddof=1) * _math.sqrt(250) * 100.0
    eligible = valid_vol.copy()
    eligible[valid_vol] = volatility < VOLATILITY_MAX
    momentum = prices[:-SKIP_RECENT_CLOSES]
    valid = (np.isfinite(momentum) & (momentum > 0)).all(axis=0)
    scores = _b.dict(_b.zip(closes.columns[valid], momentum[-1, valid] / momentum[0, valid] - 1.0))
    ranked = _b.sorted(closes.columns[eligible & valid], key=lambda code: (-scores[code], code))[:MAX_HOLDINGS]
    if not ranked:
        return ranked, scores
    recent = prices[-(TREND_CLOSES + 1):, closes.columns.get_indexer(ranked)]
    ma20 = recent[1:].mean(axis=0)
    # 两期均线共享 19 个收盘价，方向取决于移入价和移出价。
    weakening = (recent[-1] < ma20) & (recent[-1] < recent[0])
    return [code for code, blocked in _b.zip(ranked, weakening) if not blocked], scores


def _update_exits(held, passed, failed):
    for code in held & passed:
        if g.exit_pending.pop(code, None) is not None:
            log.info('%s 恢复持仓资格，撤销退出' % code)
    for code in _b.sorted(held & set(failed)):
        if code not in g.exit_pending:
            g.exit_pending[code] = failed[code]
            log.info('%s 确认出池：%s' % (code, failed[code]))


def _review_holdings(asof, universe, held):
    try:
        passed, failed = _quality(asof, universe, held)
    except Exception as exc:
        log.info('持仓快照异常：%s' % exc)
        return None
    _update_exits(held, passed, failed)
    return passed


def _eps_windows(report):
    """累计基本 EPS 跨年滚动口径。本年同期 + 上年全年 - 上年同期。"""
    def terms(end):
        if end.month == 12:
            return [(end, 1)]
        return [(end, 1), (pd.Timestamp(end.year - 1, 12, 31), 1),
                (end - pd.DateOffset(years=1), -1)]
    return terms(report), terms(report - pd.DateOffset(years=1))


def _rolling_eps(rows, actions, report, cutoff):
    # 定期报告的上年比较栏可能已经追溯调整。各条 EPS 的股数口径以该行的披露日为准，
    # 只调整此后实施的送转。现金分红、增发不用于还原旧 EPS。
    dates = rows.copy()
    for field in ('start_date', 'end_date', 'pub_date'):
        dates[field] = pd.to_datetime(dates[field], errors='coerce')
    dates = dates[(dates.pub_date <= cutoff) & (dates.end_date <= dates.pub_date)
                  & (dates.start_date.dt.year == dates.end_date.dt.year)
                  & (dates.start_date.dt.month == 1) & (dates.start_date.dt.day == 1)]
    # 只接受指定新报告的本期数，避免用比较栏或滞后的旧报代替触发报告。
    if dates[(dates.end_date == report) & (dates.report_type == 0)
             & (dates.pub_date == cutoff)].empty:
        return None
    windows = _eps_windows(report)
    values = {}
    for end in {end for terms in windows for end, sign in terms}:
        period = dates[dates.end_date == end]
        if period.empty:
            return None
        pub = period.pub_date.max()
        raw = pd.to_numeric(period.loc[period.pub_date == pub, 'basic_eps'], errors='coerce')
        if not np.isfinite(raw).all() or raw.nunique() != 1:
            return None
        later = actions[(actions.a_xr_date > pub) & (actions.a_xr_date <= cutoff)]
        if later.a_xr_date.duplicated().any():
            return None
        ratios = later[['dividend_ratio', 'transfer_ratio']].apply(pd.to_numeric, errors='coerce')
        if (later[['dividend_ratio', 'transfer_ratio']].notna() & ratios.isna()).any().any():
            return None
        ratios = ratios.fillna(0.0).astype(_b.float)
        if not np.isfinite(ratios.values).all() or (ratios < 0).any().any():
            return None
        values[end] = raw.iloc[0] / (1 + ratios.sum(axis=1) / 10).prod()
    return _b.tuple(_b.round(_b.sum(sign * values[end] for end, sign in terms), 8)
                    for terms in windows)


def _read_eps(code, report, cutoff):
    from jqdata import finance
    ends = {end.date() for terms in _eps_windows(report) for end, sign in terms}
    table = finance.STK_INCOME_STATEMENT
    rows = finance.run_query(query(table.start_date, table.end_date, table.pub_date,
                                   table.report_type, table.basic_eps).filter(
        table.code == code, table.source_id == 321003, table.end_date.in_(_b.sorted(ends)),
        table.pub_date <= cutoff.date()))
    xr = finance.STK_XR_XD
    actions = finance.run_query(query(xr.a_xr_date, xr.dividend_ratio, xr.transfer_ratio).filter(
        xr.code == code, xr.a_xr_date > _b.min(ends), xr.a_xr_date <= cutoff.date()))
    actions['a_xr_date'] = pd.to_datetime(actions.a_xr_date)
    return _rolling_eps(rows, actions, report, cutoff)


def _weak_holdings(passed):
    weak = {}
    for code in _b.sorted(passed & g.entry_dates.keys() & g.reports.keys()):
        report, pub = key = g.reports[code]
        if pub <= g.entry_dates[code]:
            continue
        cached = g.eps_cache.get(code)
        if cached is None or cached[0] != key:
            try:
                values = _read_eps(code, report, pub)
            except Exception as exc:
                # 不把接口或权限错误缓存成财务事实。隔日重试，日志保留首次错误。
                if not g.eps_counts['data_errors']:
                    log.info('[EPS数据异常] %s %s' % (code, exc))
                g.eps_counts['data_errors'] += 1
                continue
            g.eps_cache[code] = key, values
            g.eps_counts['reviewed'] += 1
            if values is None:
                g.eps_counts['unknown'] += 1
            elif values[0] <= values[1]:
                g.eps_counts['weak'] += 1
        values = g.eps_cache[code][1]
        if values is not None and values[0] <= values[1]:
            weak[code] = values
    return weak


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
    g.entry_dates = {c: d for c, d in g.entry_dates.items() if c in held}
    g.eps_cache = {c: v for c, v in g.eps_cache.items() if c in held}
    g.reports = {}
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

    reviewed = None
    if _b.len(held) >= MAX_HOLDINGS:
        reviewed = _review_holdings(asof, universe, held)
        if (reviewed is not None and _b.len(held - set(g.exit_pending)) >= MAX_HOLDINGS
                and not _weak_holdings(reviewed)):
            return

    try:
        passed, failed = _quality(asof, universe, universe | held)
    except Exception as exc:
        if held and reviewed is None:
            _review_holdings(asof, universe, held)
        log.info('全市场快照异常：%s' % exc)
        return
    _update_exits(held, passed, failed)
    kept = held - set(g.exit_pending)
    try:
        weak = _weak_holdings(kept & passed)
        ranked, scores = _rank_entries(asof, passed)
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
    g.entry_plan = (today, entries, industries, [c for c in ranked if c not in held],
                    weak, scores)


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


def _shares_for(price, budget, cash):
    estimated = price * (1 + SLIPPAGE_SPREAD / 2)
    affordable = _b.min(cash / (estimated * (1 + COMMISSION)), (cash - MIN_COMMISSION) / estimated)
    return _b.int(_b.min(budget / price, affordable) // 100) * 100


def _enter(code, shares, today):
    order = _order_target(code, shares)
    if order is not None:
        # 次日按真实持仓清理。异步成交也保留本次委托的日期。
        g.entry_dates[code] = today
    return order


def _replace(context, today, candidates, weak, scores, industries, quotes, budget):
    portfolio = context.portfolio
    for new in candidates:
        held = _held(context)
        if _b.len(held) != MAX_HOLDINGS or not weak:
            return
        pending = {o.security for o in get_open_orders().values()}
        if new in held or new in pending:
            continue
        buy_price = _tradable_price(quotes, new, buying=True)
        if buy_price is None:
            continue
        alternatives = [c for c in (weak.keys() & held) - pending
                        if c in scores and scores[c] < scores[new]
                        and _industry_available(new, held - {c}, industries)]
        for old in _b.sorted(alternatives, key=lambda c: (scores[c], c)):
            pos = portfolio.positions[old]
            sell_price = _tradable_price(quotes, old, buying=False)
            if (sell_price is None or pos.closeable_amount < pos.total_amount
                    or _b.getattr(pos, 'locked_amount', 0) > 0):
                continue
            proceeds = sell_price * (1 - SLIPPAGE_SPREAD / 2) * pos.total_amount
            cash = (portfolio.available_cash + proceeds * (1 - g.sell_tax)
                    - _b.max(MIN_COMMISSION, proceeds * COMMISSION))
            if _shares_for(buy_price, budget, cash) < 100:
                continue
            sold = _order_target(old, 0)
            eps, prior = weak.pop(old)
            if old in _held(context) or sold is None:
                g.eps_counts['sale_incomplete'] += 1
                return
            buy_price = _tradable_price(quotes, new, buying=True)
            shares = _shares_for(buy_price, budget, portfolio.available_cash) if buy_price else 0
            buy = _enter(new, shares, today) if shares >= 100 else None
            if buy is None:
                g.eps_counts['buy_unfilled'] += 1
                log.info('[EPS替换未配对] %s 卖出，%s 买入未成交' % (old, new))
                return
            if not buy.filled:
                g.unconfirmed_buys[new] = (today.date(), old, new)
                return
            g.fill_pairs.append((today.date(), old, new))
            log.info('[EPS替换] %s %s -> %s | 报告=%s 披露=%s EPS=%.4f 上年=%.4f | '
                     '动量 %.2f%% -> %.2f%% | 实际卖%d股 买%d股' %
                     (today.date(), old, new, g.reports[old][0].date(), g.reports[old][1].date(),
                      eps, prior, scores[old] * 100, scores[new] * 100, sold.filled, buy.filled))
            break


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

    _, entries, industries, candidates, weak, scores = plan
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
        shares = _shares_for(price, budget, portfolio.available_cash)
        if shares >= 100:
            _enter(code, shares, today)
    if weak and not g.exit_pending and not get_open_orders():
        _replace(context, today, candidates, weak, scores, industries, quotes, budget)


def after_trading_end(context):
    held = _held(context)
    for new, pair in g.unconfirmed_buys.items():
        if new in held:
            g.fill_pairs.append(pair)
            log.info('[EPS替换收盘确认] %s %s -> %s' % pair)
        else:
            g.eps_counts['buy_unfilled'] += 1
            log.info('[EPS替换未配对] %s %s -> %s 买入未成交' % pair)
    g.unconfirmed_buys.clear()


def on_strategy_end(context):
    # 辅助归因统一用换仓日开盘至第 60 个交易日收盘的后复权回报。
    # 停牌端点和样本末端不补价。账户净值仍由聚宽按实际撮合及费用计算。
    end = context.current_dt.date()
    spreads = []
    for day, old, new in g.fill_pairs:
        days = get_trade_days(start_date=day, end_date=end)
        if _b.len(days) < 60:
            continue
        last = pd.Timestamp(days[59]).date()
        try:
            bars = get_price([old, new], start_date=day, end_date=last, frequency='daily',
                             fields=['open', 'close', 'paused'], fq='post', skip_paused=False, panel=False)
            bars['day'] = pd.to_datetime(bars.time).dt.date
            a = bars[bars.day == day].set_index('code').reindex([old, new])
            b = bars[bars.day == last].set_index('code').reindex([old, new])
            if not (a.paused.eq(0) & b.paused.eq(0) & a.open.gt(0) & b.close.gt(0)).all():
                continue
            ret = b.close / a.open - 1
            spread = ret[new] - ret[old]
            spreads.append(spread)
            log.info('[EPS配对60日] %s 至 %s | %s %.2f%% -> %s %.2f%% | 差 %+.2f个百分点' %
                     (day, last, old, ret[old] * 100, new, ret[new] * 100, spread * 100))
        except Exception as exc:
            log.info('[EPS配对缺数] %s %s %s %s' % (day, old, new, exc))
    log.info('[EPS汇总] 财报=%d 其中不可比=%d 持平或下降=%d 接口异常次数=%d '
             '卖出未完成=%d 买入未成交=%d 实际配对=%d 满60日可比=%d '
             '新胜旧=%d 平均差=%s个百分点' % (
                 g.eps_counts['reviewed'], g.eps_counts['unknown'], g.eps_counts['weak'],
                 g.eps_counts['data_errors'], g.eps_counts['sale_incomplete'], g.eps_counts['buy_unfilled'],
                 _b.len(g.fill_pairs), _b.len(spreads), _b.sum(x > 0 for x in spreads),
                 '%.2f' % (np.mean(spreads) * 100) if spreads else 'NA'))
