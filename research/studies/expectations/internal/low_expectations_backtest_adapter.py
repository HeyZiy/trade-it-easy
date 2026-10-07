"""独立聚宽诊断：自行取数，按到期窗口计算报价收益，不依赖旧文件。"""
import json
from jqdata import *


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

def format_number(value, scale=1):
    return 'NA' if not np.isfinite(value) else '%+.5f' % (value * scale)


ROTATE_EVERY = 20
HORIZONS = (20, 60, 120)
PRIMARY_HORIZON = 60
MIN_LISTING_DAYS = 365
MAX_REPORT_AGE = 240
MIN_IC_N = 100
MIN_CELL_N = 20
QUERY_BATCH = 400
BENCHMARK = '000906.XSHG'
OUTPUT_PREFIX = 'low_expectations_v2_standalone_'
STUDY_VERSION = 'low_expectations_v2_standalone_2026-10-06'


def initialize(context):
    if (PRIMARY_HORIZON not in HORIZONS or ROTATE_EVERY < 1 or not HORIZONS
            or any(isinstance(h, bool) or not isinstance(h, int) or h < 1 for h in HORIZONS)
            or len(set(HORIZONS)) != len(HORIZONS)):
        raise ValueError('Invalid HORIZONS/PRIMARY_HORIZON/ROTATE_EVERY')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark(BENCHMARK)
    log.set_level('order', 'error')
    g.day, g.pending = -1, None
    g.active, g.rows, g.audit = [], [], []
    g.written = set()
    g.signal_count, g.eligible_records = 0, 0
    g.first_day, g.last_day = None, None
    run_daily(on_open, time='09:31')
    run_daily(on_signal, time='14:55')
    log.info('低预期＋盈利维持v2独立实验：无需上传旧CSV。'
             '个位数PE(TTM) × 已披露TTM归母利润不下降；每20日冻结，主60日。'
             '诊断脚本不下单，请看日志与导出，空账户收益不是实验结果。')


def empty_profit_snapshot():
    frame = pd.DataFrame(columns=['code', 'signal', 'asof', 'report', 'pe_ratio',
                                  'market_cap', 'industry', 'momentum120',
                                  'low_expectations', 'profit_maintained', 'cell'])
    for name in ('low_expectations', 'profit_maintained'):
        frame[name] = frame[name].astype(bool)
    return frame


