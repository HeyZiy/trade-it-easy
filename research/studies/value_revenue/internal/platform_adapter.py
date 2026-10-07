"""聚宽适配层，数据导入与引擎注入函数均按平台标准全局入口调用。

本文件供build_platform.py组装；组装时限定所有Python内置函数引用，
粘贴运行研究目录的study_jq.py。
"""
import json
from jqdata import *


ROTATE_EVERY = 20
HORIZONS = (20, 60, 120)
PRIMARY_HORIZON = 60
MIN_LISTING_DAYS = 365  # 自然日；另外要求完整121根收盘
MAX_REPORT_AGE = 240   # 自然日；不把长期未更新的财务当作当前经营信号
MIN_IC_N = 100
MIN_CELL_N = 20
N_GROUPS = 5
QUERY_BATCH = 400
BENCHMARK = '000906.XSHG'
OUTPUT_PREFIX = 'expectations_v1_'
STUDY_VERSION = 'expectations_v1_2026-10-06_runtime_fix'


def initialize(context):
    if (PRIMARY_HORIZON not in HORIZONS or ROTATE_EVERY < 1 or not HORIZONS
            or any(isinstance(h, bool) or not isinstance(h, int) or h < 1 for h in HORIZONS)
            or len(set(HORIZONS)) != len(HORIZONS)):
        raise ValueError('Invalid HORIZONS/PRIMARY_HORIZON/ROTATE_EVERY')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    log.set_level('order', 'error')
    g.day = -1
    g.pending = None
    g.active = []
    g.rows = []
    g.audit = []
    g.written = set()
    g.previous_cells = {(mode, h, cell): None for mode in ('raw', 'neutral')
                        for h in HORIZONS for cell in CELL_NAMES}
    g.signal_count = 0
    g.first_day = None
    g.last_day = None
    run_daily(on_open, time='09:31')
    run_daily(on_signal, time='14:55')
    log.info('预期差v1：盈利收益率×营收同比加速；主窗口60日，辅助20/120日；'
                '全A主板正PE，无质量/估值上限/低波/解禁筛选；不下单。')


def chunks(codes):
    for start in range(0, len(codes), QUERY_BATCH):
        yield codes[start:start + QUERY_BATCH]


def export_frame(name, frame):
    """平台文件写失败直接报错；不宣布导出成功，不悄悄丢失逐期证据。"""
    if frame.empty:
        return
    path = OUTPUT_PREFIX + name + '.csv'
    first = path not in g.written
    write_file(path, frame.to_csv(index=False, header=first), append=not first)
    g.written.add(path)


def price_frame(codes, asof, fields, count=None):
    frames = []
    for batch in chunks(codes):
        kwargs = dict(end_date=asof, fields=fields, frequency='daily', panel=False,
                      fq='pre', skip_paused=False, fill_paused=False)
        if count is None:
            kwargs['start_date'] = asof
        else:
            kwargs['count'] = count
        data = get_price(batch, **kwargs)
        if data is not None and not data.empty:
            frames.append(data)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=['code', 'time'] + fields)


