# -*- coding: utf-8 -*-
"""聚宽单文件：v5持仓质量复检节奏对照——20日 vs 每日（离散条件/全部条件）。

问题：v5只在20日调仓日复检持仓质量（仅数据未知的旧仓每日补判）。20日间隔对
ROE、单季利润同比、ST这类离散信息是人为延迟；对PE/PB/vol60这类每日随价格
变动的条件，检查频率本身就是规则强度的一部分。日检能否改善收益与回撤？

三个子账户共享每日T-1候选快照与同一套v5规则，仅持仓复检节奏不同：
  base20          v5现行：调仓日全量复检 + 未确认数据旧仓每日补判（对照臂）。
  daily_discrete  离散条件（roe_low/profit_growth_low/st/退出主板）每日复检；
                  PE/PB/vol60仍仅调仓日检查。
  daily_all       全部质量条件每日复检。

固定边界：
- 日检在盘前生成、使用T-1数据，当日开盘经既有待退出队列执行；
- 撤销语义：base20与现行一致——仅数据未知旧仓每日补判，重新确认通过即取消；
  daily_discrete新增的离散退出在周期内不撤销（与现行"确认失败后不回头"一致），
  只沿用数据未知后恢复通过的取消路径；daily_all对全部待退出每日重确认，通过即
  取消。未受阻的退出当日开盘即卖出，不存在次日撤销；
- 三臂的入场与补位节奏完全一致：只在20日调仓日从前6名补位，日检只新增
  退出判定，不提前开仓。空出的名额到下一调仓日才补，"快出慢进"是本设计
  的有意边界；若日检在该保守补位下已改善，结论对补位方式更稳健。

每天频率、2016-01-04至2026-09-01、中证800；3个子账户各100万元，共300万元。
日频执行与原v5分钟版不同，结果不能与207.03%旧摘要比较；base20为同口径对照。
判定标准：daily_discrete改善收益或回撤、且退出撤销少 → 支持"20日间隔对离散
条件是人为延迟"，再考虑修改现行规则；daily_all的改善若伴随大量退出撤销或
换手明显上升 → 视为价格阈值日检噪音，不采用；无改善则保留20日节奏。
daily_discrete退出在周期内不撤销、daily_all待退出每日可重确认通过——两者撤销
语义的差异来自条件类型，是设计的一部分，读结果时须分开。
单项期限或单一年份的优势不构成采用依据。设计见本目录README第九节。

实现说明：引擎复用 study_v5_rank_control_jq.py 的多账户共享快照结构；
build_pool与v5一致，仅指标查询在传入subset时按请求代码过滤以省平台耗时，
请求集全部缺失指标行时回退全市场查询，判定语义与v5相同（有本地测试覆盖）。
"""
from jqdata import *
from jqfactor import get_factor_values
import builtins as _b
import math as _math
import time as _time
from types import SimpleNamespace as _Namespace
import pandas as pd

TOP_N = 6
MOMENTUM_DAYS = 252
SKIP_RECENT_DAYS = 21   # 12-2动量：跳过最近21个交易日
INDUSTRY_CAP = 2        # 目标组合内同一申万一级行业最多2只
ROTATE_EVERY = 20
ROE_MIN = 12.0
ROE_FACTOR_UNIT = 'ratio'  # ratio: 0.12=12%；percent: 12=12%
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
ACCOUNT_CASH = 1000000.0
ARMS = ('base20', 'daily_discrete', 'daily_all')
# 离散信息条件：由新披露财报、状态变更或上市身份驱动，不随当日价格连续变动。
DISCRETE_EXIT_REASONS = frozenset(('roe_low', 'profit_growth_low', 'st',
                                   'not_in_mainboard_universe'))
OUTPUT_PREFIX = 'quality_momentum_v5_exit_cadence_daily_'
VERBOSE_LOGS = False       # True时显示逐笔委托、调仓信号及买卖受阻日志
DECISION_COLUMNS = ['signal_day', 'asof', 'code', 'action', 'exit_reason', 'pool_reason',
                    'rank', 'previous_rank', 'momentum', 'entry_date', 'held_days_at_signal',
                    'roe_ann', 'profit_yoy', 'pe', 'pb', 'vol60', 'industry_code',
                    'industry_name', 'stat_date', 'pub_date', 'data_error',
                    'pool_state', 'review']
HOLDING_COLUMNS = ['code', 'entry_date', 'exit_date', 'holding_days', 'entry_source',
                   'closure_source', 'signal_day', 'exit_reason', 'pool_reason', 'trigger']
MEMBER_COLUMNS = ['signal_day', 'asof', 'code', 'pool_state', 'pool_reason',
                  'roe_ttm_pct', 'np_yoy_pct', 'pe_ttm', 'pb', 'vol60', 'roe_ann',
                  'profit_yoy', 'pe', 'pb', 'stat_date', 'pub_date', 'momentum',
                  'momentum_rank', 'industry_code', 'ranking_status']
POSITION_COLUMNS = ['date', 'account', 'seed', 'code', 'quantity', 'price', 'market_value', 'weight']
REVIEW_COLUMNS = ['account', 'seed', 'signal_day', 'asof', 'code', 'pool_state',
                  'pool_reason', 'roe_ann', 'profit_yoy', 'pe', 'pb', 'vol60', 'action']