def build_profit_snapshot(asof, signal):
    """T-1截面；利润仅取已披露八个单季度，控制变量完整后冻结共同样本。"""
    asof, signal = pd.Timestamp(asof).normalize(), pd.Timestamp(signal).normalize()
    securities = get_all_securities(['stock'], date=asof.date())
    codes = [c for c in securities.index if c[:2] in ('60', '00')
             and pd.notna(securities.loc[c, 'start_date'])
             and (asof - pd.Timestamp(securities.loc[c, 'start_date'])).days >= MIN_LISTING_DAYS]
    audit = dict(signal=signal, asof=asof, listed_mainboard_n=len(codes),
                 nonst_unpaused_n=0, positive_pe_n=0, profit_eligible_n=0,
                 control_complete_n=0, common_sample_n=0, A_n=0, B_n=0, C_n=0, D_n=0,
                 profit_exclusions='{}')
    empty = empty_profit_snapshot()
    no_features = pd.DataFrame(columns=['code', 'signal', 'asof', 'profit_status'])
    if not codes:
        return empty, empty.copy(), no_features, audit
    eligible = []
    for batch in chunks(codes):
        st = get_extras('is_st', batch, start_date=asof, end_date=asof, df=True)
        eligible += [c for c in batch if c in st and not st.empty
                     and pd.notna(st[c].iloc[-1]) and not bool(st[c].iloc[-1])]
    status = price_frame(eligible, asof, ['paused'])
    status = status.set_index('code')['paused'] if not status.empty else pd.Series(dtype=float)
    codes = [c for c in eligible if c in status.index and status.loc[c] == 0]
    audit['nonst_unpaused_n'] = len(codes)
    if not codes:
        return empty, empty.copy(), no_features, audit
    valuations = []
    for batch in chunks(codes):
        valuations.append(get_fundamentals(query(valuation.code, valuation.pe_ratio,
                                                 valuation.market_cap).filter(
                                                     valuation.code.in_(batch)), date=asof))
    val = pd.concat(valuations, ignore_index=True).set_index('code')
    if val.index.has_duplicates or set(val.index) - set(codes):
        raise ValueError('Invalid valuation membership')
    val['pe_ratio'], val['market_cap'] = clean(val.pe_ratio), clean(val.market_cap)
    val = val[(val.pe_ratio > 0) & (val.market_cap > 0)]
    audit['positive_pe_n'] = len(val)
    if val.empty:
        return empty, empty.copy(), no_features, audit
    financials = []
    for batch in chunks(list(val.index)):
        rows = get_history_fundamentals(
            batch, [income.statDate, income.pubDate, income.np_parent_company_owners],
            watch_date=asof, count=8, interval='1q', stat_by_year=False)
        if rows is None or set(HISTORY_COLUMNS) - set(rows.columns):
            raise ValueError('Missing profit history schema')
        if set(rows.code) - set(batch):
            raise ValueError('Unexpected profit history membership')
        financials.append(rows[list(HISTORY_COLUMNS)])
    history = pd.concat(financials, ignore_index=True)
    # 保留取回的全部记录，让财务验证明确排除未披露、重复、缺季等问题。
    history['statDate'] = history.statDate.apply(report_date)
    snapshot = val.reset_index()
    snapshot['signal'], snapshot['asof'] = signal, asof
    snapshot['report'] = snapshot.code.map(history.groupby('code').statDate.max())
    features = prepare_profit_features(history, snapshot, MAX_REPORT_AGE)
    export_frame('profit_history', history.assign(signal=signal, asof=asof))
    audit['profit_eligible_n'] = int((features.profit_status == 'ok').sum())
    excluded = features.loc[features.profit_status != 'ok', 'profit_status'].value_counts()
    audit['profit_exclusions'] = json.dumps({key: int(value) for key, value in excluded.items()},
                                           ensure_ascii=False, sort_keys=True)
    prices = price_frame(list(val.index), asof, ['close'], count=121)
    prices['time'] = pd.to_datetime(prices['time']).dt.normalize()
    closes = prices.pivot(index='time', columns='code', values='close').reindex(columns=val.index)
    momentum = pd.Series(np.nan, index=val.index, dtype=float)
    if len(closes) == 121 and closes.index[-1] == asof:
        valid = closes.notna().all() & (closes > 0).all()
        momentum = clean(closes.iloc[-1] / closes.iloc[0] - 1).where(valid)
    snapshot['momentum120'] = snapshot.code.map(momentum)
    industries = {}
    for batch in chunks(list(val.index)):
        industries.update(get_industry(batch, date=asof))
    snapshot['industry'] = snapshot.code.map({
        c: industries.get(c, {}).get('sw_l1', {}).get('industry_code') for c in val.index})
    controlled = snapshot.dropna(subset=['industry', 'momentum120'])
    audit['control_complete_n'] = len(controlled)
    common = freeze_profit_groups(controlled, features).set_index('code', drop=False)
    audit['common_sample_n'] = len(common)
    for name in ('A', 'B', 'C', 'D'):
        audit[name + '_n'] = int((common.cell == name).sum())
    return common, snapshot, features, audit


def on_signal(context):
    if g.day < 0 or g.day % ROTATE_EVERY:
        return
    signal = pd.Timestamp(context.current_dt).normalize()
    calendar = get_trade_days(end_date=signal.date(), count=2)
    if len(calendar) != 2:
        raise ValueError('No T-1 trading date')
    asof = pd.Timestamp(calendar[0]).normalize()
    frame, snapshot, features, audit = build_profit_snapshot(asof, signal)
    export_frame('snapshots', snapshot.reset_index(drop=True))
    export_frame('profit_features', features)
    export_frame('frozen_groups', frame.reset_index(drop=True))
    export_frame('audit', pd.DataFrame([audit]))
    exposures = []
    for name in GROUP_NAMES:
        members = frame[frame.low_expectations] if name == 'LOW' else frame[frame.cell == name]
        for industry, piece in members.groupby('industry'):
            exposures.append(dict(signal=signal, group=name, industry=industry, n=len(piece),
                                  share=len(piece) / len(members), mean_pe=piece.pe_ratio.mean(),
                                  mean_log_cap=np.log(piece.market_cap).mean()))
    export_frame('industry_exposure', pd.DataFrame(exposures))
    g.audit.append(audit)
    g.eligible_records += len(frame)
    g.signal_count += 1
    # 空样本也记录成熟窗口，避免压缩时间轴及HAC缺期位置。
    g.pending = dict(signal=signal, asof=asof, frame=frame, completed=set())
    log.info('冻结 %s | 主板=%d 正PE=%d 八季利润有效=%d 共同样本=%d | A/B/C/D=%d/%d/%d/%d'
             % (signal.date(), audit['listed_mainboard_n'], audit['positive_pe_n'],
                audit['profit_eligible_n'], len(frame), audit['A_n'], audit['B_n'],
                audit['C_n'], audit['D_n']))
    if audit['profit_exclusions'] != '{}':
        log.info('利润排除原因：' + audit['profit_exclusions'])