def build_snapshot(asof):
    """所有筛选、财务、估值、控制变量截止T-1；请求失败终止而非放行。"""
    asof = pd.Timestamp(asof).normalize()
    securities = get_all_securities(['stock'], date=asof.date())
    codes = [c for c in securities.index
             if c[:2] in ('60', '00')
             and pd.notna(securities.loc[c, 'start_date'])
             and (asof - pd.Timestamp(securities.loc[c, 'start_date'])).days >= MIN_LISTING_DAYS]
    audit = dict(asof=asof, listed_mainboard_n=len(codes), nonst_unpaused_n=0,
                 positive_pe_n=0, financial_pair_n=0, common_sample_n=0)
    if not codes:
        return empty_snapshot(), audit
    eligible = []
    for batch in chunks(codes):
        st = get_extras('is_st', batch, start_date=asof, end_date=asof, df=True)
        eligible += [c for c in batch if c in st and not st.empty
                     and pd.notna(st[c].iloc[-1]) and not bool(st[c].iloc[-1])]
    status = price_frame(eligible, asof, ['paused'])
    if not status.empty:
        status = status.set_index('code')['paused']
    else:
        status = pd.Series(dtype=float)
    codes = [c for c in eligible if c in status.index and status.loc[c] == 0]
    audit['nonst_unpaused_n'] = len(codes)
    if not codes:
        return empty_snapshot(), audit
    valuations, financials = [], []
    for batch in chunks(codes):
        valuations.append(get_fundamentals(query(valuation.code, valuation.pe_ratio,
                                                 valuation.market_cap).filter(
                                                     valuation.code.in_(batch)), date=asof))
        financials.append(get_history_fundamentals(
            batch, [indicator.statDate, indicator.pubDate,
                    indicator.inc_revenue_year_on_year, indicator.roe],
            watch_date=asof, count=2, interval='1q', stat_by_year=False))
    val = pd.concat(valuations, ignore_index=True).set_index('code')
    if val.index.has_duplicates:
        raise ValueError('Duplicate valuation codes')
    val['pe_ratio'], val['market_cap'] = clean(val['pe_ratio']), clean(val['market_cap'])
    val = val[(val['pe_ratio'] > 0) & (val['market_cap'] > 0)]
    audit['positive_pe_n'] = len(val)
    fin = prepare_financials(pd.concat(financials, ignore_index=True), asof, MAX_REPORT_AGE)
    frame = val.join(fin, how='inner')
    audit['financial_pair_n'] = len(frame)
    frame['value'] = 1.0 / frame['pe_ratio']
    if frame.empty:
        return empty_snapshot(), audit
    prices = price_frame(list(frame.index), asof, ['close'], count=121)
    prices['time'] = pd.to_datetime(prices['time']).dt.normalize()
    closes = prices.pivot(index='time', columns='code', values='close').reindex(columns=frame.index)
    momentum = pd.Series(np.nan, index=frame.index, dtype=float)
    if len(closes) == 121 and closes.index[-1] == asof:
        valid = closes.notna().all() & (closes > 0).all()
        momentum = clean(closes.iloc[-1] / closes.iloc[0] - 1).where(valid)
    frame['momentum120'] = momentum
    industries = {}
    for batch in chunks(list(frame.index)):
        industries.update(get_industry(batch, date=asof))
    frame['industry'] = pd.Series({c: industries.get(c, {}).get('sw_l1', {}).get('industry_code')
                                  for c in frame.index}, dtype=object)
    # 同一完整样本比较原始与控制口径；缺失控制变量不补零。
    frame = frame.dropna(subset=['value', 'improvement', 'revenue_yoy', 'market_cap',
                                'momentum120', 'industry'])
    audit['common_sample_n'] = len(frame)
    return frame, audit


def empty_snapshot():
    return pd.DataFrame(columns=['value', 'improvement', 'industry', 'market_cap',
                                 'momentum120', 'revenue_yoy'])


def on_signal(context):
    if g.day < 0 or g.day % ROTATE_EVERY:
        return
    signal = pd.Timestamp(context.current_dt).normalize()
    calendar = get_trade_days(end_date=signal.date(), count=2)
    if len(calendar) != 2:
        raise ValueError('No T-1 trading date')
    asof = pd.Timestamp(calendar[0]).normalize()
    frame, audit = build_snapshot(asof)
    views = make_views(frame)
    cells = {mode: assign_cells(view['value'], view['improvement']) for mode, view in views.items()}
    snapshot = frame.copy()
    snapshot['code'] = snapshot.index
    snapshot['signal'], snapshot['asof'] = signal, asof
    for mode, view in views.items():
        snapshot[mode + '_cell'] = cells[mode]
        for name in FACTOR_NAMES:
            snapshot[mode + '_' + name] = view[name]
        audit[mode + '_factor_n'] = int(view['value'].notna().sum())
    audit['signal'] = signal
    g.audit.append(audit)
    export_frame('snapshots', snapshot.reset_index(drop=True))
    export_frame('audit', pd.DataFrame([audit]))
    g.signal_count += 1
    g.pending = dict(signal=signal, asof=asof, frame=frame, views=views, cells=cells,
                     completed=set())
    log.info('冻结信号 %s | 主板上市=%d | 完整样本=%d | 中性有效=%d' % (
        signal.date(), audit['listed_mainboard_n'], len(frame), audit['neutral_factor_n']))


