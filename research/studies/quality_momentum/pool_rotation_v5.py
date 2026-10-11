# -*- coding: utf-8 -*-
"""质量池v5定稿：盘前信号、当早执行，不追补买入。

整文件粘贴聚宽，统一用分钟频率、100万元、2016-01-04~2026-09-01、中证800。
EXPERIMENT='fixed'：共享修复 + 原T日14:55信号/T+1日09:31执行。
EXPERIMENT='clock'：相对fixed只改为08:00取昨收因子，当日09:31执行（现行规则）。
EXPERIMENT='buy_retry'：相对clock只增加执行日09:32~09:35补买；预算/名单冻结，
                    不重做减仓，不跨日追买。该窗口是实验条件，尚未采纳。

共享修复：数据未知不制造退出；停牌只限制交易；缺旧仓强弱不阻断整轮；
行业缺失不绕过上限；全批数据失败翌日重算，质量未知的旧仓每日补判；
买卖按实际持仓/现金/在途委托收敛。已确认的待退出仓不会因新数据未知撤销。
保留6只、前6名不顺延、行业新仓最多2只、20交易日周期、留存仓差额再平衡；
ROE_TTM>12%、净利同比>10%、0<PE<30、0<PB<5、vol60<35%。
旧v4_1_2及其日频成绩保留为历史对照，不能跨频率归因比较。
以下为用户提供的平台结果摘要，按本文件默认clock模式归档。
脚本预设区间/资金/分钟频率见上文；实际运行设置及7张过程CSV未留存，未独立复算。

策略收益
207.03%
策略年化收益
11.43%
超额收益
162.17%
基准收益
17.11%
阿尔法
0.091
贝塔
0.662
夏普比率
0.394
胜率
0.619
盈亏比
1.590
最大回撤 
24.71%
索提诺比率
0.558
日均超额收益
0.04%
超额收益最大回撤
38.63%
超额收益夏普比率
0.376
日胜率
0.511
盈利次数
159
亏损次数
98
信息比率
0.648
策略波动率
0.189
基准波动率
0.194
最大回撤区间
2024/06/04,2024/11/22
"""
from jqdata import *
from jqfactor import get_factor_values
import builtins as _b
import math as _math
import pandas as pd

EXPERIMENT = 'clock'  # v5定稿；fixed / buy_retry仅保留为对照入口，尚无结果


TOP_N = 6
MOMENTUM_DAYS = 252
SKIP_RECENT_DAYS = 21   # 12-2动量：跳过最近21个交易日
INDUSTRY_CAP = 2        # 目标组合内同一申万一级行业最多2只
ROTATE_EVERY = 20
ROE_MIN = 12.0
ROE_FACTOR_UNIT = 'ratio'  # ratio: 0.12=12%；percent: 12=12%。上线前核对返回单位，不按中位数猜测。
NP_YOY_MIN = 10.0
PE_MAX = 30.0
PB_MAX = 5.0
VOL_LOOKBACK = 61
VOL60_MAX = 35.0
LIMIT_PAD = 0.001          # 元，涨跌停价格比较容差
FEE_COMMISSION = 0.00025
MIN_COMMISSION = 5.0
SLIPPAGE_SPREAD = 0.002    # 聚宽价差参数，买/卖各为此值的一半
SELL_TAX_BEFORE = 0.001
SELL_TAX_AFTER = 0.0005
TAX_CHANGE_DATE = '2023-08-28'
BENCHMARK = '000906.XSHG'
ANNUAL_DAYS = 245.0
OUTPUT_PREFIX = 'pool_rotation_v5_' + EXPERIMENT
VERBOSE_LOGS = False       # True时显示逐笔委托、调仓信号及买卖受阻日志
DECISION_COLUMNS = ['signal_day', 'asof', 'code', 'action', 'exit_reason', 'pool_reason',
                    'rank', 'previous_rank', 'momentum', 'entry_date', 'held_days_at_signal',
                    'roe_ann', 'profit_yoy', 'pe', 'pb', 'vol60', 'industry_code',
                    'industry_name', 'stat_date', 'pub_date', 'data_error']
HOLDING_COLUMNS = ['code', 'entry_date', 'exit_date', 'holding_days', 'entry_source',
                   'closure_source', 'signal_day', 'exit_reason', 'pool_reason']

ROE_TTM_MIN = ROE_MIN
SCORE_CLOSES = MOMENTUM_DAYS + 1
SCORE_SKIP_RECENT = SKIP_RECENT_DAYS
VOL_ANNUAL_DAYS = 250.0
DECISION_COLUMNS += ['pool_state']


class PoolDataError(Exception):
    pass

