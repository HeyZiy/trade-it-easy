# -*- coding: utf-8 -*-
"""质量池 + 12-2动量轮动 v4_1_6（取消留存仓差额再平衡，单变量消融）。

与v4_1_2（当前定版）的唯一差异：调仓日不动留存仓——不减持超出预算的部分、
不补足低于预算的缺口，留存仓持有数量原样保留；仅对未持有的目标新仓按
总权益/6预算买入，退出（out_of_pool）与退出重试逻辑不变。动机：留存仓差额
再平衡自v4以来从未消融，少换手省成本与错过权重纠正孰大孰小没有数字；旁证是
v4_1_2实测跌出留存线的旧仓卖后20日中位+2.00%、胜过实际策略52.5%，该线上
强制换手未见收益，本变体提供"留存仓不动"的对照。

规格（2026-10-11）。除上述差异外，其余与v4_1_2完全相同：
1) 目标持仓 6只，新仓单只预算=策略账户总权益/6（约16.7%）；不足6只时不放大
   单只预算，其余留现金。
2) 排序分数 12-2动量：R = P(T-22)/P(T-253)-1（T为信号日），即以T-1收盘为基准
   跳过最近21个交易日、跨231个交易日（约12个月减最近1个月）的收益；需253根有效
   收盘，同分按股票代码升序。
3) 新仓门槛 前6名（行业已满2只时跳过且不顺延到第7名）；旧仓留存=仍在质量池内，
   不因行业上限强制退出（留存可超上限）。
4) 行业集中：目标组合内同一申万一级行业最多2只；缺行业映射的股票自成一组不受限。
5) 删除解禁过滤。选池：主板60/00、ROE_TTM>12%、净利同比>10%、PE与PB均为正且<30/<5、
   vol60<35%、剔ST/停牌。
6) 20交易日调仓、留存仓调仓日不动（无差额再平衡）、T日14:55信号+次日09:31先卖后买、
   费用与导出全部沿用v4_1。

对照回测设置：2016-01-04~2026-09-01，100万元，中证800基准，频率"天"。
成绩待回填（对照v4_1_2：收益216.36%、年化11.75%、最大回撤24.92%（2021/09/09~
2023/12/20）、超额最大回撤38.35%、夏普0.400）。

策略收益
193.19%
策略年化收益
10.94%
超额收益
150.35%
基准收益
17.11%
阿尔法
0.086
贝塔
0.657
夏普比率
0.371
胜率
0.555
盈亏比
1.573
最大回撤 
24.46%
索提诺比率
0.518
日均超额收益
0.04%
超额收益最大回撤
39.21%
超额收益夏普比率
0.346
日胜率
0.505
盈利次数
116
亏损次数
93
信息比率
0.619
策略波动率
0.187
基准波动率
0.194
最大回撤区间
2024/06/12,2024/11/25
"""

from jqdata import *
from jqfactor import get_factor_values

# 平台通配导入可能覆盖sum/min/max等内置函数，显式使用别名。
import builtins as _b
import math as _math
import pandas as pd

TOP_N = 6
MOMENTUM_DAYS = 252
SKIP_RECENT_DAYS = 21   # 12-2动量：跳过最近21个交易日
INDUSTRY_CAP = 2        # 目标组合内同一申万一级行业最多2只
ROTATE_EVERY = 20
ROE_MIN = 12.0
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
OUTPUT_PREFIX = 'pool_rotation_v4_1_6'
VERBOSE_LOGS = False       # True时显示逐笔委托、调仓信号及买卖受阻日志
DECISION_COLUMNS = ['signal_day', 'asof', 'code', 'action', 'exit_reason', 'pool_reason',
                    'rank', 'previous_rank', 'momentum', 'entry_date', 'held_days_at_signal',
                    'roe_ann', 'profit_yoy', 'pe', 'pb', 'vol60', 'industry_code',
                    'industry_name', 'stat_date', 'pub_date', 'data_error']
HOLDING_COLUMNS = ['code', 'entry_date', 'exit_date', 'holding_days', 'entry_source',
                   'closure_source', 'signal_day', 'exit_reason', 'pool_reason']


class PoolDataError(Exception):
    pass