def opening_quotes(codes):
    current = get_current_data()
    quotes = pd.Series(np.nan, index=codes, dtype=float)
    for code in codes:
        try:
            quote = current[code]
        except KeyError:
            continue  # 退市等导致的端点缺报价：保留成员和缺测，不终止全轮
        if quote is None:
            continue
        value = quote.day_open
        if not quote.paused and value is not None and np.isfinite(value) and value > 0:
            quotes.loc[code] = value
    return quotes


def on_open(context):
    g.day += 1
    date = pd.Timestamp(context.current_dt).normalize()
    g.first_day = date if g.first_day is None else g.first_day
    g.last_day = date
    due = [(batch, h) for batch in g.active for h in HORIZONS
           if g.day - batch['entry_day'] == h and h not in batch['completed']]
    codes = {BENCHMARK}
    for batch, _ in due:
        codes.update(batch['frame'].index)
    if g.pending is not None:
        if date <= g.pending['signal']:
            raise ValueError('Entry must follow signal')
        codes.update(g.pending['frame'].index)
    if due or g.pending is not None:
        quotes = opening_quotes(sorted(codes))
    for batch, horizon in due:
        finish_period(batch, horizon, date, quotes)
        batch['completed'].add(horizon)
    g.active = [batch for batch in g.active if len(batch['completed']) < len(HORIZONS)]
    if g.pending is not None:
        batch = g.pending
        batch['entry'], batch['entry_day'] = date, g.day
        batch['entry_valid'] = quotes.reindex(list(batch['frame'].index) + [BENCHMARK]).notna()
        g.active.append(batch)
        g.pending = None


def finish_period(batch, horizon, date, quotes):
    codes = list(batch['frame'].index) + [BENCHMARK]
    if not batch['asof'] < batch['signal'] < batch['entry'] < date:
        raise ValueError('Invalid asof/signal/entry/exit ordering')
    # 到期后重查入场开盘，沿用既有平台诊断的动态前复权口径。
    history = price_frame(codes, batch['entry'], ['open'])
    opening = (history.set_index('code')['open'].reindex(codes) if not history.empty
               else pd.Series(np.nan, index=codes, dtype=float))
    future = clean(quotes.reindex(codes) / opening - 1).where(
        (opening > 0) & batch['entry_valid'].reindex(codes, fill_value=False))
    returns = pd.DataFrame(dict(code=codes, future_return=future.reindex(codes).values,
                                entry_valid=batch['entry_valid'].reindex(codes).values))
    returns['signal'], returns['entry'], returns['exit'], returns['horizon'] = (
        batch['signal'], batch['entry'], date, horizon)
    export_frame('returns', returns)
    rows = []
    for mode, view in batch['views'].items():
        cells = batch['cells'][mode]
        stats = evaluate_snapshot(batch['frame'], view, cells, future,
                                  N_GROUPS, MIN_IC_N, MIN_CELL_N)
        stats.update(mode=mode, horizon=horizon, signal=batch['signal'], asof=batch['asof'],
                     entry=batch['entry'], exit=date, pool_n=len(batch['frame']),
                     benchmark=future.get(BENCHMARK, np.nan))
        for name in CELL_NAMES:
            current = set(cells.index[cells == name])
            key = (mode, horizon, name)
            stats[name + '_membership_turnover'] = membership_turnover(g.previous_cells[key], current)
            g.previous_cells[key] = current or None
        rows.append(stats)
        g.rows.append(stats)
        if horizon == PRIMARY_HORIZON and mode == 'raw':
            display = {name: stats[key] * 100 for name, key in (
                ('60日交互差_pp', 'interaction'), ('60日配对覆盖_pct', 'value_coverage'))
                       if np.isfinite(stats[key])}
            if display:
                record(**display)
    export_frame('periods', pd.DataFrame(rows))
    primary = rows[0]
    log.info('完成 %s→%s %d日 | 配对覆盖=%.1f%% | 四格交互差=%s' % (
        batch['entry'].date(), date.date(), horizon, primary['value_coverage'] * 100,
        format_number(primary['interaction'], 100)))
    log.info('  四格有效/冻结人数 %s | EP_IC=%s 改善_IC=%s 回归交互系数=%s' % (
        ' '.join('%s:%d/%d' % (name, primary[name + '_valid_n'], primary[name + '_n'])
                 for name in CELL_NAMES),
        format_number(primary['value_ic']), format_number(primary['improvement_ic']),
        format_number(primary['regression_interaction'])))