def initialize(context):
    if EXPERIMENT not in ('fixed', 'clock', 'buy_retry'):
        raise ValueError('Unknown EXPERIMENT')
    if ROE_FACTOR_UNIT not in ('ratio', 'percent'):
        raise ValueError('ROE_FACTOR_UNIT must be ratio or percent')
    for name, value in (('TOP_N', TOP_N), ('MOMENTUM_DAYS', MOMENTUM_DAYS),
                        ('ROTATE_EVERY', ROTATE_EVERY), ('INDUSTRY_CAP', INDUSTRY_CAP)):
        if _b.type(value) is not _b.int or value <= 0:
            raise ValueError('%s must be a positive integer' % name)
    if (not 0 <= SLIPPAGE_SPREAD < 1 or FEE_COMMISSION < 0 or MIN_COMMISSION < 0):
        raise ValueError('Invalid execution cost parameters')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    log.set_level('order', 'info' if VERBOSE_LOGS else 'error')
    set_benchmark(BENCHMARK)
    set_slippage(PriceRelatedSlippage(SLIPPAGE_SPREAD))
    g.day = 0
    g.pending = None
    g.signal_retry = False
    g.deferred_codes = set()
    g.no_adjust = set()
    g.industries = {}
    g.active_buy_plan = None
    g.target = []
    g.exit_pending = {}      # code -> 退出原因，直到实际持仓清零
    g.pool_size = 0
    g.ranked_size = 0
    g.data_paused = False
    g.last_open_day = None
    g.last_signal_day = None
    g.last_close_day = None
    g.tax_rate = None
    g.initial_equity = context.portfolio.total_value
    g.nav_rows = []
    g.signal_rows = []
    g.member_rows = []
    g.event_rows = []
    g.order_rows = []
    g.decision_rows = []
    g.holding_rows = []
    g.previous_ranks = {}
    g.current_decisions = {}
    g.exit_details = {}
    g.order_tags = {}
    g.hold_entries = {}
    g.logged_alerts = set()
    _set_costs(context)
    run_daily(on_open, time='09:31')
    run_daily(on_signal, time='14:55' if EXPERIMENT == 'fixed' else '08:00')
    if EXPERIMENT == 'buy_retry':
        for minute in (32, 33, 34, 35):
            run_daily(on_buy_retry, time='09:%02d' % minute)
    log.info('[质量池v5] N=%d 留存=仍在质量池内即持有（无名次留存线） '
             '排序=12-2动量(跳过最近%d日的%d日收益) 申万一级行业上限%d只 '
             '每%d日重建；删除解禁过滤'
             % (TOP_N, SKIP_RECENT_DAYS, MOMENTUM_DAYS - SKIP_RECENT_DAYS,
                INDUSTRY_CAP, ROTATE_EVERY))
    log.info('[质量池v5] 对照=%s；分钟频率；预算只在执行开始冻结' % EXPERIMENT)

def _evaluate_pool(universe, fundamentals, status, closes, unlock_codes=None, audit=None):
    """停牌只限制成交，不定义质量出池；解禁不参与现行规则。

    audit.pool_state 为 passed / failed / unknown。确认失败优先于未知，
    因此已知ROE不达标时，即使估值缺失仍可正确退出。
    """
    pool = []
    tracked = _b.set(universe) | _b.set(audit or {})
    for code in _b.sorted(tracked):
        failed, missing = ([], [])
        values = {}
        if code not in universe:
            failed.append('not_in_mainboard_universe')
        for field, low, high, reason in (('roe_ttm_pct', ROE_TTM_MIN, None, 'roe_low'), ('np_yoy_pct', NP_YOY_MIN, None, 'profit_growth_low'), ('pe_ttm', 0, PE_MAX, 'pe_high'), ('pb', 0, PB_MAX, 'pb_high')):
            value = fundamentals.at[code, field] if code in fundamentals.index and field in fundamentals.columns else None
            try:
                value = _b.float(value)
            except (TypeError, ValueError):
                value = _b.float('nan')
            values[field] = value
            if not _math.isfinite(value):
                missing.append('financial_missing' if high is None else 'valuation_invalid')
            elif value <= low:
                failed.append(reason if high is None else field.split('_')[0] + '_nonpos')
            elif high is not None and value >= high:
                failed.append(reason)
        if code not in status.index or 'is_st' not in status.columns or pd.isna(status.at[code, 'is_st']):
            missing.append('status_missing')
        elif _b.bool(status.at[code, 'is_st']):
            failed.append('st')
        vol = _annualized_vol(closes[code]) if code in closes.columns else None
        if vol is None:
            missing.append('vol_history_missing')
        elif vol >= VOL60_MAX:
            failed.append('vol_high')
        state = 'failed' if failed else 'unknown' if missing else 'passed'
        if audit is not None:
            audit.setdefault(code, {}).update(values, pool_state=state, pool_reason='|'.join(dict.fromkeys(failed or missing)) or 'passed', vol60=vol)
        if state == 'passed':
            pool.append(code)
    return pool

def _annualized_vol(series):
    window = pd.to_numeric(series.iloc[-VOL_LOOKBACK:], errors='coerce')
    if _b.len(window) < VOL_LOOKBACK or window.isna().any() or (not window.map(_math.isfinite).all()) or (window <= 0).any():
        return None
    return _b.float(window.pct_change().iloc[1:].std(ddof=1) * _math.sqrt(VOL_ANNUAL_DAYS) * 100.0)

def rank_pool(pool, closes):
    """253根收盘剔除最近21根；同分按代码排序，缺计分历史仅影响新仓。"""
    scores = {}
    for code in pool:
        if code not in closes.columns:
            continue
        series = closes[code].iloc[-SCORE_CLOSES:-SCORE_SKIP_RECENT]
        if _b.len(series) == SCORE_CLOSES - SCORE_SKIP_RECENT and series.notna().all() and series.map(_math.isfinite).all() and (series > 0).all():
            scores[code] = _b.float(series.iloc[-1] / series.iloc[0] - 1.0)
    return (_b.sorted(scores, key=lambda c: (-scores[c], c)), scores)