def finish_period(batch, horizon, date, quotes):
    codes = list(batch['frame'].index) + [BENCHMARK]
    if not batch['asof'] < batch['signal'] < batch['entry'] < date:
        raise ValueError('Invalid asof/signal/entry/exit ordering')
    # 到期时重查历史入场开盘，使其与当前真实报价处于同一动态前复权参照。
    history = price_frame(codes, batch['entry'], ['open'])
    opening = (history.set_index('code')['open'].reindex(codes) if not history.empty
               else pd.Series(np.nan, index=codes, dtype=float))
    entry_valid = batch['entry_valid'].reindex(codes, fill_value=False)
    exit_open = quotes.reindex(codes)
    future = clean(exit_open / opening - 1).where((opening > 0) & entry_valid)
    returns = pd.DataFrame(dict(code=codes, future_return=future.values,
                                entry_valid=entry_valid.values,
                                exit_valid=exit_open.notna().values,
                                entry_adjusted_open=opening.values, exit_open=exit_open.values))
    returns['signal'], returns['entry'], returns['exit'], returns['horizon'] = (
        batch['signal'], batch['entry'], date, horizon)
    export_frame('returns', returns)
    row = evaluate_profit_period(batch['frame'], future, future.get(BENCHMARK, np.nan),
                                 MIN_CELL_N, MIN_IC_N)
    row.update(signal=batch['signal'], asof=batch['asof'], horizon=horizon,
               entry=batch['entry'], exit=date)
    g.rows.append(row)
    export_frame('periods', pd.DataFrame([row]))
    log.info('完成 %s→%s %d日 | A有效/冻结=%d/%d B=%d/%d | A−LOW=%s A−B=%s A−基准=%s'
             % (batch['entry'].date(), date.date(), horizon, row['A_valid_n'], row['A_n'],
                row['B_valid_n'], row['B_n'], format_number(row['A_minus_LOW_observed'], 100),
                format_number(row['A_minus_B_observed'], 100),
                format_number(row['A_observed_minus_benchmark'], 100)))
    if horizon == PRIMARY_HORIZON:
        display = {label: row[key] * 100 for label, key in (
            ('A减低PE_pp', 'A_minus_LOW_observed'), ('A减B_pp', 'A_minus_B_observed'),
            ('A减基准_pp', 'A_observed_minus_benchmark')) if np.isfinite(row[key])}
        if display:
            record(**display)


def on_strategy_end(context):
    log.info('[低预期＋盈利维持汇总] 报价收益，不含费用、滑点和成交限制；空账户曲线无研究意义。')
    if g.rows:
        summary = summarize_profit_periods(pd.DataFrame(g.rows), ROTATE_EVERY)
        export_frame('summary', summary)
        main = summary[(summary.horizon == PRIMARY_HORIZON) & (summary.scope == 'all')]
        for row in main.itertuples(index=False):
            log.info('60日 %s | 有效=%d/%d 均值=%s t_HAC=%s' % (
                row.metric, row.n, row.scheduled_n, format_number(row.mean, 100),
                format_number(row.t_hac)))
    unfinished = sum(len(HORIZONS) - len(batch['completed']) for batch in g.active)
    metadata = dict(version=STUDY_VERSION, first_day=str(g.first_day), last_day=str(g.last_day),
                    horizons=list(HORIZONS), primary_horizon=PRIMARY_HORIZON,
                    rotate_every=ROTATE_EVERY, benchmark=BENCHMARK, pe_limit=PE_LIMIT,
                    min_cell_n=MIN_CELL_N, min_ic_n=MIN_IC_N, query_batch=QUERY_BATCH,
                    min_listing_days=MIN_LISTING_DAYS, max_report_age=MAX_REPORT_AGE,
                    signal_count=g.signal_count, eligible_records=g.eligible_records,
                    mature_windows=len(g.rows), unfinished_horizons=unfinished,
                    pending_signal=g.pending is not None, no_orders=True,
                    no_external_input_files=True, historical_revisions_audited=False,
                    observed_means_are_not_portfolio_returns=True,
                    status=('no_eligible_eight_quarter_sample' if not g.eligible_records else
                            'no_mature_windows' if not g.rows else 'diagnostics_completed'),
                    signal='0<PE(TTM)<10; latest disclosed TTM parent profit>=prior TTM, both positive',
                    controls='continuous EP rank/industry/log cap/momentum120',
                    quote_policy='T+1 open to H trading days later open; dynamic pre-adjusted entry requery',
                    exported_csv=sorted(g.written))
    write_file(OUTPUT_PREFIX + 'metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
    log.info('[导出完成] 文件前缀%s；信号=%d 共同样本记录=%d 成熟窗口=%d 未完成窗口=%d。'
             % (OUTPUT_PREFIX, g.signal_count, g.eligible_records, len(g.rows), unfinished))
    log.info('A低PE且盈利维持，B低PE且下降，C其他正PE且维持，D其他正PE且下降；'
             'LOW=A+B。观察均值须结合覆盖率；本实验是原文特例的代理检验。')
