# -*- coding: utf-8 -*-
"""华夏新上市股票 ETF 入场实验 v1；完整复制到聚宽回测编辑器。
历史版本：现金名称过滤会误排除自由现金流股票ETF，新实验使用v1.1。
建议：2016-01-04~2026-09-01，100万元，日频即可；不需要其他文件。
问题：新上市产品的一次入场，小亏退出并保留赢家，能否覆盖全部损失与成本？
样本是华夏股票ETF（含宽基和跨境），不是全市场首次主题；不做主题合并。
官网2026-10-04名单快照+历史名称华夏补充，不保证历史退出产品完全覆盖。
财务数据库、指数映射、联网抓取均不在回测时使用。
上市完成5交易日后9:31尝试买入；失败最多重试5交易日，实际成交后永不重入。
每只预算当时净值10%，最多10只，不为新产品强行卖掉旧仓。
收盘亏损8%或从收盘高点回撤20%或持有378交易日，次交易日9:31退出。
阈值为预先固定的探索参数；378日只是约18个月持有上限，不验证十八个月理论。
止损按收盘触发、次日撮合，实际亏损可超过阈值；不是盘中止损。
历史入场日开盘在每个评价日重新前复权，统一成本参考和收盘高点的复权基准。
逐笔现金盈亏含实际买卖费用，不含持有期现金分红；组合净值含平台记账的分红/浮盈。
期末不强平，分开报告未完成交易；未成交事件不能悄悄删除。
"""
from jqdata import *
import builtins as _b
from bisect import bisect_left, bisect_right
from collections import Counter
import math
import pandas as pd

