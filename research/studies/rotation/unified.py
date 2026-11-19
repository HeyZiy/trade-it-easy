"""ETF/LOF 跨资产趋势轮动，聚宽分钟频率单文件策略。"""

from jqdata import *
import math
import numpy as np

MOMENTUM_WINDOWS = (15, 20, 25, 30)
LOOKBACK_DAYS = max(MOMENTUM_WINDOWS)
TARGET_COUNT = 2
COMMISSION = 0.0002
MIN_COMMISSION = 5.0
ROUND_LOT = 100


def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_order_cost(OrderCost(open_tax=0, close_tax=0,
                             open_commission=COMMISSION, close_commission=COMMISSION,
                             close_today_commission=COMMISSION,
                             min_commission=MIN_COMMISSION), type='fund')
    set_slippage(FixedSlippage(0))
    g.strategy = EtfRotationStrategy()
    reset_statistics(context.portfolio.starting_cash)
    schedule_tasks()


def after_code_changed(context):
    unschedule_all()
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    log.set_level('strategy', 'info')
    g.strategy = EtfRotationStrategy()
    reset_statistics(context.portfolio.total_value)
    schedule_tasks()


def schedule_tasks():
    run_daily(trade_handler, time='14:59', reference_security='000300.XSHG')
    run_daily(after_market_close, time='after_close')


def reset_statistics(total_value):
    g.holding_stats = {}
    g.holding_days = {}
    g.yearly_performance = {}
    g.stats_last_total_value = float(total_value)


def weighted_trend_score(prices):
    log_prices = np.log(prices)
    days = np.arange(len(prices))
    weights = np.linspace(1, 2, len(prices))
    slope, intercept = np.polyfit(days, log_prices, 1, w=weights)
    effective_weights = weights ** 2
    mean_log_price = np.average(log_prices, weights=effective_weights)
    ss_total = np.sum(effective_weights * (log_prices - mean_log_price) ** 2)
    if ss_total <= 0:
        return 0.0
    residuals = log_prices - (slope * days + intercept)
    ss_residual = np.sum(effective_weights * residuals ** 2)
    r_squared = np.clip(1 - ss_residual / ss_total, 0.0, 1.0)
    return math.expm1(slope * 250) * r_squared


def affordable_shares(cash, price):
    if cash <= MIN_COMMISSION:
        return 0
    amount = min(cash - MIN_COMMISSION, cash / (1 + COMMISSION))
    return int(amount / price / ROUND_LOT) * ROUND_LOT


class EtfRotationStrategy:
    def __init__(self):
        self.etf_pool = [
            # 国内权益
            '510300.XSHG',  # 沪深300（中国宽基）
            '159915.XSHE',  # 创业板（国内成长）
            '515180.XSHG',  # 中证红利（国内价值/防御）
            '512100.XSHG',  # 中证1000（国内小盘规模维度）
            '588000.XSHG',  # 科创50（科创板代表）

            # 海外权益
            '513500.XSHG',  # 标普500（海外宽基）
            '513100.XSHG',  # 纳指100（海外科技）
            '159920.XSHE',  # 恒生ETF（港股宽基）
            '513520.XSHG',  # 日经225（日本权益）
            '513030.XSHG',  # 德国DAX（德国权益，欧洲的部分代表）

            # 商品
            '518880.XSHG',  # 黄金（避险+通胀）
            '159980.XSHE',  # 有色金属期货ETF（工业金属，不是矿业股票）
            '159985.XSHE',  # 豆粕期货ETF（农业单品种，不代表全部农产品）

            # 利率
            '511090.XSHG',  # 30年国债（久期）
            '511010.XSHG',  # 5年期国债（有久期风险，不是现金）

            '501018.XSHG',  # 南方原油：QDII-FOF-LOF，境外原油基金暴露
        ]

    def calculate_momentum_score(self, code, last_price):
        try:
            history = attribute_history(code, LOOKBACK_DAYS, '1d', ['close'],
                                        skip_paused=False, fq='pre')
            if history is None or len(history) < LOOKBACK_DAYS:
                return np.nan
            closes = np.asarray(history['close'].values[-LOOKBACK_DAYS:], dtype=float)
            prices = np.append(closes, float(last_price))
            if not np.isfinite(prices).all() or np.any(prices <= 0):
                return np.nan
            if np.any(prices[-3:] / prices[-4:-1] < 0.95):
                return 0.0
            scores = [weighted_trend_score(prices[-(window + 1):]) for window in MOMENTUM_WINDOWS]
            return float(np.mean(scores))
        except (KeyError, ValueError, TypeError, OverflowError):
            return np.nan

    def rank_and_filter(self, quotes):
        scores = {}
        for code in self.etf_pool:
            score = self.calculate_momentum_score(code, quotes[code].last_price)
            if math.isfinite(score) and 0 < score < 5:
                scores[code] = score
        return sorted(scores, key=lambda code: (-scores[code], code))

    def execute_trade(self, context):
        portfolio = context.portfolio
        quotes = get_current_data()
        targets = self.rank_and_filter(quotes)[:TARGET_COUNT]
        held = {code for code, position in portfolio.positions.items()
                if position.total_amount > 0}
        target_set = set(targets)
        if held == target_set:
            return
        exits = held - target_set
        for code in sorted(exits):
            order_target(code, 0)
        if not targets or any(self.position_shares(portfolio, code) for code in exits):
            return

        allocation = portfolio.total_value / len(targets)
        target_shares = {code: affordable_shares(allocation, quotes[code].last_price)
                         for code in targets}
        for code, shares in target_shares.items():
            held_shares = self.position_shares(portfolio, code)
            reduction = (held_shares - shares) // ROUND_LOT * ROUND_LOT
            if reduction > 0:
                order_target(code, held_shares - reduction)

        for code, shares in target_shares.items():
            held_shares = self.position_shares(portfolio, code)
            desired_buy = (shares - held_shares) // ROUND_LOT * ROUND_LOT
            affordable = affordable_shares(portfolio.available_cash, quotes[code].last_price)
            quantity = min(desired_buy, affordable)
            if quantity > 0:
                order(code, quantity)

    @staticmethod
    def position_shares(portfolio, code):
        position = portfolio.positions.get(code)
        return position.total_amount if position else 0

    def get_stock_name(self, code):
        info = get_security_info(code)
        return info.display_name if info else code