def format_number(value, scale=1):
    return 'NA' if not np.isfinite(value) else '%+.5f' % (value * scale)


def on_strategy_end(context):
    log.info('[预期差诊断汇总] 报价口径，不含费用/滑点/成交限制；不是策略收益。')
    summaries = []
    if g.rows:
        table = pd.DataFrame(g.rows)
        for (mode, horizon), block in table.groupby(['mode', 'horizon']):
            block = block.sort_values('signal')
            records = summary_records(block, int(horizon), ROTATE_EVERY)
            for row in records:
                row['mode'] = mode
            summaries += records
            for row in records:
                if row['metric'] in ('interaction', 'regression_interaction', 'value_ic',
                                     'improvement_ic', 'additive_ic', 'product_ic'):
                    log.info('%s %d日 %s %s | 有效=%d/%d 均值=%s t_HAC=%s' % (
                        mode, horizon, row['scope'], row['metric'], row['n'], row['scheduled_n'],
                        format_number(row['mean']), format_number(row.get('t_hac', np.nan))))
            common = block[list(CELL_NAMES)].dropna()
            log.info('  四格共同有效=%d/%d；A/B/C/D均值=%s；平均配对覆盖=%.1f%%' % (
                len(common), len(block), common.mean().to_dict(), block['value_coverage'].mean() * 100))
    export_frame('summary', pd.DataFrame(summaries))
    unfinished = sum(len(HORIZONS) - len(b['completed']) for b in g.active)
    metadata = dict(version=STUDY_VERSION, first_day=str(g.first_day), last_day=str(g.last_day),
                    horizons=list(HORIZONS), primary_horizon=PRIMARY_HORIZON,
                    rotate_every=ROTATE_EVERY, min_ic_n=MIN_IC_N, min_cell_n=MIN_CELL_N,
                    min_listing_days=MIN_LISTING_DAYS, max_report_age=MAX_REPORT_AGE,
                    query_batch=QUERY_BATCH, n_groups=N_GROUPS, benchmark=BENCHMARK,
                    signal_count=g.signal_count, unfinished_horizons=unfinished,
                    pending_signal=g.pending is not None,
                    factor='EP=1/PE(TTM); improvement=latest quarterly revenue YoY minus prior quarter (pp)',
                    universe='historical mainboard, listing>=365 calendar days, non-ST, unpaused at T-1, PE>0',
                    controls='industry/log cap/momentum120/current revenue YoY',
                    strict_cell_missing=True, historical_revisions_audited=False,
                    exported_csv=sorted(g.written))
    write_file(OUTPUT_PREFIX + 'metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
    log.info('[导出完成] %d张CSV+metadata.json；文件在聚宽投资研究根目录，前缀%s。'
                '未完成窗口=%d，未生效信号=%d。' % (
                    len(g.written), OUTPUT_PREFIX, unfinished, int(g.pending is not None)))
    log.info('四格：(B-A)-(D-C)，A低估值弱改善/B低估值强改善/C高估值弱改善/D高估值强改善。'
                '历史区间已经观察过；正交互差不是已验证的可交易alpha。')