ENTRY_DELAY_DAYS = 5
ENTRY_RETRY_DAYS = 5
POSITION_FRACTION = 0.10
MAX_POSITIONS = 10
HARD_STOP_PCT = 0.08
TRAIL_STOP_PCT = 0.20
MAX_HOLD_DAYS = 378
MIN_AVG_DAILY_MONEY = 1000000.0  # 前5个交易日日均成交额，元
COMMISSION_RATE = 0.0002
MIN_COMMISSION = 5.0
SLIPPAGE_RATE = 0.002
PLOT_DIAGNOSTICS = True
EXPORT_CSV = False              # 无需下载，逐笔明细同时打印日志
SOURCE_DATE = '2026-10-04'
SOURCE_URL = 'https://www.chinaamc.com/front/front/out/etf/getEtfFundList'
CHINAAMC_NAMES = {
    "159966": "华夏创业板低波价值ETF",
    "515010": "华夏中证全指证券公司ETF",
    "515050": "华夏中证5G通信主题ETF",
    "159985": "华夏饲料豆粕期货ETF",
    "516500": "华夏中证生物科技主题ETF",
    "159711": "华夏中证港股通50ETF",
    "159601": "华夏MSCI中国A50互联互通ETF",
    "159758": "华夏中证红利质量ETF",
    "515760": "华夏中证浙江国资创新发展ETF",
    "516000": "华夏中证大数据产业ETF",
    "159326": "华夏中证电网设备主题ETF",
    "562530": "华夏中证智选1000价值稳健策略ETF",
    "159666": "华夏中证全指运输ETF",
    "512050": "华夏中证A500ETF",
    "159227": "华夏国证航天航空行业ETF",
    "158008": "华夏国证储能电池ETF",
    "510670": "华夏上证180ETF",
    "515640": "华夏中证全指家用电器ETF",
    "159030": "华夏国证粮食产业ETF",
    "510630": "华夏消费ETF",
    "512770": "华夏战略新兴成指ETF",
    "159967": "华夏创成长ETF",
    "159732": "华夏国证消费电子主题ETF",
    "159888": "华夏中证智能汽车主题ETF",
    "516320": "华夏中证装备产业ETF",
    "159783": "华夏中证科创创业50ETF",
    "589000": "华夏上证科创板综合ETF",
    "588170": "华夏上证科创板半导体材料设备主题ETF",
    "561730": "华夏中证A股ETF",
    "159230": "华夏国证通用航空产业ETF",
    "589550": "华夏上证智选科创板价值50策略ETF",
    "512990": "华夏MSCI中国A股国际通ETF",
    "159995": "华夏国证半导体芯片ETF",
    "515020": "华夏中证银行ETF",
    "159790": "华夏中证内地低碳经济主题ETF",
    "159726": "华夏恒生港股通中国内地企业红利ETF",
    "159731": "华夏中证石化产业ETF",
    "518850": "华夏黄金ETF",
    "513300": "华夏纳斯达克100ETF(QDII)",
    "513330": "华夏恒生互联网科技业ETF(QDII)",
    "511200": "华夏上证基准做市公司债ETF",
    "159655": "华夏标普500ETF(QDII)",
    "159663": "华夏中证机床ETF",
    "159547": "华夏中证红利低波动ETF",
    "562570": "华夏中证信息技术应用创新产业ETF",
    "159635": "华夏中证基建ETF",
    "562560": "华夏中证全指信息技术ETF",
    "562580": "华夏中证全指可选消费ETF",
    "562550": "华夏中证绿色电力ETF",
    "562660": "华夏中证2000ETF",
    "158041": "华夏创业板算力基础设施ETF",
    "159082": "华夏中证细分化工产业主题ETF",
    "560120": "华夏中证500自由现金流ETF",
    "510140": "华夏上证综合ETF",
    "515970": "华夏中证工程机械主题ETF",
    "513520": "华夏野村日经225ETF",
    "159892": "华夏恒生生物科技ETF(QDII)",
    "515170": "华夏中证细分食品饮料产业主题ETF",
    "516710": "华夏中证新材料主题ETF",
    "159201": "华夏国证自由现金流ETF",
    "562520": "华夏中证智选1000成长创新策略ETF",
    "588820": "华夏上证科创板200ETF",
    "159323": "华夏中证港股通汽车产业主题ETF",
    "159563": "华夏创业板综合ETF",
    "513810": "华夏中证香港内地国有企业ETF(QDII)",
    "159096": "华夏国证价值100ETF",
    "159118": "华夏标普港股通低波红利ETF",
    "159151": "华夏中证全指食品ETF",
    "589490": "华夏上证科创板成长ETF",
    "510650": "华夏金融ETF",
    "516190": "华夏中证文娱传媒ETF",
    "562510": "华夏中证旅游主题ETF",
    "515030": "华夏中证新能源汽车ETF",
    "516650": "华夏中证细分有色金属产业主题ETF",
    "516260": "华夏中证物联网主题ETF",
    "589010": "华夏上证科创板人工智能ETF",
    "159510": "华夏中证智选300价值稳健策略ETF",
    "513190": "华夏中证港股通内地金融ETF",
    "158036": "华夏中证卫星产业ETF",
    "588130": "华夏上证科创板生物医药ETF",
    "159189": "华夏国证石油天然气ETF",
    "159100": "华夏布拉德斯科巴西伊博维斯帕ETF(QDII)",
    "560830": "华夏中证全指电力公用事业ETF",
    "515040": "华夏中证工业有色金属主题ETF",
    "510050": "华夏上证50ETF",
    "510660": "华夏医药ETF",
    "513660": "华夏沪港通恒生ETF",
    "515060": "华夏中证全指房地产ETF",
    "515070": "华夏中证人工智能主题ETF",
    "159850": "华夏恒生中国企业ETF(QDII)",
    "516850": "华夏中证新能源ETF",
    "159845": "华夏中证1000ETF",
    "516630": "华夏中证云计算与大数据主题ETF",
    "562500": "华夏中证机器人ETF",
    "513230": "华夏中证港股通消费主题ETF",
    "588000": "华夏上证科创板50成份ETF",
    "159301": "华夏中证全指公用事业ETF",
    "159381": "华夏创业板人工智能ETF",
    "159620": "华夏中证智选500成长创新策略ETF",
    "159627": "华夏中证A100ETF",
    "159617": "华夏中证智选500价值稳健策略ETF",
    "159523": "华夏中证智选300成长创新策略ETF",
    "562590": "华夏中证半导体材料设备主题ETF",
    "562600": "华夏中证全指医疗器械ETF",
    "159075": "华夏中证工业互联网主题ETF",
    "159068": "华夏中证全指软件ETF",
    "512370": "华夏中证A500增强策略ETF",
    "515370": "华夏中证光伏产业ETF",
    "159256": "华夏创业板软件ETF",
    "512500": "华夏中证500ETF",
    "159920": "华夏恒生ETF",
    "159957": "华夏创业板ETF",
    "159869": "华夏中证动漫游戏ETF",
    "513180": "华夏恒生科技ETF(QDII)",
    "516100": "华夏中证金融科技主题ETF",
    "562700": "华夏中证汽车零部件主题ETF",
    "159791": "华夏沪深300ESG基准ETF",
    "159367": "华夏创业板50ETF",
    "159562": "华夏中证沪深港黄金产业股票ETF",
    "159573": "华夏创业板200ETF",
    "511100": "华夏上证基准做市国债ETF",
    "513910": "华夏中证港股通央企红利ETF",
    "516040": "华夏沪深300自由现金流ETF",
    "159101": "华夏国证港股通科技ETF",
    "159053": "华夏中证稀有金属主题ETF",
    "158026": "华夏国证大盘成长ETF",
    "510330": "华夏沪深300ETF",
    "159902": "华夏中小企业100ETF",
    "512950": "华夏中证央企ETF",
    "517170": "华夏中证沪港深500ETF",
    "516810": "华夏中证农业主题ETF",
    "159368": "华夏创业板新能源ETF",
    "588800": "华夏上证科创板100ETF",
    "159021": "华夏国证大盘价值ETF",
    "588510": "华夏中证科创创业人工智能ETF",
    "512460": "华夏中证电池主题ETF",
    "159962": "华夏中证四川国改ETF",
    "159983": "华夏粤港澳大湾区创新100ETF",
    "510610": "华夏能源ETF",
    "510620": "华夏材料ETF",
    "511280": "华夏3-5年中高级可质押信用债ETF"
}