def initialize(context):
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
    g.target = []
    g.exit_pending = {}      # code -> 退出原因，直到实际持仓清零
    g.pool_size = 0
    g.ranked_size = 0
    g.data_paused = False
    g.last_open_day = None
    g.last_signal_day = None
    g.last_close_day = None
    g.tax_rate = None
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
    run_daily(on_signal, time='14:55')
    log.info('[质量池轮动v4_1_6] N=%d 留存=仍在质量池内即持有，调仓日不动仓（无差额再平衡） '
             '排序=12-2动量(跳过最近%d日的%d日收益) 申万一级行业上限%d只 '
             '每%d日重建；删除解禁过滤'
             % (TOP_N, SKIP_RECENT_DAYS, MOMENTUM_DAYS - SKIP_RECENT_DAYS,
                INDUSTRY_CAP, ROTATE_EVERY))


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
    alerts = ('DATA_PAUSE', 'ORDER_ERROR', 'STALE_SIGNAL', 'BENCHMARK_MISSING')
    if VERBOSE_LOGS or (kind in alerts and key not in g.logged_alerts):
        log.info('[质量池轮动v4_1_6] %s %s %s' % (kind, code, detail))
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
    values = frame.iloc[-1].apply(pd.to_numeric, errors='coerce').replace(
        [_b.float('inf'), -_b.float('inf')], _b.float('nan')).dropna()
    if values.empty:
        raise PoolDataError('No valid roe_ttm values')
    if values.abs().median() < 1.5:    # 小数口径（如0.12=12%）统一放大为百分比
        values = values * 100.0
    return values


def _prepare_fundamentals(rows, asof):
    _require_columns(rows, ('code', 'roe_ttm', 'inc_net_profit_year_on_year',
                            'statDate', 'pubDate'), 'indicator')
    if rows.empty or rows['code'].duplicated().any():
        raise PoolDataError('Empty or duplicate market indicator snapshot')
    ind = rows.copy().set_index('code')
    report = ind['statDate'].apply(_report_date)
    published = pd.to_datetime(ind['pubDate'], errors='coerce')
    cutoff = pd.Timestamp(asof).normalize()
    visible = (report.notna() & published.notna()
               & (report <= cutoff) & (published <= cutoff))
    if not visible.any():
        raise PoolDataError('No visible market indicator snapshot')
    ind = ind.loc[visible].copy()
    for field in ('roe_ttm', 'inc_net_profit_year_on_year'):
        ind[field] = pd.to_numeric(ind[field], errors='coerce')
    ind['roe_ann'] = ind['roe_ttm']
    ind = ind.replace([_b.float('inf'), -_b.float('inf')], _b.float('nan')).dropna(
        subset=['roe_ann', 'inc_net_profit_year_on_year'])
    if ind.empty:
        raise PoolDataError('No valid numeric market indicators')
    return ind


def _history_closes(codes, asof):
    count = _b.max(VOL_LOOKBACK, MOMENTUM_DAYS + 1)
    closes = history(count, '1d', 'close', security_list=codes,
                     skip_paused=False, df=True)
    if (not _b.isinstance(closes, pd.DataFrame)
            or not set(codes) <= set(closes.columns) or _b.len(closes) != count):
        raise PoolDataError('Incomplete daily close matrix')
    dates = pd.to_datetime(closes.index).normalize()
    if (dates.max() != pd.Timestamp(asof).normalize()
            or dates.duplicated().any() or not dates.is_monotonic_increasing):
        raise PoolDataError('Daily close matrix cutoff does not match T-1')
    return closes[codes].apply(pd.to_numeric, errors='coerce').replace(
        [_b.float('inf'), -_b.float('inf')], _b.float('nan'))


def _valid_closes(series):
    return series.notna().all() and (series > 0).all()


