# -*- coding: utf-8 -*-
"""ETF 二日超跌回收研究——天级回测版。

昨日收盘判信号，今日开盘市价撮合；回测频率选"每天"。
整个文件可复制至聚宽策略编辑器。信号与退出内核沿用分钟版；
执行改为每日开盘一次撮合，建池与挂单管理按天级简化，口径见 reversal_v1.md。
"""
from jqdata import *
import builtins as _b
import re
import numpy as np
import pandas as pd

MA_DAYS = 20
ATR_DAYS = 20
ENTRY_D = -2.0
MAX_HOLD_DAYS = 10
WEIGHT = 0.10
MAX_NAMES = 3
MAX_EXPOSURE = 0.30
LIQ_MIN = 30_000_000.0
POOL_REFRESH_DAYS = 5  # 标的池每周重建一次，避免每天全表查询拖慢回测
COMMISSION = 0.0001
# 照 etfs.py：货币基金不参与，未映射指数的黄金产品单列一组。
CASH_FUND_PATTERN = '货币|快线|快钱'
GOLD_FUND_PATTERN = '黄金|金ETF|上海金'


def entry_signal(frame):
    """ATR 为20日简单平均真实波幅，非 Wilder 平滑；最简二日回收。"""
    if not valid_bars(frame, 22):
        return None
    close = frame['close'].astype(float)
    high = frame['high'].astype(float)
    low = frame['low'].astype(float)
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    ma = close.rolling(MA_DAYS).mean()
    atr = tr.rolling(ATR_DAYS).mean()
    d = (close - ma) / atr
    if not (d.iloc[-2] < ENTRY_D <= d.iloc[-1]):
        return None
    return {'signal_date': str(frame.index[-1].date()),
            'previous_d': float(d.iloc[-2]), 'd': float(d.iloc[-1]),
            'ma': float(ma.iloc[-1]), 'signal_close': float(close.iloc[-1]),
            'low_ratio': float(low.iloc[-2:].min() / close.iloc[-1]),
            'ma_ratio': float(ma.iloc[-1] / close.iloc[-1])}


def valid_bars(frame, minimum):
    if (frame is None or len(frame) < minimum or not frame.index.is_unique
            or not frame.index.is_monotonic_increasing):
        return False
    required = ['open', 'high', 'low', 'close', 'money', 'paused']
    if not set(required).issubset(frame.columns):
        return False
    values = np.asarray(frame[required], dtype=float)
    if not np.isfinite(values).all() or (values[:, :4] <= 0).any():
        return False
    return _b.bool((frame['money'] > 0).all() and (frame['paused'] == 0).all()
                and (frame['high'] >= frame[['open', 'low', 'close']].max(axis=1)).all()
                and (frame['low'] <= frame[['open', 'close']].min(axis=1)).all())


def entry_allowed(signal, price):
    # 锚点全部以信号收盘的比率表示，与退出侧口径一致，对复权重基准不变。
    if signal is None or not np.isfinite(price) or price <= 0:
        return False
    base = signal['signal_close']
    if not np.isfinite(base) or base <= 0:
        return False
    rel = price / base
    return _b.bool(signal['low_ratio'] < rel < signal['ma_ratio'])


def exit_window_ok(frame, stamp):
    """退出只需末20根收盘与信号日锚点行；不做全窗口停牌校验。"""
    if len(frame) < 20 or pd.Timestamp(stamp) not in frame.index:
        return False
    closes = frame['close'].iloc[-20:].astype(float)
    if not np.isfinite(np.asarray(closes, dtype=float)).all() or (closes <= 0).any():
        return False
    if not _b.bool((np.asarray(frame['paused'].iloc[-20:], dtype=float) == 0).all()):
        return False
    anchor = float(frame.loc[stamp, 'close'])
    return _b.bool(np.isfinite(anchor) and anchor > 0)


def exit_reason(frame, meta):
    stamp = pd.Timestamp(meta['signal_date'])
    if not exit_window_ok(frame, stamp):
        return None
    # 不保存跨日期绝对复权价格：每天同一 get_price 返回中重建固定低点。
    fixed_low = meta['low_ratio'] * float(frame.loc[stamp, 'close'])
    close = float(frame['close'].iloc[-1])
    if close < fixed_low:
        return 'structure'
    if close >= float(frame['close'].iloc[-20:].mean()):
        return 'mean'
    return None