def _day(value):
    return pd.Timestamp(value).date()


def _number(value, default=0.0):
    try:
        result = _b.float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _quantity(context, code):
    position = context.portfolio.positions.get(code)
    return _b.int(_number(getattr(position, 'total_amount', 0))) if position else 0


def _equity_name(name):
    # 名称过滤只区分明显非股票产品，不按历史成绩挑主题。
    return not (_b.any(word in name for word in ('债', '货币', '现金', '期货'))
                or name.endswith('黄金ETF'))


def _issuer_name(code, historical_name):
    official = CHINAAMC_NAMES.get(code.split('.')[0])
    if official:
        return official, '官网快照'
    if '华夏' in historical_name:
        return historical_name, '历史名称补充'
    return None, None


def initialize(context):
    if (ENTRY_DELAY_DAYS < 1 or ENTRY_RETRY_DAYS < 1 or MAX_HOLD_DAYS < 1
            or MAX_POSITIONS < 1 or not 0 < POSITION_FRACTION <= 1
            or not 0 < HARD_STOP_PCT < 1 or not 0 < TRAIL_STOP_PCT < 1
            or MIN_AVG_DAILY_MONEY < 0):
        raise ValueError('Invalid experiment parameters')
    set_option('avoid_future_data', True)
    set_option('use_real_price', True)
    set_option('order_volume_ratio', 0.10)
    set_benchmark('000300.XSHG')
    set_order_cost(OrderCost(open_tax=0, close_tax=0,
                   open_commission=COMMISSION_RATE, close_commission=COMMISSION_RATE,
                   close_today_commission=COMMISSION_RATE,
                   min_commission=MIN_COMMISSION), type='fund')
    set_slippage(PriceRelatedSlippage(SLIPPAGE_RATE))
    log.set_level('order', 'error')
    g.start = _day(context.current_dt)
    g.initial_cash = _number(context.portfolio.total_value)
    g.calendar = [_day(d) for d in get_trade_days(start_date='2000-01-01', end_date=g.start)
                  if _day(d) <= g.start]
    g.registered = _b.set()
    g.events = {}
    g.active = {}
    g.completed = []
    g.old_count = 0
    g.non_equity_count = 0
    g.blocked = Counter()
    g.data_issues = Counter()
    g.nav = []
    run_daily(market_open, time='09:31')
    run_daily(market_close, time='15:10')
    log.info('华夏新ETF v1 | 上市完成%d日后尝试%d日 | 每只%.0f%%/最多%d只 | 止损%.0f%%/回撤%.0f%%/持有上限%d交易日'
             % (ENTRY_DELAY_DAYS, ENTRY_RETRY_DAYS, POSITION_FRACTION * 100,
                MAX_POSITIONS, HARD_STOP_PCT * 100, TRAIL_STOP_PCT * 100, MAX_HOLD_DAYS))
    log.info('名单来源=%s，快照=%s；历史退出覆盖未保证；按历史在市日期加入，不筛选未来收益。'
             % (SOURCE_URL, SOURCE_DATE))