def _audit_indicators(audit, universe, raw, valid):
    """只解释本轮已有查询；记录持仓和财务过线候选，不额外取数据。"""
    if audit is None:
        return
    qualifying = valid[(valid['roe_ann'] > ROE_MIN)
                       & (valid['inc_net_profit_year_on_year'] > NP_YOY_MIN)]
    tracked = set(audit) | (universe & set(qualifying.index))
    raw = raw.set_index('code')
    for code in tracked:
        row = audit.setdefault(code, {})
        if code not in universe:
            row['pool_reason'] = 'not_in_mainboard_universe'
        elif code not in raw.index:
            row['pool_reason'] = 'financial_missing'
        elif code not in valid.index:
            # 本轮财务总快照有效，个别股票缺失/未披露/非数值按原规则被排除。
            row['pool_reason'] = 'financial_not_visible_or_invalid'
            row['stat_date'] = _b.str(raw.loc[code, 'statDate'])
            row['pub_date'] = _b.str(raw.loc[code, 'pubDate'])
        else:
            v = valid.loc[code]
            row.update(roe_ann=_b.float(v['roe_ann']),
                       profit_yoy=_b.float(v['inc_net_profit_year_on_year']),
                       stat_date=_b.str(v['statDate']), pub_date=_b.str(v['pubDate']))
            failed = []
            if v['roe_ann'] <= ROE_MIN:
                failed.append('roe_low')
            if v['inc_net_profit_year_on_year'] <= NP_YOY_MIN:
                failed.append('profit_growth_low')
            row['pool_reason'] = '|'.join(failed) if failed else 'passed'


def _audit_reason(audit, codes, reason):
    if audit is not None:
        for code in codes:
            audit.setdefault(code, {})['pool_reason'] = reason


def build_pool(context, asof, audit=None):
    """沿用平台质量池阈值；ROE改用因子库roe_ttm；同时取253根日线，末61根用于vol60；解禁过滤已删除。"""
    dt = context.current_dt
    secs = get_all_securities(['stock'], date=dt)
    if not _b.isinstance(secs, pd.DataFrame) or secs.empty:
        raise PoolDataError('Missing historical stock universe')
    # 使用A股主板正向白名单，同时排除B股、科创、创业和北交所。
    universe = {c for c in secs.index if c[:2] in ('60', '00')}
    ind = get_fundamentals(query(indicator.code, indicator.inc_net_profit_year_on_year,
                           indicator.statDate, indicator.pubDate), date=asof)
    raw_ind = ind
    ind = ind.copy()
    ind['roe_ttm'] = ind['code'].map(_fetch_roe_ttm(universe, asof))
    ind = _prepare_fundamentals(ind, asof)
    _audit_indicators(audit, universe, raw_ind, ind)
    ind = ind[(ind['roe_ann'] > ROE_MIN)
              & (ind['inc_net_profit_year_on_year'] > NP_YOY_MIN)]
    cands = sorted(universe & set(ind.index))
    if not cands:
        return [], pd.DataFrame()
    st = get_extras('is_st', security_list=cands, end_date=dt, count=1)
    if (not _b.isinstance(st, pd.DataFrame) or st.empty
            or not set(cands) <= set(st.columns) or st[cands].iloc[-1].isna().any()):
        raise PoolDataError('Incomplete ST snapshot')
    passing = [c for c in cands if not _b.bool(st[c].iloc[-1])]
    _audit_reason(audit, set(cands) - set(passing), 'st')
    cands = passing
    if not cands:
        return [], pd.DataFrame()
    val = get_fundamentals(query(valuation.code, valuation.pe_ratio,
                           valuation.pb_ratio).filter(valuation.code.in_(cands)), date=asof)
    _require_columns(val, ('code', 'pe_ratio', 'pb_ratio'), 'valuation')
    if val.empty or val['code'].duplicated().any():
        raise PoolDataError('Empty or duplicate valuation snapshot')
    val = val.copy().set_index('code')
    for field in ('pe_ratio', 'pb_ratio'):
        val[field] = pd.to_numeric(val[field], errors='coerce')
    val = val.replace([_b.float('inf'), -_b.float('inf')], _b.float('nan'))
    if audit is not None:
        for code in cands:
            row = audit[code]
            if code not in val.index:
                row['pool_reason'] = 'valuation_missing'
                continue
            v = val.loc[code]
            row.update(pe=v['pe_ratio'], pb=v['pb_ratio'])
            if v[['pe_ratio', 'pb_ratio']].isna().any():
                row['pool_reason'] = 'valuation_invalid'
            else:
                failed = []
                if v['pe_ratio'] <= 0:
                    failed.append('pe_nonpos')
                elif v['pe_ratio'] >= PE_MAX:
                    failed.append('pe_high')
                if v['pb_ratio'] <= 0:
                    failed.append('pb_nonpos')
                elif v['pb_ratio'] >= PB_MAX:
                    failed.append('pb_high')
                row['pool_reason'] = '|'.join(failed) if failed else 'passed'
    val = val.dropna(subset=['pe_ratio', 'pb_ratio'])
    if val.empty:
        raise PoolDataError('No valid candidate valuations')
    # PE/PB须为正：资不抵债股（负权益）的ROE_TTM为负/负的正假象，四道筛选对其同时失效
    val = val[(val['pe_ratio'] > 0) & (val['pe_ratio'] < PE_MAX)
              & (val['pb_ratio'] > 0) & (val['pb_ratio'] < PB_MAX)]
    cd = get_current_data()
    passing = [c for c in cands if c in val.index and not cd[c].paused]
    _audit_reason(audit, (set(cands) & set(val.index)) - set(passing), 'paused')
    cands = passing
    if not cands:
        return [], pd.DataFrame()
    closes = _history_closes(cands, asof)
    passing = [c for c in cands if _valid_closes(closes[c].iloc[-VOL_LOOKBACK:])]
    _audit_reason(audit, set(cands) - set(passing), 'vol_history_missing')
    cands = passing
    if not cands:
        return [], closes
    rets = closes[cands].iloc[-VOL_LOOKBACK:].pct_change().iloc[1:]
    vol = rets.std(ddof=1) * _math.sqrt(250) * 100
    if audit is not None:
        for code in cands:
            audit[code]['vol60'] = _b.float(vol[code])
    _audit_reason(audit, [c for c in cands if not vol[c] < VOL60_MAX], 'vol_high')
    cands = [c for c in cands if vol[c] < VOL60_MAX]
    return cands, closes


