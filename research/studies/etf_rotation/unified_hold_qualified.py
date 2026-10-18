# ETF跨资产动量：持仓合格则继续持有（11只池、聚宽单文件对照）
# 对照 new_unified_expanded.py，仅取消排名下降触发的换仓。
# 空仓时仍买合格候选第一名；持仓仍合格则继续持有，即使已不是第一名。
# 持仓不合格时退出；有其他合格候选则换入第一名，否则持有现金。
# 合格条件仍为0<分数<5、通过原跌幅过滤且数据有效；未新增趋势识别算法。
# 保留25日评分、单只持仓、费用、10:40调仓和年度收益/持仓统计。
# 回测设置与11只排名轮动版相同：2016-01-01至2026-10-01，10万元，频率每天。
# 分钟口径验证时，两版本同步切换为分钟频率，不混用不同频率的结果。
# 本地验证不代表聚宽回测结果；收益、回撤、夏普和成交次数以平台运行结果为准。

from jqdata import *
import numpy as np
import pandas as pd
import math

# ========================= 全局参数区 =========================
# —— 下单与风控 ——
MIN_ORDER_VALUE    = 2000.0   # 单笔最小金额
RESERVE_CASH_RATIO = 0.00     # 预留现金比例
ROUND_LOT          = 100      # ETF最小买入单位

# —— 外部信号开关（占位，实际未使用）——
g.transdir_flag = 0

# ========================= 初始化 =========================
def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    set_order_cost(OrderCost(open_tax=0, close_tax=0,
                             open_commission=0.0002, close_commission=0.0002,
                             close_today_commission=0, min_commission=5), type='fund')
    set_slippage(FixedSlippage(0))
    
    # 单一策略，不再分子账户
    g.strategy = EtfRotationStrategy(context)
    
    # 初始化按自然年分组的持仓统计
    g.holding_stats = {}
    g.last_position = None
    g.yearly_performance = {}
    g.stats_last_total_value = float(context.portfolio.starting_cash)
    
    # 调度任务
    run_daily(trade_handler, time="10:40", reference_security='000300.XSHG')
    run_daily(after_market_close, time='15:01')
    
    # 可选过滤器的日内检查（如果启用）
    run_daily(intraday_stop_check, time='10:00')
    run_daily(volume_check, time='13:30')


def after_code_changed(context):
    """热更新"""
    unschedule_all()
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    log.set_level('strategy', 'info')  # 只保留策略日志
    
    g.strategy = EtfRotationStrategy(context)
    
    run_daily(trade_handler, time="10:40", reference_security='000300.XSHG')
    run_daily(after_market_close, time='15:01')
    run_daily(intraday_stop_check, time='10:00')
    run_daily(volume_check, time='13:30')
    
    # 初始化按自然年分组的持仓统计
    g.holding_stats = {}
    g.last_position = None
    g.yearly_performance = {}
    # 热更新从当前净值重新记录，收益区间会在报告中显示。
    g.stats_last_total_value = float(context.portfolio.total_value)