def _register(context, previous):
    frame = get_all_securities(['etf'], date=previous)
    required = ('display_name', 'start_date')
    if not _b.all(col in frame.columns for col in required):
        raise ValueError('ETF metadata missing display_name/start_date')
    for code, row in frame.iterrows():
        if code in g.registered:
            continue
        listing = _day(row['start_date'])
        if listing > previous:
            continue
        g.registered.add(code)
        name, source = _issuer_name(code, _b.str(row['display_name']))
        if name is None:
            continue
        if not _equity_name(name):
            g.non_equity_count += 1
            continue
        if listing < g.start:
            g.old_count += 1
            continue
        event = {'code': code, 'name': name, 'source': source,
                 'listing_date': listing, 'status': 'waiting', 'attempts': 0,
                 'last_block': '', 'entry_date': None, 'exit_date': None,
                 'entry_price': None, 'entry_open': None, 'entry_outlay': 0.0,
                 'buy_quantity': 0, 'sale_proceeds': 0.0, 'sold_quantity': 0,
                 'peak': None, 'entry_ref': None, 'exit_reason': '', 'cash_pnl': None}
        g.events[code] = event
        log.info('新事件 %s %s | 上市=%s | 识别=%s' % (code, name, listing, source))


def _block(event, reason):
    event['last_block'] = reason
    g.blocked[reason] += 1


def _filled(order, before, after, side):
    difference = _b.max(after - before, 0) if side == 'buy' else _b.max(before - after, 0)
    if order is None:
        return difference
    # 只归因已成交部分，不能用委托量。
    return _b.max(difference, _b.int(_number(getattr(order, 'filled', 0))))


def _execute_exit(context, code, event, current):
    quote = current[code]
    if quote.paused:
        g.blocked['卖出停牌'] += 1
        return
    before = _quantity(context, code)
    if before <= 0:
        raise ValueError('Active event lost its position: %s' % code)
    position = context.portfolio.positions[code]
    if _number(getattr(position, 'closeable_amount', before)) <= 0:
        g.blocked['卖出不可用持仓'] += 1
        return
    px = _number(getattr(quote, 'last_price', 0))
    lower = _number(getattr(quote, 'low_limit', 0))
    if px <= 0 or (lower > 0 and px <= lower + 0.000001):
        g.blocked['卖出无报价或跌停'] += 1
        return
    before_cash = _number(context.portfolio.cash)
    order = order_target(code, 0)
    after = _quantity(context, code)
    filled = _filled(order, before, after, 'sell')
    if filled <= 0:
        g.blocked['卖出未成交'] += 1
        return
    event['sold_quantity'] += filled
    event['sale_proceeds'] += _number(context.portfolio.cash) - before_cash
    log.info('卖出成交 %s 数量=%d | 原因=%s | 剩余=%d'
             % (code, filled, event['exit_reason'], after))
    if after > 0:
        return
    event['status'] = 'closed'
    event['exit_date'] = _day(context.current_dt)
    event['cash_pnl'] = event['sale_proceeds'] - event['entry_outlay']
    g.completed.append(event)
    del g.active[code]
    log.info('完成 %s %s | %s→%s | 现金净盈亏=%+.2f | 现金收益=%+.2f%% | %s'
             % (code, event['name'], event['entry_date'], event['exit_date'], event['cash_pnl'],
                100 * event['cash_pnl'] / event['entry_outlay'], event['exit_reason']))