def _sw_industries(codes, asof):
    """申万一级行业PIT映射（按T-1快照）；缺映射的股票自成一组，不受行业上限约束。"""
    data = get_industry(_b.list(codes), date=asof)
    if not _b.isinstance(data, dict) or (codes and not data):
        raise PoolDataError('Incomplete industry snapshot')
    industries = {}
    for code in codes:
        node = data.get(code) or {}
        sw = node.get('sw_l1') or {}
        industries[code] = (_b.str(sw.get('industry_code') or code),
                            _b.str(sw.get('industry_name') or ''))
    return industries


def rank_pool(pool, closes):
    """12-2动量：倒数第22根/倒数第253根收盘-1，即跳过最近21个交易日、
    跨231个交易日的收益（截至T-1）。"""
    scores = {}
    for code in pool:
        series = closes[code].iloc[-(MOMENTUM_DAYS + 1):-SKIP_RECENT_DAYS]
        if _b.len(series) == MOMENTUM_DAYS + 1 - SKIP_RECENT_DAYS and _valid_closes(series):
            scores[code] = _b.float(series.iloc[-1] / series.iloc[0] - 1)
    ranked = sorted(scores, key=lambda c: (-scores[c], c))
    return ranked, scores


def select_targets(ranked, holdings, industries, pool):
    """仍在质量池内的旧仓全部留存（v4_1_2取消名次留存线；留存优先，不因行业上限
    强制退出），再从前6名按名次补空位；目标组合内同一申万一级行业最多2只，
    受限候选不顺延到第7名。调用方已保证持仓的池内成员均可排名。"""
    if _b.len(set(ranked)) != _b.len(ranked):
        raise ValueError('Duplicate ranked code')
    rank = {c: i + 1 for i, c in _b.enumerate(ranked)}
    kept = sorted((c for c in set(holdings) if c in pool), key=lambda c: rank[c])
    if _b.len(kept) > TOP_N:
        raise ValueError('More eligible existing positions than TOP_N')
    target = list(kept)
    counts = {}
    for code in kept:
        key = industries[code][0]
        counts[key] = counts.get(key, 0) + 1
    chosen = set(kept)
    cap_blocked = []
    for code in ranked[:TOP_N]:
        if _b.len(target) >= TOP_N:
            break
        if code in chosen:
            continue
        key = industries[code][0]
        if counts.get(key, 0) >= INDUSTRY_CAP:
            cap_blocked.append(code)
            continue
        target.append(code)
        chosen.add(code)
        counts[key] = counts.get(key, 0) + 1
    return target, kept, cap_blocked


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