# ========================= 策略核心 =========================
class EtfRotationStrategy:
    def __init__(self, context):
        self.name = 'ETF跨资产动量轮动'
        self.context = context
        
        # ======== 核心参数 ========
        self.m_days = 25              # 动量回看天数
        self.m_score_low = 0          # 动量分数下限
        self.m_score_high = 5         # 动量分数上限
        
        # ======== 扩充地域覆盖池（11只） ========
        # 保留原8只，新增港股宽基、日本和德国；不添加行业/主题ETF。
        self.etf_pool = [
            # 国内权益
            '510300.XSHG',  # 沪深300（中国宽基）
            '159915.XSHE',  # 创业板（国内成长）
            '515180.XSHG',  # 中证红利（国内价值/防御）
            
            # 海外权益
            '513500.XSHG',  # 标普500（海外宽基）
            '513100.XSHG',  # 纳指100（海外科技）
            '159920.XSHE',  # 恒生ETF（港股宽基）
            '513520.XSHG',  # 日经225（日本权益）
            '513030.XSHG',  # 德国DAX（德国权益，欧洲的部分代表）
            
            # 商品
            '518880.XSHG',  # 黄金（避险+通胀）
            
            # 利率
            '511090.XSHG',  # 30年国债（久期）
            '511010.XSHG',  # 5年期国债（有久期风险，不是现金）
        ]
        
        # ======== 可选过滤器开关 ========
        # 建议：先全关闭跑基线，再逐个打开做消融实验
        self.enable_drop_filter = True        # 近3日单日跌幅>5%排除
        self.enable_intraday_stop = False     # 日内相对开盘-3%清仓（容易误杀）
        self.enable_volume_filter = False     # 成交量>7日均量2倍排除（回测口径不一致）
        self.enable_rsrs_filter = False       # RSRS趋势过滤（计算量大且增量存疑）
        
        self.stoploss_limit = -0.03           # 日内止损阈值
        self.volume_days = 7
        self.volume_threshold = 2.0
        
        # ======== 缓存 ========
        self._cached_cur = None
        self._cached_cur_date = None
        self._ah_cache_date = None
        self._ah_cache = {}
        self._rs_cache_date = None
        self._rs_cache = {'slope18': {}, 'beta_250_20': {}}
    
    # -------------------- 工具方法 --------------------
    def _cur(self):
        d = self.context.current_dt.date()
        if self._cached_cur_date != d:
            self._cached_cur = get_current_data()
            self._cached_cur_date = d
        return self._cached_cur
    
    def _ah(self, code, days, fields):
        d = self.context.current_dt.date()
        if self._ah_cache_date != d:
            self._ah_cache_date = d
            self._ah_cache = {}
        key = (code, days, tuple(sorted(fields)))
        if key in self._ah_cache:
            return self._ah_cache[key]
        df = attribute_history(code, days, '1d', fields, skip_paused=False, fq='pre')
        self._ah_cache[key] = df
        return df
    
    def get_stock_name(self, code):
        try:
            return get_security_info(code).display_name
        except:
            return code
    
    def cal_cur_to_open_ratio(self, code):
        cur = self._cur()
        last = cur[code].last_price
        open_px = cur[code].day_open
        return (last - open_px) / open_px if open_px else 0
    
    # -------------------- 动量打分 --------------------
    def calculate_momentum_score(self, code):
        """25日加权动量 × R²"""
        try:
            hist = self._ah(code, self.m_days, ["close"])
            if hist is None or hist.empty or len(hist["close"]) < self.m_days:
                return np.nan
            
            cur = self._cur()
            prices = np.append(hist["close"].values, cur[code].last_price)
            
            if len(prices) < self.m_days or not np.isfinite(prices).all():
                return np.nan
            
            # 加权线性回归
            y = np.log(prices)
            x = np.arange(len(y))
            w = np.linspace(1, 2, len(y))
            slope, intercept = np.polyfit(x, y, 1, w=w)
            ann = math.exp(slope * 250) - 1.0
            
            # 加权R²
            effective_w = w ** 2
            mean_y = float(np.average(y, weights=effective_w))
            resid = y - (slope * x + intercept)
            ss_res = float(np.sum(effective_w * resid * resid))
            ss_tot = float(np.sum(effective_w * (y - mean_y) ** 2))
            r2 = float(np.clip(1 - ss_res / ss_tot, 0.0, 1.0)) if ss_tot > 0 else 0.0
            
            score = ann * r2
            
            # 过滤器1：近3日单日跌幅
            if self.enable_drop_filter and len(prices) >= 4:
                r1 = prices[-1] / prices[-2]
                r2 = prices[-2] / prices[-3]
                r3 = prices[-3] / prices[-4]
                if r1 < 0.95 or r2 < 0.95 or r3 < 0.95:
                    return 0.0
            
            return score
        except Exception as e:
            # 不打印每次计算失败，只在最终无候选时提示
            return np.nan
    
    # -------------------- 排名与过滤 --------------------
    def rank_and_filter(self):
        """返回可交易候选列表（按动量降序）"""
        scores = {}
        cur = self._cur()
        
        for code in self.etf_pool:
            score = self.calculate_momentum_score(code)
            if np.isfinite(score) and self.m_score_low < score < self.m_score_high:
                scores[code] = score
        
        if not scores:
            return []
        
        # 按分数降序
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        candidates = [code for code, _ in ranked]
        
        # 只打印前3名（减少日志）
        top3 = [(c, s, self.get_stock_name(c)) for c, s in ranked[:3]]
        
        # 过滤器2：日内止损
        if self.enable_intraday_stop:
            filtered = []
            for code in candidates:
                ratio = self.cal_cur_to_open_ratio(code)
                if ratio > self.stoploss_limit:
                    filtered.append(code)
            candidates = filtered
        
        # 过滤器3：成交量异常
        if self.enable_volume_filter and candidates:
            candidates = self.filter_volume_in_rank(candidates)
        
        # 过滤器4：RSRS
        if self.enable_rsrs_filter and candidates:
            candidates = self.filter_rsrs(candidates)
        
        return candidates
    
    def filter_volume_in_rank(self, codes):
        """简化版成交量过滤：仅用于排名阶段"""
        res = []
        for code in codes:
            try:
                hist = self._ah(code, self.volume_days, ['volume'])
                if hist is None or len(hist) < self.volume_days:
                    res.append(code)
                    continue
                avg_vol = hist['volume'].mean()
                last_vol = hist['volume'].iloc[-1]
                if avg_vol > 0 and last_vol / avg_vol > self.volume_threshold:
                    pass  # 排除但不打印
                else:
                    res.append(code)
            except:
                res.append(code)
        return res
    
    def filter_rsrs(self, codes):
        """RSRS过滤（保留原逻辑但标记为可选）"""
        # 此处省略完整RSRS实现以简化代码
        # 实际使用时可从原代码复制完整的_rs_reset_if_newday等方法
        return codes
    
    # -------------------- 交易执行 --------------------
    def execute_trade(self):
        """单一持仓逻辑"""
        candidates = self.rank_and_filter()
        
        # 当前持仓
        positions = self.context.portfolio.positions
        current_holding = None
        for code in positions:
            if code in self.etf_pool and positions[code].total_amount > 0:
                current_holding = code
                break
        
        # 唯一行为变化：自身仍符合原候选条件，就不因相对排名下降换仓。
        if current_holding and current_holding in candidates:
            g.last_position = current_holding
            return

        # 目标
        target = candidates[0] if candidates else None
        
        # 无候选则清仓
        if not target:
            if current_holding:
                log.info(f"[调仓] {current_holding}({self.get_stock_name(current_holding)}) → 空仓")
                self.order_target_value_safe(current_holding, 0)
                g.last_position = None
            return
        
        # 调仓
        if current_holding and current_holding != target:
            log.info(f"[调仓] {current_holding}({self.get_stock_name(current_holding)}) → "
                    f"{target}({self.get_stock_name(target)})")
            self.order_target_value_safe(current_holding, 0)
            cash = self.context.portfolio.available_cash
            self.order_target_value_safe(target, cash * (1 - RESERVE_CASH_RATIO))
            g.last_position = target
        elif not current_holding:
            log.info(f"[建仓] {target}({self.get_stock_name(target)})")
            cash = self.context.portfolio.available_cash
            self.order_target_value_safe(target, cash * (1 - RESERVE_CASH_RATIO))
            g.last_position = target
        else:
            # 持仓不变，不打印
            g.last_position = target
    
    def order_target_value_safe(self, code, target_value):
        """安全下单封装"""
        try:
            if target_value <= 0:
                order_target_value(code, 0)
            else:
                # 优先用金额接口
                od = order_target_value(code, float(target_value))
                if not od:
                    # 回退整手
                    cur = self._cur()
                    px = cur[code].last_price
                    if px and px > 0:
                        shares = int(target_value / px / ROUND_LOT) * ROUND_LOT
                        if shares > 0:
                            order_target(code, shares)
        except Exception as e:
            log.error(f"{code} 下单失败: {e}")
    
    # -------------------- 日内检查 --------------------
    def check_intraday_stop(self):
        """10:00检查日内止损"""
        if not self.enable_intraday_stop:
            return
        positions = self.context.portfolio.positions
        for code in positions:
            if code in self.etf_pool and positions[code].total_amount > 0:
                ratio = self.cal_cur_to_open_ratio(code)
                if ratio < self.stoploss_limit:
                    log.info(f"{code} 日内止损({ratio*100:.2f}%)，清仓")
                    self.order_target_value_safe(code, 0)
    
    def check_volume_holding(self):
        """13:30检查持仓成交量"""
        if not self.enable_volume_filter:
            return
        # 简化实现：略
        pass