def index_identity(row):
    raw = row.get('traced_index_code')
    if pd.notna(raw) and str(raw).strip():
        token = str(raw).strip().upper()
        match = re.fullmatch(r'(\d{6})(?:\.(?:XSHG|XSHE|SH|SZ|CSI))?', token)
        if match:
            return match.group(1), 'official_code'
        return token, 'unmapped_code'
    name = row.get('traced_index_name')
    if not (pd.notna(name) and str(name).strip()):
        return None, 'unmapped'
    tokens = {t for t in re.findall(r'[（(](\d{6}(?:\.(?:XSHG|XSHE|SH|SZ|CSI))?)[）)]', str(name).upper())}
    keys = {t.split('.')[0] for t in tokens}
    display = 'NAME:' + re.sub(r'\s+', '', str(name)).upper()
    if len(keys) == 1:
        if len(tokens) > 1:
            return display, 'name_ambiguous'
        return keys.pop(), 'parsed_code'
    return display, 'name_only'


def build_pool(asof):
    """点时快照建池，流程照 etfs.py：
    基金名单→ETF→公告生效的跟踪映射→取最新公告→可归类（剔除货币基金、
    黄金单列）→上市满365日→20日均成交额→每只指数保留成交额最大的一只。
    """
    date = pd.Timestamp(asof).normalize()
    date_str = str(date.date())
    securities = get_all_securities(['fund'], date=date_str)
    if securities is None or securities.empty:
        raise ValueError('历史基金名单为空')
    etfs = securities[securities['type'].astype(str).str.lower() == 'etf'].copy()
    codes = sorted(etfs.index)
    if not codes:
        raise ValueError('历史ETF名单为空')

    mappings = []
    for offset in range(0, len(codes), 50):
        batch = codes[offset:offset + 50]
        result = finance.run_query(query(finance.FUND_INVEST_TARGET).filter(
            finance.FUND_INVEST_TARGET.code.in_(batch),
            finance.FUND_INVEST_TARGET.pub_date <= date_str))
        if result is not None and not result.empty:
            mappings.append(result)
    if mappings:
        mapping = pd.concat(mappings, ignore_index=True)
        for field in ['pub_date', 'start_date', 'end_date']:
            mapping[field] = pd.to_datetime(mapping[field])
        # 只保留当天已生效的映射，再按公告日取每只ETF的最新一条。
        mapping = mapping[(mapping.start_date <= date)
                          & (mapping.end_date.isna() | (mapping.end_date > date))]
        mapping = mapping.sort_values('pub_date').drop_duplicates('code', keep='last')
        etfs = etfs.join(mapping.set_index('code')[
            ['traced_index_name', 'traced_index_code']])
    else:
        etfs['traced_index_name'] = None
        etfs['traced_index_code'] = None

    identities = [index_identity(row) for _, row in etfs.iterrows()]
    etfs['index_key'] = [key for key, _ in identities]
    etfs['mapping_source'] = [source for _, source in identities]
    etfs['pool_key'] = etfs['index_key']
    names = etfs['display_name'].astype(str)
    gold = etfs['pool_key'].isna() & names.str.contains(GOLD_FUND_PATTERN, na=False)
    etfs.loc[gold, 'index_key'] = 'MANUAL:GOLD'
    etfs.loc[gold, 'pool_key'] = 'MANUAL:GOLD'
    cash = etfs['pool_key'].isna() & names.str.contains(CASH_FUND_PATTERN, na=False)
    etfs = etfs[etfs['pool_key'].notna() & ~cash].copy()
    mapped = len(etfs)
    start = pd.to_datetime(etfs['start_date'], errors='coerce')
    etfs = etfs[start.notna() & (start <= date - pd.Timedelta(days=365))]
    listed = len(etfs)
    if etfs.empty:
        log.info('建池 %s：ETF=%d 可归类且上市满365日=0' % (date_str, len(codes)))
        return []

    amounts = []
    pool_codes = sorted(etfs.index)
    for offset in range(0, len(pool_codes), 100):
        data = get_price(pool_codes[offset:offset + 100], end_date=date_str, count=20,
                         frequency='daily', fields=['money'], skip_paused=True,
                         panel=False)
        if data is not None and not data.empty:
            amounts.append(data)
    if amounts:
        flat = pd.concat(amounts, ignore_index=True)
        etfs['avg_money'] = etfs.index.map(flat.groupby('code')['money'].mean())
    else:
        etfs['avg_money'] = np.nan
    etfs['avg_money'] = etfs['avg_money'].fillna(0.0)
    etfs = etfs[etfs['avg_money'] >= LIQ_MIN].sort_values('avg_money', ascending=False)
    selected = etfs.drop_duplicates(subset=['pool_key'], keep='first')
    rows = [dict(code=str(code), index_key=str(row['index_key']),
                 pool_key=str(row['pool_key']),
                 mapping_source=str(row['mapping_source']),
                 avg_money=float(row['avg_money']))
            for code, row in selected.iterrows()]
    log.info('建池 %s：ETF=%d → 可归类 %d → 上市满365日 %d → 流动性 %d → 代表 %d'
             % (date_str, len(codes), mapped, listed, len(etfs), len(rows)))
    return rows