def on_signal(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_signal_day == today:
        return
    g.last_signal_day = today
    g.day += 1
    if (g.day - 1) % ROTATE_EVERY:
        return
    audit = {c: {} for c in _held(context)}
    asof = None
    try:
        asof = _prev_trade_day(context.current_dt)
        pool, closes = build_pool(context, asof, audit=audit)
        ranked, scores = rank_pool(pool, closes)
        held = _held(context)
        if held & (set(pool) - set(ranked)):
            raise PoolDataError('Existing pool holding has incomplete momentum history')
        industries = _sw_industries(ranked, asof)
        for code in ranked:
            row = audit.setdefault(code, {})
            row['industry_code'], row['industry_name'] = industries[code]
        selected, kept, cap_blocked = select_targets(ranked, held, industries, set(pool))
        if (held & set(pool)) - set(selected):
            raise ValueError('Pool holding not retained')
    except Exception as exc:
        g.pending = None
        g.data_paused = True
        error = '%s: %s' % (_b.type(exc).__name__, exc)
        for code in sorted(_held(context)):
            _decision(context, asof, code, 'data_pause', audit, data_error=error)
        _event(context, 'DATA_PAUSE', detail=error)
        return
    rank = {c: i + 1 for i, c in _b.enumerate(ranked)}
    g.data_paused = False
    g.pool_size, g.ranked_size = _b.len(pool), _b.len(ranked)
    g.pending = {'signal_day': today, 'selected': selected,
                 'exit_reasons': {c: 'out_of_pool' for c in held - set(selected)}}
    decisions = {}
    for code in sorted(held | set(selected)):
        action = ('keep' if code in held else 'entry') if code in selected else 'exit'
        decisions[code] = _decision(context, asof, code, action, audit,
                                    rank=rank.get(code), score=scores.get(code),
                                    exit_reason=g.pending['exit_reasons'].get(code, ''))
    g.pending['decisions'] = decisions
    g.pending['exit_details'] = {c: decisions[c] for c in g.pending['exit_reasons']}
    g.signal_rows.append({'signal_day': _b.str(today.date()), 'asof': _b.str(asof.date()),
                          'pool_size': _b.len(pool), 'ranked_size': _b.len(ranked),
                          'kept': ';'.join(kept), 'selected': ';'.join(selected),
                          'cap_blocked': ';'.join(cap_blocked),
                          'planned_exits': _b.len(g.pending['exit_reasons'])})
    cap_blocked_set = set(cap_blocked)
    for code in sorted(pool):
        row = dict(audit.get(code, {}))
        row.update(signal_day=_b.str(today.date()), code=code,
                   momentum=scores.get(code, _b.float('nan')), rank=rank.get(code),
                   selected=code in selected, kept=code in kept,
                   entry_cap_blocked=code in cap_blocked_set,
                   ranking_status='ranked' if code in scores else 'momentum_history_missing')
        g.member_rows.append(row)
    g.previous_ranks = rank
    _event(context, 'SIGNAL', detail='池%d 可排名%d 留存%d 目标%d 行业受限%d 计划退出%d' % (
        _b.len(pool), _b.len(ranked), _b.len(kept), _b.len(selected),
        _b.len(cap_blocked), _b.len(g.pending['exit_reasons'])))


def _price(cd, code):
    try:
        value = _b.float(cd[code].last_price)
    except (TypeError, ValueError):
        return None
    return value if _math.isfinite(value) and value > 0 else None


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
    px = _price(cd, code)
    blocked = ('paused' if cd[code].paused else 'invalid_price' if px is None
               else 'lower_limit' if px <= cd[code].low_limit + LIMIT_PAD else '')
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


def on_open(context):
    today = pd.Timestamp(context.current_dt).normalize()
    if g.last_open_day == today:
        return
    g.last_open_day = today
    _set_costs(context)
    plan = g.pending
    g.pending = None
    if plan is not None and _prev_trade_day(context.current_dt) != plan['signal_day']:
        _event(context, 'STALE_SIGNAL', detail=_b.str(plan['signal_day'].date()))
        plan = None
    cd = get_current_data()
    open_orders = get_open_orders()
    open_codes = {o.security for o in open_orders.values()}
    if plan is not None:
        g.target = plan['selected']
        # 新一轮重新决定退出；重新入选的旧待退出仓可取消退出计划。
        g.exit_pending = plan['exit_reasons']
        g.exit_details = plan.get('exit_details', {})
        g.current_decisions = plan.get('decisions', {})
        per = context.portfolio.total_value / TOP_N
    for code, reason in sorted(g.exit_pending.items()):
        if code in _held(context) and code not in open_codes:
            _sell(context, cd, code, 0, reason)
    g.exit_pending = {c: r for c, r in g.exit_pending.items() if c in _held(context)}
    if plan is None:
        return
    # v4_1_6消融：调仓日不动留存仓——不减持超额、不补足缺口；只按预算买入未持有的新仓。
    reserved = _held(context) | {o.security for o in open_orders.values() if o.is_buy}
    for code in g.target:
        if code in _held(context) or code in open_codes:
            continue
        px = _price(cd, code)
        blocked = ('paused' if cd[code].paused else 'ST' if cd[code].is_st
                   else 'invalid_price' if px is None
                   else 'upper_limit' if px >= cd[code].high_limit - LIMIT_PAD else '')
        if blocked:
            _event(context, 'BUY_BLOCKED', code, blocked)
            continue
        # 此处只处理新仓（current恒为0），留存仓已在上行被跳过。
        if code not in reserved and _b.len(reserved) >= TOP_N:
            _event(context, 'BUY_BLOCKED', code, 'pending exit occupies slot')
            continue
        desired = _b.int(per / px // 100) * 100
        delta = _b.min(desired, _affordable_shares(context.portfolio.available_cash, px))
        # 零股来自公司行动，买入增量仍必须为100股整数倍。
        delta = _b.int(delta // 100) * 100
        if delta < 100:
            _event(context, 'BUY_BLOCKED', code, 'cash/lot')
            continue
        if _submit(context, code, delta, 'equal_weight') is not None:
            reserved.add(code)


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
    log.info('[诊断汇总] 原因表示依次筛选的首个失败环节；同环节多个失败以|连接。'
             '全批数据暂停不计为退出；平仓盈亏须与实际成交表配对，未在这里推算。')


def on_strategy_end(context):
    if not g.nav_rows:
        log.info('[质量池轮动v4_1_6] 无收盘净值；检查平台收盘回调')
        return
    df = pd.DataFrame(g.nav_rows)
    nav = df['equity'] / df['equity'].iloc[0]
    drawdown = (nav / nav.cummax() - 1).min()
    log.info('[质量池轮动v4_1_6] 实际成交净值 收益=%+.2f%% 绝对最大回撤=%.2f%%'
             % ((nav.iloc[-1] - 1) * 100, drawdown * 100))
    if df['benchmark_close'].notna().all() and _b.len(df) > 1:
        benchmark = df['benchmark_close'] / df['benchmark_close'].iloc[0]
        relative = nav / benchmark
        ann_excess = relative.iloc[-1] ** (ANNUAL_DAYS / (_b.len(df) - 1)) - 1
        relative_dd = (relative / relative.cummax() - 1).min()
        active = nav.pct_change().iloc[1:] - benchmark.pct_change().iloc[1:]
        sharpe = (active.mean() / active.std(ddof=1) * _math.sqrt(ANNUAL_DAYS)
                  if active.std(ddof=1) > 0 else _b.float('nan'))
        log.info('[质量池轮动v4_1_6] vs800 年化相对收益=%+.2f%% 相对最大回撤=%.2f%% '
                 '日超额夏普=%+.3f' % (ann_excess * 100, relative_dd * 100, sharpe))
    else:
        log.info('[质量池轮动v4_1_6] 基准缺测或区间不足，不输出超额成绩')
    log.info('[质量池轮动v4_1_6] 平均持仓=%.1f 平均现金=%.1f%% 数据异常轮数=%d；'
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
                 '回到根目录并刷新；找到pool_rotation_v4_1_6_开头的CSV，勾选后下载。'
                 '文件在聚宽网站上，不会自动保存到电脑。')
        log.info('[文件位置] 优先下载 %s_decisions.csv 和 %s_holding_periods.csv；'
                 '这些过程表由策略导出，入口为投资研究的文件列表。'
                 % (OUTPUT_PREFIX, OUTPUT_PREFIX))