ROE_TTM_MIN = ROE_MIN
SCORE_CLOSES = MOMENTUM_DAYS + 1
SCORE_SKIP_RECENT = SKIP_RECENT_DAYS
VOL_ANNUAL_DAYS = 250.0


class PoolDataError(Exception):
    pass


def new_account(index, mode):
    state = _Namespace(index=index, mode=mode, label=mode, seed=index)
    state.pending = None
    state.deferred_codes = set()
    state.no_adjust = set()
    state.industries = {}
    state.target = []
    state.exit_pending = {}      # code -> 退出原因，直到实际持仓清零
    state.pool_size = 0
    state.ranked_size = 0
    state.data_paused = False
    state.last_open_day = None
    state.initial_equity = ACCOUNT_CASH
    state.nav_rows = []
    state.signal_rows = []
    state.event_rows = []
    state.order_rows = []
    state.decision_rows = []
    state.holding_rows = []
    state.review_rows = []
    state.previous_ranks = {}
    state.current_decisions = {}
    state.exit_details = {}
    state.order_tags = {}
    state.hold_entries = {}
    state.logged_alerts = set()
    state.order_ids = set()
    state.previous_values = {}
    state.attribution = {}
    state.previous_equity = ACCOUNT_CASH
    return state


def initialize(context):
    seeds = ARMS
    expected = ACCOUNT_CASH * _b.len(seeds)
    if not _math.isclose(_b.float(context.portfolio.starting_cash), expected, abs_tol=.01):
        raise ValueError('初始资金须为%.0f元；%d个独立账户各100万元' % (expected, _b.len(seeds)))
    params = _b.getattr(context, 'run_params', None)
    if params is not None and _b.getattr(params, 'frequency', 'day') not in ('day', 'daily'):
        raise ValueError('请选择每天频率；本实验只比较日频执行下的复检节奏')
    if ROE_FACTOR_UNIT not in ('ratio', 'percent'):
        raise ValueError('ROE_FACTOR_UNIT must be ratio or percent')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    set_slippage(PriceRelatedSlippage(SLIPPAGE_SPREAD))
    log.set_level('order', 'info' if VERBOSE_LOGS else 'error')
    set_subportfolios([SubPortfolioConfig(cash=ACCOUNT_CASH, type='stock') for _ in seeds])
    g.accounts = [new_account(i, mode) for i, mode in _b.enumerate(seeds)]
    g.day, g.signal_retry, g.tax_rate = 0, False, None
    g.last_signal_day, g.last_close_day = None, None
    g.member_rows, g.position_rows = [], []
    g.rotation_days = set()   # 调仓日signal_day字符串，用于区分退出触发来源
    g.detail_files_started = set()
    _set_costs(context)
    g.wall_started = _time.time()
    run_daily(on_signal, time='before_open')
    run_daily(on_open, time='open')
    g.settings = {
        'arms': ';'.join(ARMS),
        'account_cash': ACCOUNT_CASH, 'total_cash': expected,
        'start_date': _b.str(_b.getattr(params, 'start_date', '')),
        'end_date': _b.str(_b.getattr(params, 'end_date', '')),
        'frequency': _b.getattr(params, 'frequency', 'day'), 'benchmark': BENCHMARK,
        'signal_time': 'before_open', 'execution_time': 'open',
        'discrete_exit_reasons': ';'.join(_b.sorted(DISCRETE_EXIT_REASONS)),
        'review_schedule': ('base20: rotation-day full recheck + daily deferred review; '
                            'daily arms: daily recheck (discrete only / all conditions)'),
        'daily_exit_execution': 'same-day open via pending-exit queue',
        'cancel_rule': 'base20/deferred and daily_all: cancel on reconfirmed pass; '
                       'daily_discrete discrete exits stick within cycle',
        'entry_refill': 'rotation days only; mid-cycle exits stay cash until next rotation',
        'roe_min_pct': ROE_MIN, 'roe_factor_unit': ROE_FACTOR_UNIT,
        'profit_yoy_min_pct': NP_YOY_MIN, 'pe_max': PE_MAX, 'pb_max': PB_MAX,
        'vol60_max_pct': VOL60_MAX, 'vol_annual_days': VOL_ANNUAL_DAYS,
        'momentum_days': MOMENTUM_DAYS, 'skip_recent_days': SKIP_RECENT_DAYS,
        'top_n': TOP_N, 'industry_cap': INDUSTRY_CAP, 'rotate_every': ROTATE_EVERY,
        'commission': FEE_COMMISSION, 'min_commission': MIN_COMMISSION,
        'slippage_spread': SLIPPAGE_SPREAD, 'sell_tax_before': SELL_TAX_BEFORE,
        'sell_tax_after': SELL_TAX_AFTER, 'tax_change_date': TAX_CHANGE_DATE,
        'annual_days': ANNUAL_DAYS,
    }
    log.info('[复检节奏对照] %d臂各100万元；日检盘前用T-1数据、当日开盘执行；'
             '补位仅在每%d日调仓日。平台主图为合计，请看分账户结果。'
             % (_b.len(seeds), ROTATE_EVERY))


def _account_orders(state, orders):
    """用本账户实际提交的订单ID隔离，不能按股票代码区分不同账户。"""
    return {oid: order for oid, order in orders.items() if _b.str(oid) in state.order_ids}