def read_daily(code, asof):
    kwargs = dict(end_date=asof, frequency='daily',
                  fields=['open', 'high', 'low', 'close', 'money', 'paused'],
                  skip_paused=False, fq='pre', panel=False)
    kwargs['count'] = 22
    sessions = pd.DatetimeIndex(get_trade_days(end_date=asof, count=22)).normalize()
    frame = get_price(code, **kwargs)
    if frame is None:
        raise ValueError('行情缺失 %s' % code)
    frame = frame.copy()
    frame.index = pd.DatetimeIndex(frame.index).normalize()
    if not frame.index.equals(sessions) or not len(frame) or frame.index[-1] != pd.Timestamp(asof):
        raise ValueError('日线日期缺失/过期/未来 %s' % code)
    return frame


def load_frames(codes, asof, count=22):
    """一次批量取全池日线，避免逐只查询把回测拖慢。"""
    if not codes:
        return {}
    data = get_price(list(codes), end_date=str(pd.Timestamp(asof).date()), count=count,
                     frequency='daily',
                     fields=['open', 'high', 'low', 'close', 'money', 'paused'],
                     skip_paused=False, fq='pre', panel=False)
    if data is None or data.empty or not {'time', 'code'}.issubset(data.columns):
        return {}
    data = data.copy()
    data['time'] = pd.DatetimeIndex(data['time']).normalize()
    return {code: grp.sort_values('time').set_index('time').drop(columns='code')
            for code, grp in data.groupby('code')}


def initialize(context):
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_benchmark('000300.XSHG')
    set_slippage(FixedSlippage(0.002))  # 基金价格绝对滑点，不是百分比
    set_order_cost(OrderCost(open_tax=0, close_tax=0, open_commission=COMMISSION,
                             close_commission=COMMISSION, min_commission=5), type='fund')
    g.book = {}
    g.day = None
    g.bought_today = set()
    g.sold_day = None
    g.pool_date = None
    g.pool_rows = []
    # 天级回测下 every_bar 每天开盘(9:30)运行一次；市价单按当日开盘价撮合。
    run_daily(run_reversal, time='every_bar')
    log.info('reversal_v1_daily 初始化完成，回测频率须选"每天"')


def held_positions(context):
    return {c: p for c, p in context.portfolio.positions.items() if p.total_amount > 0}


def sync_book(context):
    today = str(context.current_dt.date())
    held = held_positions(context)
    live = get_open_orders()
    pending_buys = {o.security for o in live.values() if o.is_buy}
    for code, meta in list(g.book.items()):
        if code in held and meta['entry_date'] is None:
            meta['entry_date'] = today  # 首次观察真实持仓，不以接受委托冒充成交
        if code not in held and code not in pending_buys:
            if meta.get('exit'):
                g.sold_day = today
            del g.book[code]


def manage_exits(context):
    today = str(context.current_dt.date())
    for code, position in held_positions(context).items():
        meta = g.book.get(code)
        if meta is None:
            log.info('未知持仓元数据，禁止新买并人工核查: %s' % code)
            continue
        try:
            if meta['entry_date'] is None or meta['entry_date'] >= today:
                continue
            if not meta.get('exit'):
                days = get_trade_days(start_date=meta['entry_date'], end_date=context.previous_date)
                age = len(days)  # 入场交易日算第1天；第10天收盘确认，次日开盘执行
                if age >= MAX_HOLD_DAYS:
                    meta['exit'] = 'time'
                else:
                    frame = read_daily(code, context.previous_date)
                    meta['exit'] = exit_reason(frame, meta)
            if not meta.get('exit'):
                continue
            g.sold_day = today  # 当日任意退出，禁止包括其他组在内的新开仓
            if position.closeable_amount <= 0:
                continue
            result = order_target(code, 0)
            log.info('退出 %s %s 委托=%s' % (code, meta['exit'], result is not None))
        except Exception as exc:
            log.info('持仓处理失败，保留状态 %s: %s' % (code, exc))
    sync_book(context)