def market_open(context):
    today = _day(context.current_dt)
    if not g.calendar or g.calendar[-1] < today:
        g.calendar.append(today)
    index = bisect_left(g.calendar, today)
    if index <= 0:
        raise ValueError('No previous trading day available')
    previous = g.calendar[index - 1]
    current = get_current_data()
    for code in _b.sorted(_b.list(g.active)):
        event = g.active[code]
        if event['exit_reason']:
            _execute_exit(context, code, event, current)
    _register(context, previous)
    entry_nav = _number(context.portfolio.total_value)
    waiting = _b.sorted((e for e in g.events.values() if e['status'] == 'waiting'),
                       key=lambda e: (e['listing_date'], e['code']))
    for event in waiting:
        completed_days = bisect_right(g.calendar, previous) - bisect_left(g.calendar, event['listing_date'])
        if completed_days < ENTRY_DELAY_DAYS:
            continue
        if completed_days >= ENTRY_DELAY_DAYS + ENTRY_RETRY_DAYS:
            event['status'] = 'missed'
            event['last_block'] = event['last_block'] or '超过入场窗口'
            log.info('未入场 %s %s | 原因=%s' % (event['code'], event['name'], event['last_block']))
            continue
        event['attempts'] += 1
        code = event['code']
        if _b.len(g.active) >= MAX_POSITIONS:
            _block(event, '仓位名额不足')
            continue
        quote = current[code]
        if quote.paused:
            _block(event, '买入停牌')
            continue
        px = _number(getattr(quote, 'last_price', 0))
        opening = _number(getattr(quote, 'day_open', 0))
        upper = _number(getattr(quote, 'high_limit', 0))
        if px <= 0 or opening <= 0 or (upper > 0 and px >= upper - 0.000001):
            _block(event, '买入无报价或涨停')
            continue
        liquidity = get_price(code, end_date=previous, count=ENTRY_DELAY_DAYS,
                     frequency='daily', fields=['money'], skip_paused=False, fq='pre', panel=False)
        if liquidity.empty or 'money' not in liquidity or _b.len(liquidity) < ENTRY_DELAY_DAYS:
            _block(event, '历史行情不足')
            continue
        values = pd.to_numeric(liquidity['money'], errors='coerce')
        if values.isna().any() or _number(values.mean()) < MIN_AVG_DAILY_MONEY:
            _block(event, '成交额不足')
            continue
        budget = _b.min(entry_nav * POSITION_FRACTION,
                       _number(context.portfolio.cash) / (1 + COMMISSION_RATE) - MIN_COMMISSION)
        if budget < 100 * px:
            _block(event, '现金或整手不足')
            continue
        before = _quantity(context, code)
        before_cash = _number(context.portfolio.cash)
        order = order_target_value(code, budget)
        after = _quantity(context, code)
        filled = _filled(order, before, after, 'buy')
        if filled <= 0:
            _block(event, '买入未成交')
            continue
        outlay = before_cash - _number(context.portfolio.cash)
        if after <= 0 or outlay <= 0:
            raise ValueError('Filled buy without settled position/cash: %s' % code)
        executed_price = _number(getattr(order, 'price', 0))
        if executed_price <= 0:
            executed_price = _number(getattr(context.portfolio.positions[code], 'avg_cost', px), px)
        event.update(status='holding', entry_date=today, entry_price=executed_price,
                     entry_open=opening, entry_outlay=outlay, buy_quantity=filled,
                     peak=executed_price, entry_ref=opening)
        g.active[code] = event
        log.info('买入成交 %s %s | 数量=%d | 价格=%.4f | 含费支出=%.2f'
                 % (code, event['name'], filled, executed_price, outlay))