def select_targets(ranked, held, pool, industries, deferred=frozenset()):
    """留存质量通过/暂缓判断的旧仓；只从前6名补位，未知行业不绕过约束。"""
    if _b.len(_b.set(ranked)) != _b.len(ranked):
        raise ValueError('Duplicate ranked code')
    rank = {c: i + 1 for i, c in _b.enumerate(ranked)}
    kept = _b.sorted(held & (_b.set(pool) | _b.set(deferred)), key=lambda c: (rank.get(c, _b.len(ranked) + 1), c))
    if _b.len(kept) > TOP_N:
        raise ValueError('More existing positions than TOP_N')
    selected = _b.list(kept)
    counts = {}
    if _b.any((not industries.get(c) for c in kept)):
        return (selected, kept)
    for code in kept:
        key = industries[code]
        counts[key] = counts.get(key, 0) + 1
    for code in ranked[:TOP_N]:
        if _b.len(selected) >= TOP_N:
            break
        key = industries.get(code)
        if code in selected or not key or counts.get(key, 0) >= INDUSTRY_CAP:
            continue
        selected.append(code)
        counts[key] = counts.get(key, 0) + 1
    return (selected, kept)


def build_pool(context, asof, audit=None, subset=None):
    secs = get_all_securities(['stock'], date=context.current_dt)
    if not _b.isinstance(secs, pd.DataFrame) or secs.empty:
        raise PoolDataError('Missing historical stock universe')
    universe = {c for c in secs.index if c[:2] in ('60', '00')}
    requested = universe if subset is None else set(subset)
    ind = get_fundamentals(query(indicator.code, indicator.inc_net_profit_year_on_year,
                                indicator.statDate, indicator.pubDate), date=asof)
    _require_columns(ind, ('code', 'inc_net_profit_year_on_year', 'statDate', 'pubDate'), 'financial')
    if ind.empty or ind['code'].duplicated().any():
        raise PoolDataError('Empty or duplicate financial snapshot')
    ind = ind.set_index('code')
    visible = ((ind['statDate'].apply(_report_date) <= asof)
               & (pd.to_datetime(ind['pubDate'], errors='coerce') <= asof))
    roe = _fetch_roe_ttm(requested & universe, asof) if requested & universe else pd.Series(dtype=float)
    fin = pd.DataFrame(index=sorted(requested))
    fin['roe_ttm_pct'] = roe
    fin['np_yoy_pct'] = pd.to_numeric(ind['inc_net_profit_year_on_year'], errors='coerce').where(visible)
    # 未知/未披露财报不能使用因子值冒充质量已知。
    fin.loc[~fin.index.isin(ind.index[visible]), 'roe_ttm_pct'] = _b.float('nan')
    val = get_fundamentals(query(valuation.code, valuation.pe_ratio, valuation.pb_ratio)
                           .filter(valuation.code.in_(_b.list(requested))), date=asof)
    _require_columns(val, ('code', 'pe_ratio', 'pb_ratio'), 'valuation')
    if val['code'].duplicated().any():
        raise PoolDataError('Duplicate valuation snapshot')
    val = val.set_index('code')
    fin['pe_ttm'] = pd.to_numeric(val['pe_ratio'], errors='coerce')
    fin['pb'] = pd.to_numeric(val['pb_ratio'], errors='coerce')
    if not fin.notna().any().any():
        raise PoolDataError('No usable financial values')
    cd = get_current_data()
    status = pd.DataFrame({'is_st': {c: _b.getattr(cd[c], 'is_st', None) for c in requested}})
    held = _held(context) & requested
    candidates = {c for c in fin.index if fin.at[c, 'roe_ttm_pct'] > ROE_MIN
                  and fin.at[c, 'np_yoy_pct'] > NP_YOY_MIN
                  and 0 < fin.at[c, 'pe_ttm'] < PE_MAX and 0 < fin.at[c, 'pb'] < PB_MAX}
    history_codes = sorted((candidates | held) & universe)
    closes = _history_closes(history_codes, asof) if history_codes else pd.DataFrame()
    pool = _evaluate_pool(universe & requested, fin, status, closes, audit=audit)
    if audit is not None:
        for c, row in audit.items():
            row.update(roe_ann=row.get('roe_ttm_pct'), profit_yoy=row.get('np_yoy_pct'),
                       pe=row.get('pe_ttm'), pb=row.get('pb'))
            if c in ind.index:
                row.update(stat_date=_b.str(ind.at[c, 'statDate']), pub_date=_b.str(ind.at[c, 'pubDate']))
    return pool, closes


def _sw_industries(codes, asof):
    if not codes:
        return {}
    data = get_industry(_b.list(codes), date=asof)
    if not _b.isinstance(data, dict) or not data:
        raise PoolDataError('Missing industry snapshot')
    result = {}
    for code in codes:
        sw = (data.get(code) or {}).get('sw_l1') or {}
        result[code] = _b.str(sw.get('industry_code') or '')
    return result