def record_execution_metrics(context, previous_value):
    """单边换手=(当日买入+卖出成交额)/(2×前次收盘净值)，不含费用。"""
    turnover = float('nan')
    exposure = float('nan')
    try:
        previous_value = float(previous_value)
        if not math.isfinite(previous_value) or previous_value <= 0:
            raise ValueError('前次收盘净值无效')
        traded = 0.0
        for trade in get_trades().values():
            price = float(trade.price)
            amount = abs(float(trade.amount))
            if not math.isfinite(price) or price <= 0 or not math.isfinite(amount):
                raise ValueError('实际成交价格或数量无效')
            traded += price * amount
        if not math.isfinite(traded):
            raise ValueError('实际成交额无效')
        turnover = traded / (2.0 * previous_value) * 100.0
    except Exception as error:
        log.error('[副图换手缺失] %s' % error)
    try:
        total = float(context.portfolio.total_value)
        if not math.isfinite(total) or total <= 0:
            raise ValueError('账户净值无效')
        invested = 0.0
        for position in context.portfolio.positions.values():
            if position.total_amount <= 0:
                continue
            value = float(position.value)
            if not math.isfinite(value) or value < 0:
                raise ValueError('实际持仓市值无效')
            invested += value
        if not math.isfinite(invested):
            raise ValueError('实际持仓总市值无效')
        exposure = invested / total * 100.0
    except Exception as error:
        log.error('[副图仓位缺失] %s' % error)
    record(**{'单边换手%': turnover, '实际仓位%': exposure})


# ========================= 调度入口 =========================
def trade_handler(context):
    g.strategy.execute_trade(context)

def after_market_close(context):
    total = context.portfolio.total_value
    record_execution_metrics(context, getattr(g, 'stats_last_total_value',
                                              context.portfolio.starting_cash))

    # 各自然年独立累计；跨年后的首个交易日输出上一年，避免年末休市漏报。
    year = context.current_dt.year
    if year not in g.holding_stats:
        if g.holding_stats:
            print_yearly_holding_stats(sorted(g.holding_stats)[-1])
        g.holding_stats[year] = {}

    year_stats = g.holding_stats[year]
    held = [code for code, position in context.portfolio.positions.items()
            if position.total_amount > 0]
    if held:
        g.holding_days[year] = g.holding_days.get(year, 0) + 1
        for code in held:
            year_stats[code] = year_stats.get(code, 0) + 1

    # 首年以初始资金为基准，此后以上一年最后记录的收盘净值为基准，
    # 包含新年首个交易日的收益；空仓日也照常记录账户净值。
    today = context.current_dt.date()
    if year not in g.yearly_performance:
        g.yearly_performance[year] = {
            'start_value': getattr(g, 'stats_last_total_value', None),
            'start_date': today,
        }
    g.yearly_performance[year]['end_value'] = float(total)
    g.yearly_performance[year]['end_date'] = today
    g.stats_last_total_value = float(total)


def print_yearly_holding_stats(year):
    """每只标的按实持天数统计；以非空仓日为分母，比例之和可大于 100%。"""
    year_stats = g.holding_stats[year]
    total_days = g.holding_days.get(year, 0)
    log.info("=" * 60)
    performance = getattr(g, 'yearly_performance', {}).get(year)
    if performance and performance['start_value'] is not None and performance['start_value'] > 0:
        start_value = performance['start_value']
        end_value = performance['end_value']
        yearly_return = (end_value / start_value - 1) * 100
        profit = end_value - start_value
        log.info(f"{year}年账户收益（{performance['start_date']} 至 {performance['end_date']}）")
        log.info(f"  年度收益率: {yearly_return:+.2f}% | 收益额: {profit:+,.2f}元")
        log.info(f"  期初资产: {start_value:,.2f}元 | 期末资产: {end_value:,.2f}元")
    else:
        log.info(f"{year}年账户收益: 暂无完整净值记录，需重新回测")
    log.info(f"{year}年持仓统计（持仓总天数: {total_days}天）")
    if total_days > 0:
        sorted_holdings = sorted(year_stats.items(), key=lambda x: x[1], reverse=True)
        for code, days in sorted_holdings:
            pct = days / total_days * 100
            name = g.strategy.get_stock_name(code)
            log.info(f"  {name}({code}): {days}天 ({pct:.1f}%)")
    else:
        log.info("  本年度无持仓")
    log.info("=" * 60)

def on_strategy_end(context):
    """回测结束时逐年打印统计，首尾不足整年的按实际回测区间统计。"""
    if not hasattr(g, 'holding_stats') or not g.holding_stats:
        return
    log.info("")
    log.info(" 最终年度收益及持仓统计报告（各年按实际统计区间计算） ")
    for year in sorted(g.holding_stats):
        print_yearly_holding_stats(year)