def _daily_bars(codes, day, fields):
    if not codes:
        return {}
    frame = get_price(codes, start_date=day, end_date=day, frequency='daily',
                      fields=fields, skip_paused=False, fq='pre', panel=False)
    if frame.empty:
        return {}
    if 'code' not in frame:
        if _b.len(codes) != 1:
            raise ValueError('Batch price response missing code')
        frame = frame.copy()
        frame['code'] = codes[0]
    if 'time' in frame:
        dates = pd.to_datetime(frame['time'])
    else:
        dates = pd.to_datetime(frame.index)
    if _b.any(_day(value) != day for value in dates):
        raise ValueError('Price endpoint returned a different date')
    return {row['code']: row for _, row in frame.iterrows()}


def market_close(context):
    today = _day(context.current_dt)
    codes = _b.sorted(g.active)
    quotes = _daily_bars(codes, today, ['close'])
    groups = {}
    for code in codes:
        groups.setdefault(g.active[code]['entry_date'], []).append(code)
    entries = {}
    # 同日入场的产品合并查询；重新前复权，不能跨评价日缓存旧价格。
    for date, members in groups.items():
        entries.update(_daily_bars(members, date, ['open']))
    for code in codes:
        event = g.active[code]
        close = _number(quotes.get(code, {}).get('close', 0))
        entry_ref = _number(entries.get(code, {}).get('open', 0))
        if close <= 0 or entry_ref <= 0:
            g.data_issues['持仓收盘或复权参考缺失'] += 1
            if bisect_left(g.calendar, today) - bisect_left(g.calendar, event['entry_date']) >= MAX_HOLD_DAYS - 1:
                event['exit_reason'] = event['exit_reason'] or '持有到期'
            continue
        event['peak'] *= entry_ref / event['entry_ref']
        event['entry_ref'] = entry_ref
        event['peak'] = _b.max(event['peak'], close)
        basis = event['entry_price'] * entry_ref / event['entry_open']
        held_days = bisect_left(g.calendar, today) - bisect_left(g.calendar, event['entry_date'])
        reason = ''
        if close <= basis * (1 - HARD_STOP_PCT):
            reason = '固定止损'
        elif close <= event['peak'] * (1 - TRAIL_STOP_PCT):
            reason = '高点回撤'
        elif held_days >= MAX_HOLD_DAYS - 1:
            reason = '持有到期'
        if reason and not event['exit_reason']:
            event['exit_reason'] = reason
            log.info('退出信号 %s | %s | 成本收益=%+.2f%% | 高点回撤=%+.2f%%；下一交易日执行'
                     % (code, reason, 100 * (close / basis - 1), 100 * (close / event['peak'] - 1)))
    nav = _number(context.portfolio.total_value)
    g.nav.append((today, nav))
    if PLOT_DIAGNOSTICS:
        record(持仓数=_b.len(g.active), 已买入数=_b.len(g.active) + _b.len(g.completed),
               已平仓数=_b.len(g.completed),
               仓位_pct=100 * (nav - _number(context.portfolio.cash)) / nav if nav > 0 else 0,
               未入场数=_b.sum(e['status'] == 'missed' for e in g.events.values()))