def _evaluate_pool(universe, fundamentals, status, closes, audit=None):
    """停牌只限制成交，不定义质量出池。

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


def build_pool(context, asof, audit=None, subset=None, held=frozenset()):
    secs = get_all_securities(['stock'], date=context.current_dt)
    if not _b.isinstance(secs, pd.DataFrame) or secs.empty:
        raise PoolDataError('Missing historical stock universe')
    universe = {c for c in secs.index if c[:2] in ('60', '00')}
    requested = universe if subset is None else set(subset)
    ind = _indicator_frame(context, asof, requested)
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
    held = set(held) & requested
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


def _indicator_frame(context, asof, requested):
    """单季净利润快照。subset为全市场时一次无过滤查询；小请求集按代码过滤，
    请求代码全部缺失指标时回退全市场——保证口径不随请求集改变。"""
    if len(requested) > 3000:
        return _indicator_fetch(asof, None)
    ind = _indicator_fetch(asof, requested)
    if ind.empty:
        return _indicator_fetch(asof, None)
    return ind


def _indicator_fetch(asof, requested):
    q = query(indicator.code, indicator.inc_net_profit_year_on_year,
              indicator.statDate, indicator.pubDate)
    if requested:
        q = q.filter(indicator.code.in_(_b.list(requested)))
    frame = get_fundamentals(q, date=asof)
    _require_columns(frame, ('code', 'inc_net_profit_year_on_year', 'statDate', 'pubDate'), 'financial')
    return frame


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


def _plan_account(state, context, asof, pool, momentum_ranked, scores, industries, snapshot_audit):
    """调仓日计划；三臂逻辑一致，差异只来自各自exit_pending/deferred状态。"""
    today = pd.Timestamp(context.current_dt).normalize()
    held = _held(state, context)
    audit = {c: dict(snapshot_audit[c]) for c in set(pool) | held}
    deferred = {c for c in held if audit[c]['pool_state'] == 'unknown'}
    protected = deferred - set(state.exit_pending)
    selected, kept = select_targets(momentum_ranked, held, set(pool), industries, protected)
    rank = {c: i + 1 for i, c in _b.enumerate(momentum_ranked)}
    state.deferred_codes, state.data_paused = deferred, False
    exits = {c: 'out_of_pool' for c in held if audit[c]['pool_state'] == 'failed'}
    exits.update({c: state.exit_pending[c] for c in deferred & set(state.exit_pending)})
    no_adjust = deferred | {c for c in kept if not industries.get(c)}
    decisions = {}
    for code in _b.sorted(held | set(selected)):
        audit[code]['industry_code'] = industries.get(code, '')
        action = ('exit' if code in exits else 'defer' if code in no_adjust
                  else 'keep' if code in held else 'entry')
        decisions[code] = _decision(state, context, asof, code, action, audit,
            rank=rank.get(code), score=scores.get(code), exit_reason=exits.get(code, ''),
            review='rotation')
    state.pool_size, state.ranked_size = _b.len(pool), _b.len(momentum_ranked)
    state.pending = dict(signal_day=today, asof=asof, selected=selected, exit_reasons=exits,
        decisions=decisions, exit_details={c: decisions[c] for c in exits},
        deferred=no_adjust, industries=industries)
    state.signal_rows.append(dict(signal_day=_b.str(today.date()), asof=_b.str(asof.date()),
        pool_size=_b.len(pool), ranked_size=_b.len(momentum_ranked), kept=';'.join(kept),
        selected=';'.join(selected),
        entry_not_selected=';'.join(c for c in momentum_ranked[:TOP_N] if c not in selected),
        planned_exits=_b.len(exits), deferred=_b.len(deferred)))
    if no_adjust:
        _event(state, context, 'DATA_DEFER', detail='hold without adjustment: %s' % ';'.join(_b.sorted(no_adjust)))
    state.previous_ranks = rank
    _event(state, context, 'SIGNAL', detail='pool=%d target=%d deferred=%d' % (
        _b.len(pool), _b.len(selected), _b.len(deferred)))


def _review_set(state, context):
    """每日复检集合：base20只补判未确认旧仓，daily两臂复查全部持仓。"""
    held = _held(state, context)
    return held if state.mode != 'base20' else held & state.deferred_codes


def _daily_review(context, asof):
    plans = [(state, _review_set(state, context)) for state in g.accounts]
    codes = set().union(*[codes for _, codes in plans]) if plans else set()
    if not codes:
        return
    audit = {c: {} for c in codes}
    try:
        build_pool(context, asof, audit=audit, subset=codes, held=codes)
    except Exception as exc:
        for state, own in plans:
            if own:
                _event(state, context, 'DATA_DEFER', detail='daily review paused: %s' % exc)
        return
    for state, own in plans:
        tag = 'daily_deferred' if state.mode == 'base20' else 'daily_held'
        for code in _b.sorted(own):
            row = audit[code]
            pool_state = row['pool_state']
            reasons = [r for r in (row.get('pool_reason') or '').split('|') if r]
            # daily_discrete只对离散失败设退出；价格失败留待下一调仓日重判。
            failed = (pool_state == 'failed'
                      and (state.mode != 'daily_discrete'
                           or any(r in DISCRETE_EXIT_REASONS for r in reasons)))
            acted = '-'
            if failed:
                if code not in state.exit_pending:
                    acted = 'exit_set'
                    state.exit_details[code] = _decision(state, context, asof, code, 'exit',
                                                        audit, exit_reason='out_of_pool', review=tag)
                    _event(state, context, 'EXIT_PLANNED', code,
                           'review=%s reason=%s' % (tag, row.get('pool_reason')))
                state.exit_pending[code] = 'out_of_pool'
            elif pool_state == 'passed' and code in state.exit_pending:
                # base20/daily_discrete沿用现行路径：仅数据未知补判恢复通过才取消；
                # 确认过的退出（离散或价格）在周期内不回头。daily_all对待退出每日
                # 重确认，通过即取消——价格阈值的反复撤销是本臂设计的一部分。
                cancellable = (state.mode == 'daily_all' or code in state.deferred_codes)
                if cancellable:
                    acted = 'exit_cancel'
                    state.exit_pending.pop(code, None)
                    state.exit_details.pop(code, None)
                    _event(state, context, 'EXIT_CANCELLED', code,
                           'review=%s reconfirmed pass' % tag)
            state.review_rows.append(dict(signal_day=_b.str(pd.Timestamp(context.current_dt).date()),
                asof=_b.str(asof.date()), code=code, pool_state=pool_state,
                pool_reason=row.get('pool_reason', ''), roe_ann=row.get('roe_ann'),
                profit_yoy=row.get('profit_yoy'), pe=row.get('pe'), pb=row.get('pb'),
                vol60=row.get('vol60'), action=acted))
        state.deferred_codes = {c for c in own if audit[c]['pool_state'] == 'unknown'}


def on_signal(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_signal_day == today:
        return
    g.last_signal_day = today
    g.day += 1
    asof = _prev_trade_day(context.current_dt)
    if (g.day - 1) % ROTATE_EVERY and not g.signal_retry:
        _daily_review(context, asof)
        return
    held = set().union(*(_held(state, context) for state in g.accounts))
    audit = {c: {} for c in held}
    try:
        pool, closes = build_pool(context, asof, audit=audit, held=held)
        ranked, scores = rank_pool(pool, closes)
        industries = _sw_industries(set(ranked) | held, asof)
    except Exception as exc:
        g.signal_retry = True
        for state in g.accounts:
            state.pending, state.data_paused = None, True
            for code in _b.sorted(_held(state, context)):
                _decision(state, context, asof, code, 'data_pause', audit, data_error=_b.str(exc))
            _event(state, context, 'DATA_PAUSE', detail='%s; shared snapshot retry next session' % exc)
        return
    g.signal_retry = False
    g.rotation_days.add(_b.str(today.date()))
    momentum_ranks = {c: i + 1 for i, c in _b.enumerate(ranked)}
    for code in _b.sorted(set(pool) | held):
        row = dict(audit[code])
        row.update(signal_day=_b.str(today.date()), asof=_b.str(asof.date()), code=code,
            momentum=scores.get(code), momentum_rank=momentum_ranks.get(code),
            industry_code=industries.get(code, ''),
            ranking_status='ranked' if code in scores else
                'momentum_history_missing' if code in pool else 'outside_pool')
        g.member_rows.append(row)
    for state in g.accounts:
        _plan_account(state, context, asof, pool, ranked, scores, industries, audit)


def _industry_buy_block(state, code, held):
    if code in held:
        return ''
    key = state.industries.get(code)
    if not key or _b.any(not state.industries.get(c) for c in held):
        return 'industry_unknown'
    return 'industry_cap' if _b.sum(state.industries[c] == key for c in held) >= INDUSTRY_CAP else ''


def _buy_targets(state, context, cd, per):
    open_orders = _account_orders(state, get_open_orders())
    open_codes = {o.security for o in open_orders.values()}
    reserved = _held(state, context) | {o.security for o in open_orders.values() if o.is_buy}
    for code in state.target:
        if code in open_codes or code in state.no_adjust or code in state.exit_pending:
            continue
        px = _price(cd, code)
        blocked = _quote_block(cd, code, 'buy')
        if not blocked:
            blocked = _industry_buy_block(state, code, reserved)
        if blocked:
            _event(state, context, 'BUY_BLOCKED', code, blocked)
            continue
        positions = context.subportfolios[state.index].long_positions
        pos = positions[code] if code in positions.keys() else None
        current = _b.int(pos.total_amount) if pos is not None else 0
        desired = _b.int(per / px // 100) * 100
        if desired <= current:
            continue
        if current == 0 and code not in reserved and _b.len(reserved) >= TOP_N:
            _event(state, context, 'BUY_BLOCKED', code, 'pending exit occupies slot')
            continue
        delta = _b.min(desired - current, _affordable_shares(context.subportfolios[state.index].available_cash, px))
        delta = _b.int(delta // 100) * 100
        if delta < 100:
            _event(state, context, 'BUY_BLOCKED', code, 'cash/lot')
            continue
        if _submit(state, context, code, current + delta, 'equal_weight') is not None:
            reserved = _held(state, context) | {o.security for o in _account_orders(state, get_open_orders()).values() if o.is_buy}


def _account_on_open(state, context):
    today = pd.Timestamp(context.current_dt).normalize()
    if state.last_open_day == today:
        return
    state.last_open_day = today
    _set_costs(context)
    plan, state.pending = state.pending, None
    expected = today
    if plan is not None and plan['signal_day'] != expected:
        _event(state, context, 'STALE_SIGNAL', detail=_b.str(plan['signal_day'].date()))
        plan = None
    cd = get_current_data()
    if plan is not None:
        state.target = plan['selected']
        state.no_adjust = set(plan['deferred'])
        state.industries = plan['industries']
        state.exit_pending = plan['exit_reasons']
        state.exit_details = plan['exit_details']
        state.current_decisions = plan['decisions']
        per = context.subportfolios[state.index].total_value / TOP_N
    open_codes = {o.security for o in _account_orders(state, get_open_orders()).values()}
    for code, reason in sorted(state.exit_pending.items()):
        if code in _held(state, context) and code not in open_codes:
            _sell(state, context, cd, code, 0, reason)
    state.exit_pending = {c: r for c, r in state.exit_pending.items() if c in _held(state, context)}
    if plan is None:
        return
    for code in state.target:
        if code in state.no_adjust or code in open_codes:
            continue
        if code in _held(state, context):
            px = _price(cd, code)
            if px is not None:
                current = _b.int(context.subportfolios[state.index].long_positions[code].total_amount)
                reduction = _b.int(_b.max(0, current - per / px) // 100) * 100
                if reduction:
                    _sell(state, context, cd, code, current - reduction, 'equal_weight')
    _buy_targets(state, context, cd, per)


def on_open(context):
    for state in g.accounts:
        _account_on_open(state, context)


def _set_costs(context):
    tax = (SELL_TAX_AFTER if pd.Timestamp(context.current_dt).normalize()
           >= pd.Timestamp(TAX_CHANGE_DATE) else SELL_TAX_BEFORE)
    if tax != g.tax_rate:
        set_order_cost(OrderCost(open_tax=0, close_tax=tax,
                                 open_commission=FEE_COMMISSION,
                                 close_commission=FEE_COMMISSION,
                                 min_commission=MIN_COMMISSION), type='stock')
        g.tax_rate = tax


def _event(state, context, kind, code='', detail=''):
    state.event_rows.append({'time': _b.str(context.current_dt), 'kind': kind,
                         'code': code, 'detail': detail})
    # 过程表保留所有事件；默认仅显示严重异常，同一异常只提醒一次。
    key = (kind, code, detail)
    alerts = ('DATA_PAUSE', 'DATA_DEFER', 'ORDER_ERROR', 'STALE_SIGNAL', 'BENCHMARK_MISSING')
    if VERBOSE_LOGS or (kind in alerts and key not in state.logged_alerts):
        log.info('[%s] %s %s %s' % (state.label, kind, code, detail))
        state.logged_alerts.add(key)


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


def _held(state, context):
    # 卖单冻结的数量仍然占用一个持仓名额。
    return {c for c, p in context.subportfolios[state.index].long_positions.items()
            if p.total_amount + _b.getattr(p, 'locked_amount', 0) > 0}


def _holding_info(state, code):
    entry = state.hold_entries.get(code)
    if entry is None:
        return '', None
    return entry['entry_date'], _b.len(state.nav_rows) - entry['entry_index']


def _decision(state, context, asof, code, action, audit, rank=None, score=None,
              exit_reason='', data_error='', review='rotation'):
    entry_date, age = _holding_info(state, code)
    row = dict(audit.get(code, {}))
    row.update(signal_day=_b.str(pd.Timestamp(context.current_dt).date()),
               asof=_b.str(asof.date()) if asof is not None else '', code=code,
               action=action, exit_reason=exit_reason, rank=rank, momentum=score,
               previous_rank=state.previous_ranks.get(code), entry_date=entry_date,
               held_days_at_signal=age, data_error=data_error, review=review)
    state.decision_rows.append(row)
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


def _submit(state, context, code, quantity, reason):
    try:
        order = order_target(code, quantity, pindex=state.index)
        _event(state, context, 'SUBMITTED' if order is not None else 'REJECTED', code,
               '%s target_shares=%d' % (reason, quantity))
        if order is not None:
            state.order_ids.add(_b.str(order.order_id))
            source = (state.exit_details.get(code, {}) if reason != 'equal_weight'
                      else state.current_decisions.get(code, {}))
            date = _b.str(pd.Timestamp(context.current_dt).date())
            _, age = _holding_info(state, code)
            state.order_tags[(date, code)] = {'reason': reason,
                'signal_day': source.get('signal_day', ''),
                'pool_reason': source.get('pool_reason', ''),
                'holding_days_at_order': age}
        return order
    except Exception as exc:
        _event(state, context, 'ORDER_ERROR', code, _b.type(exc).__name__)
        return None


def _sell(state, context, cd, code, target, reason):
    positions = context.subportfolios[state.index].long_positions
    # 聚宽兼容容器的get可能读取缺失键并告警，先检查实际持仓键。
    pos = positions[code] if code in positions.keys() else None
    if pos is None or pos.total_amount <= target:
        return
    blocked = _quote_block(cd, code, 'sell')
    if blocked:
        _event(state, context, 'SELL_BLOCKED', code, '%s blocked_by=%s' % (reason, blocked))
        return
    if pos.closeable_amount <= 0:
        _event(state, context, 'SELL_BLOCKED', code, '%s blocked_by=no_closeable_shares' % reason)
        return
    amount = _b.max(target, _b.int(pos.total_amount - pos.closeable_amount))
    _submit(state, context, code, amount, reason)


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


def _observe_holding_periods(state, day, held, orders):
    """按实际收盘持仓从有到无闭合；部分卖出、拒单和退出计划不算退出完成。"""
    date = _b.str(day.date())
    day_index = _b.len(state.nav_rows)
    for code in sorted(set(state.hold_entries) - held):
        entry = state.hold_entries.pop(code)
        fills = [o for o in orders.values() if o.security == code
                 and not o.is_buy and (o.filled or 0) > 0]
        tag = state.order_tags.get((date, code), {})
        signal_day = tag.get('signal_day', '')
        trigger = ''
        if signal_day:
            trigger = 'daily' if signal_day not in g.rotation_days else 'rotation'
        row = {'code': code, 'entry_date': entry['entry_date'], 'exit_date': date,
               'holding_days': day_index - entry['entry_index'],
               'entry_source': entry['entry_source'],
               'closure_source': 'filled_order' if fills else 'position_disappeared',
               'signal_day': signal_day,
               'exit_reason': tag.get('reason', 'unknown'),
               'pool_reason': tag.get('pool_reason', ''),
               'trigger': trigger}
        state.holding_rows.append(row)
    for code in sorted(held - set(state.hold_entries)):
        fills = [o for o in orders.values() if o.security == code
                 and o.is_buy and (o.filled or 0) > 0]
        state.hold_entries[code] = {'entry_date': date, 'entry_index': day_index,
                               'entry_source': 'filled_order' if fills else 'first_observed'}


def after_trading_end(context):
    """所有统计来自平台子账户及实际成交，不把目标持仓当作成交。"""
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_close_day == today:
        return
    orders = get_orders()
    owned = set().union(*(state.order_ids for state in g.accounts))
    if {_b.str(oid) for oid in orders} - owned:
        raise RuntimeError('Unattributed platform order; stop rather than mix account results')
    try:
        benchmark = _benchmark_close(today)
    except Exception as exc:
        benchmark = _b.float('nan')
        for state in g.accounts:
            _event(state, context, 'BENCHMARK_MISSING', detail=_b.type(exc).__name__)
    curves = {}
    for state in g.accounts:
        _close_account(state, context, today, _account_orders(state, orders), benchmark)
        curves[state.label] = (state.nav_rows[-1]['equity'] / state.initial_equity - 1) * 100
    record(**curves)
    g.last_close_day = today
    completed = _b.len(g.accounts[0].nav_rows)
    if completed % 20 == 0:
        _flush_shared_details()
    if completed == 20 or completed % 250 == 0:
        log.info('[运行进度] 截至%s，完成%d个交易日，实际耗时%.1f分钟。'
                 % (today.date(), completed, (_time.time() - g.wall_started) / 60.0))


def _close_account(state, context, today, orders, benchmark):
    portfolio = context.subportfolios[state.index]
    total = _b.float(portfolio.total_value)
    if not _math.isfinite(total) or total <= 0:
        raise ValueError('Invalid account equity: ' + state.label)
    date = _b.str(today.date())
    turnover, net_buys, reported_costs = 0.0, {}, {}
    for oid, order in orders.items():
        filled, price = _b.float(order.filled or 0), _b.float(order.price or 0)
        value = filled * price
        turnover += _b.abs(value)
        net_buys[order.security] = net_buys.get(order.security, 0.0) + (value if order.is_buy else -value)
        row = dict(date=date, id=_b.str(oid), code=order.security, is_buy=order.is_buy,
                   filled=filled, price=price, status=_b.str(order.status))
        row.update(state.order_tags.get((date, order.security), {}))
        row['commission'] = _b.getattr(order, 'commission', None)
        if row['commission'] is not None:
            reported_costs[order.security] = reported_costs.get(order.security, 0.0) + _b.float(row['commission'])
        state.order_rows.append(row)
    values = {}
    for code, pos in portfolio.long_positions.items():
        quantity = _b.float(pos.total_amount) + _b.float(_b.getattr(pos, 'locked_amount', 0))
        if quantity <= 0:
            continue
        value = quantity * _b.float(pos.price)
        if not _math.isfinite(value) or value <= 0:
            raise ValueError('Invalid closing position value: %s/%s' % (state.label, code))
        values[code] = value
        g.position_rows.append(dict(date=date, account=state.label, seed=state.seed, code=code,
            quantity=quantity, price=_b.float(pos.price), market_value=value, weight=value / total))
    price_pnl = 0.0
    year = today.year
    for code in set(values) | set(state.previous_values) | set(net_buys):
        pnl = values.get(code, 0.0) - state.previous_values.get(code, 0.0) - net_buys.get(code, 0.0)
        price_pnl += pnl
        key = (year, code)
        row = state.attribution.setdefault(key, dict(year=year, code=code, price_pnl=0.0,
            reported_commission=0.0, held_days=0, residual=0.0))
        row['price_pnl'] += pnl
        row['reported_commission'] += reported_costs.get(code, 0.0)
        row['held_days'] += _b.int(code in values)
    residual = total - state.previous_equity - price_pnl
    key = (year, '__residual__')
    row = state.attribution.setdefault(key, dict(year=year, code='__residual__', price_pnl=0.0,
        reported_commission=None, held_days=None, residual=0.0))
    row['residual'] += residual
    state.previous_values, state.previous_equity = values, total
    held = _held(state, context)
    _observe_holding_periods(state, today, held, orders)
    state.exit_pending = {c: r for c, r in state.exit_pending.items() if c in held}
    state.exit_details = {c: r for c, r in state.exit_details.items() if c in held}
    state.order_tags = {}
    invested = _b.sum(values.values())
    state.nav_rows.append(dict(date=date, equity=total, benchmark_close=benchmark,
        cash=_b.float(portfolio.available_cash), invested=invested, cash_pct=(total - invested) / total * 100,
        holdings=_b.len(held), pool_size=state.pool_size, ranked_size=state.ranked_size,
        target_size=_b.len(state.target), pending_exits=_b.len(state.exit_pending),
        data_paused=state.data_paused, turnover=turnover / total, traded_value=turnover))


def account_metrics(rows, initial):
    """收益及回撤从原始本金起算，包含首日费用；年度起点由上年末承接。"""
    df = pd.DataFrame(rows)
    if df.empty:
        return {}
    equity = pd.to_numeric(df['equity'])
    total_return = equity.iloc[-1] / initial - 1
    peak = equity.cummax().clip(lower=initial)
    return dict(first_date=df['date'].iloc[0], last_date=df['date'].iloc[-1],
        trading_days=_b.len(df), initial_equity=initial, final_equity=equity.iloc[-1],
        pnl=equity.iloc[-1] - initial, return_pct=total_return * 100,
        annual_return_pct=((1 + total_return) ** (ANNUAL_DAYS / _b.len(df)) - 1) * 100,
        max_drawdown_pct=-(equity / peak - 1).min() * 100,
        average_cash_pct=df['cash_pct'].mean(), average_holdings=df['holdings'].mean(),
        turnover_sum=df['turnover'].sum(), traded_value=df['traded_value'].sum(),
        data_paused_days=_b.int(df['data_paused'].sum()))


def yearly_metrics(state):
    groups = {}
    for row in state.nav_rows:
        groups.setdefault(_b.int(row['date'][:4]), []).append(row)
    initial, rows = state.initial_equity, []
    for year in _b.sorted(groups):
        metrics = account_metrics(groups[year], initial)
        metrics.update(year=year, account=state.label, seed=state.seed)
        rows.append(metrics)
        initial = metrics['final_equity']
    return rows


def segment_return(annual, account, y0, y1):
    rows = [r for r in annual if r['account'] == account and y0 <= r['year'] <= y1]
    if not rows:
        return None
    total = 1.0
    for r in rows:
        total *= 1 + r['return_pct'] / 100.0
    return (total - 1.0) * 100.0


COMPARISON_METRICS = ('return_pct', 'annual_return_pct', 'max_drawdown_pct',
                      'average_cash_pct', 'average_holdings', 'turnover_sum')
SEGMENTS = (('2016-2019', 2016, 2019), ('2020-2026', 2020, 2026))


def comparison_rows(summaries, annual):
    base = _b.next((r for r in summaries if r['account'] == 'base20'), None)
    rows = []
    if base is not None:
        for arm in ('daily_discrete', 'daily_all'):
            other = _b.next((r for r in summaries if r['account'] == arm), None)
            if other is None:
                continue
            for metric in COMPARISON_METRICS:
                rows.append(dict(period='full', account=arm, metric=metric,
                    base20=base[metric], value=other[metric], diff=other[metric] - base[metric]))
    by_year = {}
    for r in annual:
        by_year.setdefault(r['year'], {})[r['account']] = r['return_pct']
    for year in _b.sorted(by_year):
        values = by_year[year]
        if 'base20' not in values:
            continue
        for arm in ('daily_discrete', 'daily_all'):
            if arm in values:
                rows.append(dict(period=_b.str(year), account=arm, metric='return_pct',
                    base20=values['base20'], value=values[arm], diff=values[arm] - values['base20']))
    for label, y0, y1 in SEGMENTS:
        for arm in ('daily_discrete', 'daily_all'):
            arm_seg = segment_return(annual, arm, y0, y1)
            base_seg = segment_return(annual, 'base20', y0, y1)
            if arm_seg is not None and base_seg is not None:
                rows.append(dict(period=label, account=arm, metric='return_pct',
                    base20=base_seg, value=arm_seg, diff=arm_seg - base_seg))
    return rows


def exit_stats_rows(state):
    groups = {}
    for row in state.holding_rows:
        reason = (row.get('exit_reason') or 'unknown') + '/' + (row.get('pool_reason') or '-')
        agg = groups.setdefault(reason, dict(account=state.label, exit_reason=reason,
                                             closed=0, daily_trigger=0, _days=[]))
        agg['closed'] += 1
        agg['daily_trigger'] += _b.int(row.get('trigger') == 'daily')
        agg['_days'].append(row['holding_days'])
    rows = []
    for reason in _b.sorted(groups):
        agg = groups[reason]
        days = _b.sorted(agg.pop('_days'))
        agg['median_holding_days'] = days[_b.len(days) // 2] if days else None
        rows.append(agg)
    return rows


def _account_rows(attribute):
    return [dict(row, account=state.label, seed=state.seed)
            for state in g.accounts for row in _b.getattr(state, attribute)]


def _write_flush(name, rows, columns, clear=None):
    started = name in g.detail_files_started
    if rows or not started:
        write_file('%s_%s.csv' % (OUTPUT_PREFIX, name),
                   pd.DataFrame(rows, columns=columns).to_csv(index=False, header=not started),
                   append=started)
        g.detail_files_started.add(name)
    if clear:
        for state in g.accounts:
            _b.getattr(state, clear).clear()


def _flush_shared_details():
    """成员/持仓/日检明细每20日写出，避免十多年明细一直堆在内存。"""
    _write_flush('members', g.member_rows, MEMBER_COLUMNS, None)
    g.member_rows.clear()
    _write_flush('positions', g.position_rows, POSITION_COLUMNS, None)
    g.position_rows.clear()
    _write_flush('reviews', _account_rows('review_rows'), REVIEW_COLUMNS, 'review_rows')


def on_strategy_end(context):
    summaries, annual, attribution, exit_stats = [], [], [], []
    for state in g.accounts:
        if not state.nav_rows:
            raise RuntimeError('Missing closing observations for ' + state.label)
        summary = account_metrics(state.nav_rows, state.initial_equity)
        summary.update(account=state.label, seed=state.seed,
            data_pause_events=_b.sum(r['kind'] == 'DATA_PAUSE' for r in state.event_rows),
            order_errors=_b.sum(r['kind'] == 'ORDER_ERROR' for r in state.event_rows),
            daily_exit_plans=_b.sum(r['action'] == 'exit' and _b.str(r.get('review', '')).startswith('daily')
                                    for r in state.decision_rows),
            exit_cancels=_b.sum(r['kind'] == 'EXIT_CANCELLED' for r in state.event_rows),
            blocked_sell_days=_b.sum(r['kind'] == 'SELL_BLOCKED' for r in state.event_rows))
        summaries.append(summary)
        annual.extend(yearly_metrics(state))
        attribution.extend(dict(row, account=state.label, seed=state.seed)
                           for _, row in _b.sorted(state.attribution.items()))
        exit_stats.extend(exit_stats_rows(state))
        log.info('[%s] 收益%+.2f%%；最大回撤%.2f%%；平均现金%.2f%%；累计双边换手%.2f倍；'
                 '日检计划退出%d次，撤销%d次'
                 % (state.label, summary['return_pct'], summary['max_drawdown_pct'],
                    summary['average_cash_pct'], summary['turnover_sum'],
                    summary['daily_exit_plans'], summary['exit_cancels']))
    comparisons = comparison_rows(summaries, annual)
    for row in comparisons:
        if row['period'] == 'full':
            log.info('[对照] %s %s：base20=%.2f，%s=%.2f，差%+.2f'
                     % (row['account'], row['metric'], row['base20'], row['account'], row['value'], row['diff']))
    log.info('[口径] 年化按245交易日；年度以前一交易日净值起算；期末未平仓也计入账户收益。'
             '日检臂补位仅在调仓日，快出慢进属设计边界。')
    log.info('[个股分解] price_pnl为市值变化扣除净买入成交额；残差单列费用、分红及其他。'
             'reported_commission仅供核对，已含在账户净值及残差中，不能再扣一次。')
    tables = [
        ('settings', [g.settings], None), ('summary', summaries, None),
        ('annual', annual, None), ('comparison', comparisons,
            ['period', 'account', 'metric', 'base20', 'value', 'diff']),
        ('exit_stats', exit_stats,
            ['account', 'exit_reason', 'closed', 'daily_trigger', 'median_holding_days']),
        ('nav', _account_rows('nav_rows'), None), ('signals', _account_rows('signal_rows'),
            ['account', 'seed', 'signal_day', 'asof', 'pool_size', 'ranked_size', 'kept', 'selected',
             'entry_not_selected', 'planned_exits', 'deferred']),
        ('orders', _account_rows('order_rows'),
            ['account', 'seed', 'date', 'id', 'code', 'is_buy', 'filled', 'price', 'status',
             'reason', 'signal_day', 'pool_reason', 'holding_days_at_order', 'commission']),
        ('decisions', _account_rows('decision_rows'), ['account', 'seed'] + DECISION_COLUMNS),
        ('holding_periods', _account_rows('holding_rows'), ['account', 'seed'] + HOLDING_COLUMNS),
        ('events', _account_rows('event_rows'), ['account', 'seed', 'time', 'kind', 'code', 'detail']),
        ('attribution', attribution,
            ['account', 'seed', 'year', 'code', 'price_pnl', 'reported_commission', 'held_days', 'residual']),
    ]
    failures = []
    try:
        _flush_shared_details()
    except Exception as exc:
        failures.append('members/positions/reviews')
        log.info('[导出失败] members/positions/reviews: %s' % exc)
    for name, rows, columns in tables:
        try:
            write_file('%s_%s.csv' % (OUTPUT_PREFIX, name), pd.DataFrame(rows, columns=columns).to_csv(index=False))
        except Exception as exc:
            failures.append(name)
            log.info('[导出失败] %s: %s' % (name, exc))
    if failures:
        raise RuntimeError('Incomplete experiment export: ' + ', '.join(failures))
    log.info('[导出完成] %d张CSV；在聚宽投资研究文件列表下载%s_开头的文件。'
             '先看summary、comparison、exit_stats，平台主图不能作为单账户成绩。'
             % (_b.len(tables) + 3, OUTPUT_PREFIX))