def run_reversal(context):
    today = str(context.current_dt.date())
    if g.day != today:
        g.day = today
        g.bought_today = set()
    sync_book(context)
    manage_exits(context)
    if g.sold_day == today:
        return
    held = held_positions(context)
    unknown = [c for c in held if c not in g.book]
    if unknown:
        log.info('未知持仓暂停新仓: %s' % unknown)
        return
    if len(held) >= MAX_NAMES:
        return
    try:
        date_key = str(pd.Timestamp(context.previous_date).date())
        if g.pool_date is None or pool_stale(date_key):
            g.pool_rows = build_pool(context.previous_date)
            g.pool_date = date_key
    except Exception as exc:
        log.info('建池失败，只管理持仓: %s' % exc)
        return
    execute_entries(context, g.pool_rows)


def pool_stale(date_key):
    try:
        days = get_trade_days(start_date=g.pool_date, end_date=date_key)
        return len(days) > POOL_REFRESH_DAYS
    except Exception:
        return True


def after_trading_end(context):
    sync_book(context)


def execute_entries(context, rows):
    cash_left = float(context.portfolio.available_cash)
    if not np.isfinite(cash_left) or cash_left < 0:
        log.info('现金未知，不买')
        return
    today = str(context.current_dt.date())
    rows = sorted(rows, key=lambda r: (-r['avg_money'], r['code']))
    frames = load_frames([row['code'] for row in rows], context.previous_date)
    opened = 0
    signals = 0
    for row in rows:
        held = held_positions(context)
        reserved = set(held)
        if len(reserved) >= MAX_NAMES:
            break
        unknown = [c for c in reserved if c not in g.book]
        if unknown:
            break
        indices = {g.book[c]['index_key'] for c in reserved}
        code = row['code']
        if code in reserved or code in g.bought_today or row['index_key'] in indices:
            continue
        try:
            frame = frames.get(code)
            signal = entry_signal(frame)
            # 天级闸门：以昨收代入比率锚，等价于"收盘仍在MA20下方且高于二日低点"。
            if signal is None or not entry_allowed(signal, signal['signal_close']):
                continue
            signals += 1
            equity = float(context.portfolio.total_value)
            if not np.isfinite(equity) or equity <= 0:
                raise ValueError('账户权益无效')
            exposure = float(_b.sum(float(p.value) for p in held.values()))
            budget = equity * WEIGHT
            if (not np.isfinite(exposure) or exposure + budget > equity * MAX_EXPOSURE + 1e-8
                    or _b.min(cash_left, float(context.portfolio.available_cash)) < budget):
                continue  # 不把10%缩成碎仓，也不补旧仓
            # 昨收估算份数；实际按今开撮合，可用资金由平台兜底，不会超买。
            price = float(frame['close'].iloc[-1])
            amount = int((budget - 5.) / ((price + .002) * (1 + COMMISSION)) // 100) * 100
            if amount < 100:
                continue
            meta = dict(signal, pool_key=row['pool_key'], index_key=row['index_key'],
                        entry_date=None, order_date=today, exit=None)
            g.book[code] = meta
            result = order(code, amount)
            pending = _b.any(o.security == code and o.is_buy
                             for o in get_open_orders().values())
            if result is not None and (int(result.filled) > 0 or pending):
                g.bought_today.add(code)
                cash_left -= budget
                if int(result.filled) > 0:
                    meta['entry_date'] = today
                opened += 1
                log.info('接受新仓 %s %d份 group=%s signal=%s' %
                         (code, amount, row['pool_key'], signal['signal_date']))
            elif code not in held_positions(context):
                g.book.pop(code, None)
                log.info('买入拒单/零成交 %s' % code)
        except Exception as exc:
            log.info('新仓处理失败 %s: %s' % (code, exc))
    log.info('今日扫描 %d 只，信号 %d 只，新开 %d 只，持仓 %d 只'
             % (len(rows), signals, opened, len(held_positions(context))))
    sync_book(context)