def on_strategy_end(context):
    statuses = Counter(e['status'] for e in g.events.values())
    pnl = [e['cash_pnl'] for e in g.completed]
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x < 0]
    payoff = (_b.sum(wins) / _b.len(wins)) / (-_b.sum(losses) / _b.len(losses)) if wins and losses else None
    peak, drawdown = g.initial_cash, 0.0
    for _, value in g.nav:
        peak = _b.max(peak, value)
        drawdown = _b.min(drawdown, value / peak - 1) if peak > 0 else drawdown
    ending = _number(context.portfolio.total_value)
    run, longest = 0, 0
    for value in pnl:
        run = run + 1 if value < 0 else 0
        longest = _b.max(longest, run)
    best = _b.sorted(g.completed, key=lambda e: e['cash_pnl'], reverse=True)[:3]
    top_pnl = _b.sum(e['cash_pnl'] for e in best if e['cash_pnl'] > 0)
    positive_total = _b.sum(wins)
    log.info('================ 华夏新上市ETF v1 汇总 ================')
    log.info('窗口=%s~%s | 参数：延迟%d/尝试%d日、每只%.0f%%、最多%d只、止损%.0f%%、回撤%.0f%%、上限%d日'
             % (g.start, _day(context.current_dt), ENTRY_DELAY_DAYS, ENTRY_RETRY_DAYS,
                POSITION_FRACTION * 100, MAX_POSITIONS, HARD_STOP_PCT * 100,
                TRAIL_STOP_PCT * 100, MAX_HOLD_DAYS))
    log.info('事件=%d | 状态=%s | 起点前旧产品=%d | 非股票产品=%d'
             % (_b.len(g.events), _b.dict(statuses), g.old_count, g.non_equity_count))
    log.info('组合收益=%+.2f%% | 收盘最大回撤=%.2f%%；含期末浮盈，不等于逐笔平仓收益'
             % (100 * (ending / g.initial_cash - 1), -100 * drawdown))
    log.info('平仓=%d | 胜率=%.1f%% | 平均盈利/平均亏损=%s | 最长连续亏损=%d'
             % (_b.len(pnl), 100 * _b.len(wins) / _b.len(pnl) if pnl else 0,
                '%.2f' % payoff if payoff is not None else 'NA', longest))
    log.info('平仓成交现金盈亏=%+.2f | 最大3笔盈利占全部正盈亏=%.1f%%；不含持有期现金分红'
             % (_b.sum(pnl), 100 * top_pnl / positive_total if positive_total else 0))
    log.info('阻碍次数=%s | 数据异常次数=%s' % (_b.dict(g.blocked), _b.dict(g.data_issues)))
    log.info('以下逐笔现金收益含买卖费，不含现金分红；期末未平仓单独列示。')
    for event in _b.sorted(g.events.values(), key=lambda e: (e['listing_date'], e['code'])):
        cash_return = (100 * event['cash_pnl'] / event['entry_outlay']) if event['cash_pnl'] is not None else None
        log.info('%s %s | 上市=%s | %s | 入场=%s | 退出=%s | 盈亏=%s | 收益=%s | 原因=%s'
                 % (event['code'], event['name'], event['listing_date'], event['status'],
                    event['entry_date'], event['exit_date'],
                    '%+.2f' % event['cash_pnl'] if event['cash_pnl'] is not None else 'NA',
                    '%+.2f%%' % cash_return if cash_return is not None else 'NA',
                    event['exit_reason'] or event['last_block'] or '尚未完成窗口'))
    log.info('探索样本来自官网快照+历史名称，历史退出覆盖未保证；未识别不等于没有该产品。')
    log.info('=======================================================')
    if EXPORT_CSV:
        columns = ['code', 'name', 'source', 'listing_date', 'status', 'attempts',
                   'entry_date', 'exit_date', 'entry_price', 'entry_outlay',
                   'buy_quantity', 'sale_proceeds', 'sold_quantity', 'cash_pnl',
                   'exit_reason', 'last_block']
        frame = pd.DataFrame(_b.list(g.events.values()), columns=columns)
        write_file('chinaamc_new_etf_v1_trades.csv', frame.to_csv(index=False), append=False)