# ========================= 调度入口 =========================
def trade_handler(context):
    g.strategy.execute_trade()

def after_market_close(context):
    total = context.portfolio.total_value
    ret = (total / context.portfolio.starting_cash - 1) * 100
    record(**{'收益率%': ret})
    
    # 确保统计字典已初始化
    if not hasattr(g, 'holding_stats'):
        g.holding_stats = {}
    if not hasattr(g, 'last_position'):
        g.last_position = None
    if not hasattr(g, 'yearly_performance'):
        g.yearly_performance = {}
    
    # 各自然年独立累计；跨年后的首个交易日输出上一年，避免年末休市漏报。
    year = context.current_dt.year
    if year not in g.holding_stats:
        if g.holding_stats:
            print_yearly_holding_stats(sorted(g.holding_stats)[-1])
        g.holding_stats[year] = {}

    year_stats = g.holding_stats[year]
    if g.last_position:
        year_stats[g.last_position] = year_stats.get(g.last_position, 0) + 1

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
    """按当年有持仓的天数计算占比，空仓日不计入分母。"""
    year_stats = g.holding_stats[year]
    # 聚宽环境裸 sum 被覆盖（numpy.sum 语义），显式累加。
    total_days = 0
    for days in year_stats.values():
        total_days += days
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

def intraday_stop_check(context):
    g.strategy.check_intraday_stop()

def volume_check(context):
    g.strategy.check_volume_holding()