def _review_deferred(context, asof):
    codes = g.deferred_codes & _held(context)
    audit = {c: {} for c in codes}
    if codes:
        try:
            build_pool(context, asof, audit=audit, subset=codes)
        except Exception as exc:
            _event(context, 'DATA_DEFER', detail='old holdings still unknown: %s' % exc)
            return
    unresolved = set()
    for code in codes:
        state = audit[code]['pool_state']
        if state == 'unknown':
            unresolved.add(code)
        elif state == 'failed':
            g.exit_pending[code] = 'out_of_pool'
            g.exit_details[code] = _decision(context, asof, code, 'exit', audit, exit_reason='out_of_pool')
        else:
            g.exit_pending.pop(code, None)
            g.exit_details.pop(code, None)
    g.deferred_codes = unresolved


def on_signal(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_signal_day == today:
        return
    g.last_signal_day = today
    g.day += 1
    asof = _prev_trade_day(context.current_dt)
    if (g.day - 1) % ROTATE_EVERY and not g.signal_retry:
        _review_deferred(context, asof)
        return
    audit = {c: {} for c in _held(context)}
    try:
        pool, closes = build_pool(context, asof, audit=audit)
        ranked, scores = rank_pool(pool, closes)
        held = _held(context)
        deferred = {c for c in held if audit[c]['pool_state'] == 'unknown'}
        industries = _sw_industries(set(ranked) | held, asof)
        protected = deferred - set(g.exit_pending)
        selected, kept = select_targets(ranked, held, set(pool), industries, protected)
    except Exception as exc:
        g.pending = None
        g.data_paused = True
        g.signal_retry = True
        for code in sorted(_held(context)):
            _decision(context, asof, code, 'data_pause', audit, data_error=_b.str(exc))
        _event(context, 'DATA_PAUSE', detail='%s; retry next session' % exc)
        return
    g.signal_retry = False
    g.data_paused = False
    g.deferred_codes = deferred
    rank = {c: i + 1 for i, c in _b.enumerate(ranked)}
    exits = {c: 'out_of_pool' for c in held if audit[c]['pool_state'] == 'failed'}
    exits.update({c: g.exit_pending[c] for c in deferred & set(g.exit_pending)})
    no_adjust = deferred | {c for c in kept if not industries.get(c)}
    decisions = {}
    for code in sorted(held | set(selected)):
        row = audit.setdefault(code, {})
        row['industry_code'] = industries.get(code, '')
        action = 'exit' if code in exits else 'defer' if code in no_adjust else 'keep' if code in held else 'entry'
        decisions[code] = _decision(context, asof, code, action, audit,
                                   rank=rank.get(code), score=scores.get(code), exit_reason=exits.get(code, ''))
    g.pool_size, g.ranked_size = _b.len(pool), _b.len(ranked)
    g.pending = dict(signal_day=today, asof=asof, selected=selected, exit_reasons=exits,
                     decisions=decisions, exit_details={c: decisions[c] for c in exits},
                     deferred=no_adjust, industries=industries)
    entry_not_selected = [c for c in ranked[:TOP_N] if c not in selected]
    g.signal_rows.append(dict(signal_day=_b.str(today.date()), asof=_b.str(asof.date()),
        pool_size=_b.len(pool), ranked_size=_b.len(ranked), kept=';'.join(kept), selected=';'.join(selected),
        entry_not_selected=';'.join(entry_not_selected), planned_exits=_b.len(exits), deferred=_b.len(deferred)))
    if no_adjust:
        _event(context, 'DATA_DEFER', detail='hold without adjustment: %s' % ';'.join(sorted(no_adjust)))
    for code in pool:
        row = dict(audit[code])
        row.update(signal_day=_b.str(today.date()), code=code, momentum=scores.get(code),
                   rank=rank.get(code), selected=code in selected, kept=code in kept,
                   entry_not_selected=code in entry_not_selected,
                   ranking_status='ranked' if code in scores else 'momentum_history_missing')
        g.member_rows.append(row)
    g.previous_ranks = rank
    _event(context, 'SIGNAL', detail='pool=%d target=%d deferred=%d' % (_b.len(pool), _b.len(selected), _b.len(deferred)))


def _industry_buy_block(code, held):
    if code in held:
        return ''
    key = g.industries.get(code)
    if not key or _b.any(not g.industries.get(c) for c in held):
        return 'industry_unknown'
    return 'industry_cap' if _b.sum(g.industries[c] == key for c in held) >= INDUSTRY_CAP else ''


def _buy_targets(context, cd, per):
    open_orders = get_open_orders()
    open_codes = {o.security for o in open_orders.values()}
    reserved = _held(context) | {o.security for o in open_orders.values() if o.is_buy}
    for code in g.target:
        if code in open_codes or code in g.no_adjust or code in g.exit_pending:
            continue
        px = _price(cd, code)
        blocked = _quote_block(cd, code, 'buy')
        if not blocked:
            blocked = _industry_buy_block(code, reserved)
        if blocked:
            _event(context, 'BUY_BLOCKED', code, blocked)
            continue
        positions = context.portfolio.positions
        pos = positions[code] if code in positions.keys() else None
        current = _b.int(pos.total_amount) if pos is not None else 0
        desired = _b.int(per / px // 100) * 100
        if desired <= current:
            continue
        if current == 0 and code not in reserved and _b.len(reserved) >= TOP_N:
            _event(context, 'BUY_BLOCKED', code, 'pending exit occupies slot')
            continue
        delta = _b.min(desired - current, _affordable_shares(context.portfolio.available_cash, px))
        delta = _b.int(delta // 100) * 100
        if delta < 100:
            _event(context, 'BUY_BLOCKED', code, 'cash/lot')
            continue
        if _submit(context, code, current + delta, 'equal_weight') is not None:
            reserved = _held(context) | {o.security for o in get_open_orders().values() if o.is_buy}


def on_open(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_open_day == today:
        return
    g.last_open_day = today
    g.active_buy_plan = None
    _set_costs(context)
    plan, g.pending = g.pending, None
    expected = _prev_trade_day(context.current_dt) if EXPERIMENT == 'fixed' else today
    if plan is not None and plan['signal_day'] != expected:
        _event(context, 'STALE_SIGNAL', detail=_b.str(plan['signal_day'].date()))
        plan = None
    cd = get_current_data()
    if plan is not None:
        g.target = plan['selected']
        g.no_adjust = set(plan['deferred'])
        g.industries = plan['industries']
        g.exit_pending = plan['exit_reasons']
        g.exit_details = plan['exit_details']
        g.current_decisions = plan['decisions']
        per = context.portfolio.total_value / TOP_N
    open_codes = {o.security for o in get_open_orders().values()}
    for code, reason in sorted(g.exit_pending.items()):
        if code in _held(context) and code not in open_codes:
            _sell(context, cd, code, 0, reason)
    g.exit_pending = {c: r for c, r in g.exit_pending.items() if c in _held(context)}
    if plan is None:
        return
    for code in g.target:
        if code in g.no_adjust or code in open_codes:
            continue
        if code in _held(context):
            px = _price(cd, code)
            if px is not None:
                current = _b.int(context.portfolio.positions[code].total_amount)
                reduction = _b.int(_b.max(0, current - per / px) // 100) * 100
                if reduction:
                    _sell(context, cd, code, current - reduction, 'equal_weight')
    _buy_targets(context, cd, per)
    if EXPERIMENT == 'buy_retry':
        g.active_buy_plan = dict(day=today, budget=per)


def on_buy_retry(context):
    plan = g.active_buy_plan
    if plan is None:
        return
    today = pd.Timestamp(context.current_dt).normalize()
    if today != plan['day'] or context.current_dt.strftime('%H:%M') > '09:35':
        g.active_buy_plan = None
        return
    _buy_targets(context, get_current_data(), plan['budget'])
    if context.current_dt.strftime('%H:%M') == '09:35':
        _event(context, 'BUY_WINDOW_CLOSED', detail='remaining entries expire; no next-day chase')
        g.active_buy_plan = None


def _set_costs(context):
    tax = (SELL_TAX_AFTER if pd.Timestamp(context.current_dt).normalize()
           >= pd.Timestamp(TAX_CHANGE_DATE) else SELL_TAX_BEFORE)
    if tax != g.tax_rate:
        set_order_cost(OrderCost(open_tax=0, close_tax=tax,
                                 open_commission=FEE_COMMISSION,
                                 close_commission=FEE_COMMISSION,
                                 min_commission=MIN_COMMISSION), type='stock')
        g.tax_rate = tax


def _event(context, kind, code='', detail=''):
    g.event_rows.append({'time': _b.str(context.current_dt), 'kind': kind,
                         'code': code, 'detail': detail})
    # 过程表保留所有事件；默认仅显示严重异常，同一异常只提醒一次。
    key = (kind, code, detail)
    alerts = ('DATA_PAUSE', 'DATA_DEFER', 'ORDER_ERROR', 'STALE_SIGNAL', 'BENCHMARK_MISSING')
    if VERBOSE_LOGS or (kind in alerts and key not in g.logged_alerts):
        log.info('[质量池v5] %s %s %s' % (kind, code, detail))
        g.logged_alerts.add(key)


def _prev_trade_day(dt):
    days = get_trade_days(end_date=pd.Timestamp(dt).date(), count=2)
    if _b.len(days) != 2:
        raise PoolDataError('Missing previous trading day')
    return pd.Timestamp(days[0]).normalize()


def _require_columns(rows, columns, source):
    if not _b.isinstance(rows, pd.DataFrame) or not set(columns) <= set(rows.columns):
        raise PoolDataError('Incomplete %s schema' % source)


def _report_date(value):
    if pd.isna(value):
        return pd.NaT
    text = _b.str(value).strip().lower()
    try:
        if (_b.len(text) == 6 and text[:4].isdigit()
                and text[4] == 'q' and text[5] in '1234'):
            return pd.Period(text, freq='Q').end_time.normalize()
        date = pd.Timestamp(value).normalize()
        return date if date.is_quarter_end else pd.NaT
    except (TypeError, ValueError, OverflowError):
        return pd.NaT


def _fetch_roe_ttm(codes, asof):
    """因子库roe_ttm（净利润TTM/期末股东权益）；按code返回百分比Series。"""
    data = get_factor_values(securities=_b.list(codes), factors=['roe_ttm'],
                             end_date=pd.Timestamp(asof).date(), count=1)
    if not _b.isinstance(data, dict) or 'roe_ttm' not in data:
        raise PoolDataError('Missing roe_ttm factor snapshot')
    frame = data['roe_ttm']
    if not _b.isinstance(frame, pd.DataFrame) or frame.empty:
        raise PoolDataError('Empty roe_ttm factor snapshot')
    if pd.Timestamp(frame.index[-1]).normalize() != pd.Timestamp(asof).normalize():
        raise PoolDataError('ROE cutoff mismatch')
    values = frame.iloc[-1].apply(pd.to_numeric, errors='coerce').replace(
        [_b.float('inf'), -_b.float('inf')], _b.float('nan')).dropna()
    if values.empty:
        raise PoolDataError('No valid roe_ttm values')
    # 固定的数据源口径，不能随当日分布或补判股票子集改变单位。
    return values * (100.0 if ROE_FACTOR_UNIT == 'ratio' else 1.0)


def _history_closes(codes, asof):
    count = _b.max(VOL_LOOKBACK, MOMENTUM_DAYS + 1)
    closes = history(count, '1d', 'close', security_list=codes,
                     skip_paused=False, df=True)
    if (not _b.isinstance(closes, pd.DataFrame)
            or not set(codes) <= set(closes.columns) or _b.len(closes) != count):
        raise PoolDataError('Incomplete daily close matrix')
    dates = pd.to_datetime(closes.index).normalize()
    expected = pd.DatetimeIndex(get_trade_days(end_date=pd.Timestamp(asof).date(), count=count)).normalize()
    if (dates.max() != pd.Timestamp(asof).normalize()
            or dates.duplicated().any() or not dates.is_monotonic_increasing
            or not dates.equals(expected)):
        raise PoolDataError('Daily close matrix cutoff does not match T-1')
    return closes[codes].apply(pd.to_numeric, errors='coerce').replace(
        [_b.float('inf'), -_b.float('inf')], _b.float('nan'))


def _held(context):
    # 卖单冻结的数量仍然占用一个持仓名额。
    return {c for c, p in context.portfolio.positions.items()
            if p.total_amount + _b.getattr(p, 'locked_amount', 0) > 0}


def _holding_info(code):
    entry = g.hold_entries.get(code)
    if entry is None:
        return '', None
    return entry['entry_date'], _b.len(g.nav_rows) - entry['entry_index']


def _decision(context, asof, code, action, audit, rank=None, score=None,
              exit_reason='', data_error=''):
    entry_date, age = _holding_info(code)
    row = dict(audit.get(code, {}))
    row.update(signal_day=_b.str(pd.Timestamp(context.current_dt).date()),
               asof=_b.str(asof.date()) if asof is not None else '', code=code,
               action=action, exit_reason=exit_reason, rank=rank, momentum=score,
               previous_rank=g.previous_ranks.get(code), entry_date=entry_date,
               held_days_at_signal=age, data_error=data_error)
    g.decision_rows.append(row)
    return row


def _price(cd, code):
    try:
        value = _b.float(cd[code].last_price)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    return value if _math.isfinite(value) and value > 0 else None


def _quote_block(cd, code, side):
    try:
        quote = cd[code]
        flags = (quote.paused, quote.is_st)
        high, low = _b.float(quote.high_limit), _b.float(quote.low_limit)
        if (_b.any(pd.isna(flag) for flag in flags)
                or not _math.isfinite(high) or not _math.isfinite(low)
                or not high > low > 0):
            return 'status/limits_unknown'
    except (TypeError, ValueError, KeyError, AttributeError):
        return 'status/limits_unknown'
    if quote.paused:
        return 'paused'
    if side == 'buy' and quote.is_st:
        return 'ST'
    price = _price(cd, code)
    if price is None:
        return 'invalid_price'
    if side == 'buy' and price >= high - LIMIT_PAD:
        return 'upper_limit'
    if side == 'sell' and price <= low + LIMIT_PAD:
        return 'lower_limit'
    return ''


def _submit(context, code, quantity, reason):
    try:
        order = order_target(code, quantity)
        _event(context, 'SUBMITTED' if order is not None else 'REJECTED', code,
               '%s target_shares=%d' % (reason, quantity))
        if order is not None:
            source = (g.exit_details.get(code, {}) if reason != 'equal_weight'
                      else g.current_decisions.get(code, {}))
            date = _b.str(pd.Timestamp(context.current_dt).date())
            _, age = _holding_info(code)
            g.order_tags[(date, code)] = {'reason': reason,
                'signal_day': source.get('signal_day', ''),
                'pool_reason': source.get('pool_reason', ''),
                'holding_days_at_order': age}
        return order
    except Exception as exc:
        _event(context, 'ORDER_ERROR', code, _b.type(exc).__name__)
        return None


def _sell(context, cd, code, target, reason):
    positions = context.portfolio.positions
    # 聚宽兼容容器的get可能读取缺失键并告警，先检查实际持仓键。
    pos = positions[code] if code in positions.keys() else None
    if pos is None or pos.total_amount <= target:
        return
    blocked = _quote_block(cd, code, 'sell')
    if blocked:
        _event(context, 'SELL_BLOCKED', code, '%s blocked_by=%s' % (reason, blocked))
        return
    if pos.closeable_amount <= 0:
        _event(context, 'SELL_BLOCKED', code, '%s blocked_by=no_closeable_shares' % reason)
        return
    amount = _b.max(target, _b.int(pos.total_amount - pos.closeable_amount))
    _submit(context, code, amount, reason)


def _affordable_shares(cash, price):
    # 实际佣金=max(比例佣金,最低佣金)，同时满足两种资金约束。
    estimated = price * (1 + SLIPPAGE_SPREAD / 2)
    by_rate = _b.int(_b.max(0, cash) / (estimated * (1 + FEE_COMMISSION)) // 100) * 100
    by_minimum = _b.int(_b.max(0, cash - MIN_COMMISSION) / estimated // 100) * 100
    return _b.min(by_rate, by_minimum)


def _benchmark_close(day):
    prices = get_price(BENCHMARK, end_date=day, count=1, frequency='daily', fields=['close'])
    if (prices is None or prices.empty
            or pd.Timestamp(prices.index[-1]).normalize() != pd.Timestamp(day).normalize()):
        raise PoolDataError('Missing benchmark close')
    value = _b.float(prices['close'].iloc[-1])
    if not _math.isfinite(value) or value <= 0:
        raise PoolDataError('Invalid benchmark close')
    return value


def _observe_holding_periods(day, held, orders):
    """按实际收盘持仓从有到无闭合；部分卖出、拒单和退出计划不算退出完成。"""
    date = _b.str(day.date())
    day_index = _b.len(g.nav_rows)
    for code in sorted(set(g.hold_entries) - held):
        entry = g.hold_entries.pop(code)
        fills = [o for o in orders.values() if o.security == code
                 and not o.is_buy and (o.filled or 0) > 0]
        tag = g.order_tags.get((date, code), {})
        row = {'code': code, 'entry_date': entry['entry_date'], 'exit_date': date,
               'holding_days': day_index - entry['entry_index'],
               'entry_source': entry['entry_source'],
               'closure_source': 'filled_order' if fills else 'position_disappeared',
               'signal_day': tag.get('signal_day', ''),
               'exit_reason': tag.get('reason', 'unknown'),
               'pool_reason': tag.get('pool_reason', '')}
        g.holding_rows.append(row)
    for code in sorted(held - set(g.hold_entries)):
        fills = [o for o in orders.values() if o.security == code
                 and o.is_buy and (o.filled or 0) > 0]
        g.hold_entries[code] = {'entry_date': date, 'entry_index': day_index,
                               'entry_source': 'filled_order' if fills else 'first_observed'}


def after_trading_end(context):
    """平台收盘回调；订单的filled及均价用于统计，申请量不当成成交量。"""
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_close_day == today:
        return
    g.last_close_day = today
    orders = get_orders()
    turnover = 0.0
    for oid, order in orders.items():
        filled, price = _b.float(order.filled or 0), _b.float(order.price or 0)
        turnover += _b.abs(filled * price)
        row = {'date': _b.str(today.date()), 'id': _b.str(oid),
               'code': order.security, 'is_buy': order.is_buy,
               'filled': filled, 'price': price, 'status': _b.str(order.status)}
        row.update(g.order_tags.get((row['date'], order.security), {}))
        row['commission'] = _b.getattr(order, 'commission', None)
        g.order_rows.append(row)
    try:
        benchmark = _benchmark_close(today)
    except Exception as exc:
        benchmark = _b.float('nan')
        _event(context, 'BENCHMARK_MISSING', detail=_b.type(exc).__name__)
    total, held = context.portfolio.total_value, _held(context)
    _observe_holding_periods(today, held, orders)
    g.exit_pending = {c: r for c, r in g.exit_pending.items() if c in held}
    g.exit_details = {c: r for c, r in g.exit_details.items() if c in held}
    g.order_tags = {}        # 已导出的当日标签不跨日累积；待退原因由exit_details保留。
    g.nav_rows.append({'date': _b.str(today.date()), 'equity': total,
                       'benchmark_close': benchmark, 'cash': context.portfolio.available_cash,
                       'holdings': _b.len(held), 'pool_size': g.pool_size,
                       'ranked_size': g.ranked_size, 'target_size': _b.len(g.target),
                       'pending_exits': _b.len(g.exit_pending), 'data_paused': g.data_paused,
                       'turnover': turnover / total if total > 0 else 0.0})
    record(holdings=_b.len(held), cash_pct=context.portfolio.available_cash / total * 100
           if total > 0 else 0, pending_exits=_b.len(g.exit_pending),
           turnover_pct=turnover / total * 100 if total > 0 else 0)


def _exit_reason_counts(rows):
    counts = {}
    for row in rows:
        key = row.get('exit_reason') or 'unknown'
        if key == 'out_of_pool':
            key += '/' + (row.get('pool_reason') or 'unknown')
        counts[key] = counts.get(key, 0) + 1
    return counts


def _diagnostic_summary():
    planned = [r for r in g.decision_rows if r['action'] == 'exit']
    known = [r for r in g.holding_rows if r['entry_source'] == 'filled_order'
             and r['closure_source'] == 'filled_order']
    short = [r for r in known if r['holding_days'] <= ROTATE_EVERY]
    log.info('[诊断汇总] 计划退出=%d次（同一待退股票可能重复计划）；实际闭合=%d轮；'
             '期末仍持有=%d轮' % (_b.len(planned), _b.len(g.holding_rows), _b.len(g.hold_entries)))
    log.info('[诊断汇总] 已知开平仓=%d轮；其中持有<=%d交易日=%d轮；'
             '信号日持有天数与下一交易日实际退出天数分开记录' % (
                 _b.len(known), ROTATE_EVERY, _b.len(short)))
    for label, rows in (('计划退出原因', planned), ('实际退出原因', known),
                        ('短持仓退出原因', short)):
        counts = _exit_reason_counts(rows)
        text = '; '.join('%s=%d' % (key, counts[key]) for key in sorted(counts))
        log.info('[诊断汇总] %s：%s' % (label, text or '本次无样本'))
    log.info('[诊断汇总] 原因记录全部已确认失败条件，以|连接。'
             '全批数据暂停不计为退出；平仓盈亏须与实际成交表配对，未在这里推算。')


def on_strategy_end(context):
    if not g.nav_rows:
        log.info('[质量池v5] 无收盘净值；检查平台收盘回调')
        return
    df = pd.DataFrame(g.nav_rows)
    nav = df['equity'] / g.initial_equity
    drawdown = (nav / nav.cummax().clip(lower=1.0) - 1).min()
    log.info('[质量池v5] 实际成交净值 收益=%+.2f%% 绝对最大回撤=%.2f%%'
             % ((nav.iloc[-1] - 1) * 100, drawdown * 100))
    if df['benchmark_close'].notna().all() and _b.len(df) > 1:
        benchmark = df['benchmark_close'] / df['benchmark_close'].iloc[0]
        relative = nav / benchmark
        ann_excess = relative.iloc[-1] ** (ANNUAL_DAYS / (_b.len(df) - 1)) - 1
        relative_dd = (relative / relative.cummax() - 1).min()
        active = nav.pct_change().iloc[1:] - benchmark.pct_change().iloc[1:]
        sharpe = (active.mean() / active.std(ddof=1) * _math.sqrt(ANNUAL_DAYS)
                  if active.std(ddof=1) > 0 else _b.float('nan'))
        log.info('[质量池v5] vs800 年化相对收益=%+.2f%% 相对最大回撤=%.2f%% '
                 '日超额夏普=%+.3f' % (ann_excess * 100, relative_dd * 100, sharpe))
    else:
        log.info('[质量池v5] 基准缺测或区间不足，不输出超额成绩')
    log.info('[质量池v5] 平均持仓=%.1f 平均现金=%.1f%% 数据异常轮数=%d；'
             '本策略成绩应与相同资金/费用/时钟的对照比较'
             % (df['holdings'].mean(), (df['cash'] / df['equity']).mean() * 100,
                _b.sum(r['kind'] == 'DATA_PAUSE' for r in g.event_rows)))
    counts = {}
    for row in g.event_rows:
        kind = row['kind']
        counts[kind] = counts.get(kind, 0) + 1
    log.info('[运行汇总] 委托申请=%d 拒单=%d 下单异常=%d 买入受阻=%d次 卖出受阻=%d次；'
             '同一待退股票可能多日受阻，完整事件见%s_events.csv' % (
                 counts.get('SUBMITTED', 0), counts.get('REJECTED', 0),
                 counts.get('ORDER_ERROR', 0), counts.get('BUY_BLOCKED', 0),
                 counts.get('SELL_BLOCKED', 0), OUTPUT_PREFIX))
    _diagnostic_summary()
    exported = []
    for name, rows, columns in (('nav', g.nav_rows, None), ('signals', g.signal_rows, None),
                                ('members', g.member_rows, None), ('events', g.event_rows, None),
                                ('orders', g.order_rows, None),
                                ('decisions', g.decision_rows, DECISION_COLUMNS),
                                ('holding_periods', g.holding_rows, HOLDING_COLUMNS)):
        try:
            filename = '%s_%s.csv' % (OUTPUT_PREFIX, name)
            write_file(filename, pd.DataFrame(rows, columns=columns).to_csv(index=False))
            exported.append(filename)
        except Exception as exc:
            log.info('[导出失败] %s：%s: %s' % (name, _b.type(exc).__name__, exc))
    if exported:
        log.info('[导出完成] %d/7张CSV：%s' % (_b.len(exported), ', '.join(exported)))
        log.info('[文件位置] 打开 https://www.joinquant.com/research → 投资研究文件列表，'
                 '回到根目录并刷新；找到%s_开头的CSV，勾选后下载。'
                 '文件在聚宽网站上，不会自动保存到电脑。' % OUTPUT_PREFIX)
        log.info('[文件位置] 优先下载 %s_decisions.csv 和 %s_holding_periods.csv；'
                 '这些过程表由策略导出，入口为投资研究的文件列表。'
                 % (OUTPUT_PREFIX, OUTPUT_PREFIX))
