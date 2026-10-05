# 克隆自聚宽文章：https://www.joinquant.com/post/65937
# 标题：【实用贴】一份对研究行业ETF轮动有帮助的代码
# 作者：烟花三月ETF

# 克隆自聚宽文章：https://www.joinquant.com/post/62287
# 标题：双动量ETF，五年年化77.57%，最大回撤16.63%
# 作者：小卡君

from jqdata import *
import numpy as np
import pandas as pd
import math, datetime
import talib
from prettytable import PrettyTable
from jqlib.technical_analysis import MACD
import requests, json, builtins


# ========================= 全局参数区 =========================
# —— 资金比例（对应子账户索引 0/1/2/3）
g.global_strategy_proportion     = 0.5  # （0）
g.momentum_strategy_proportion   = 0.5  # （1）

# —— 下单与风控（通用）——
MIN_ORDER_VALUE    = 2000.0   # 单笔最小金额
RESERVE_CASH_RATIO = 0.00     # 预留现金比例（0=不预留）
ROUND_LOT          = 100      # ETF最小买入单位（100份）

# —— 外部信号开关 ——
g.transdir_flag = 0

# ========================= 初始化 & 热更新 =========================

def initialize(context):
    set_benchmark('000300.XSHG')
    set_option('use_real_price', True)
    set_option('avoid_future_data', True)
    # log.set_level('order', 'error')
    set_order_cost(OrderCost(open_tax=0, close_tax=0,
                             open_commission=0.0002, close_commission=0.0002,
                             close_today_commission=0, min_commission=5), type='fund')
    set_slippage(FixedSlippage(0))

    # —— 仅在 initialize 设置子账户 ——
    subportfolios = [
        SubPortfolioConfig(context.portfolio.starting_cash * g.global_strategy_proportion,     'stock'),  # 0
        SubPortfolioConfig(context.portfolio.starting_cash * g.momentum_strategy_proportion,   'stock'),  # 1
    ]
    set_subportfolios(subportfolios)
    
    _build_strategies(context)

def after_code_changed(context):
    """热更新：重挂任务 + 重建策略对象（禁止再 set_subportfolios）"""
    unschedule_all()
    # 过滤order中低于error级别的日志
    log.set_level('order', 'error')
    log.set_level('system', 'error')
    log.set_level('strategy', 'debug')
    _build_strategies(context)

    # —— 顶层交易与收盘打印 ——
    run_daily(after_market_close,   time="15:01")
    
    run_daily(s1_my_trade,       time="10:55", reference_security='000300.XSHG')
    run_daily(s1_print_rank_after_close, time='15:01')
    
    # —— 策略4：按原策略节奏 —— 
    run_daily(s2_my_trade,               time="10:40", reference_security='000300.XSHG')
    run_daily(s2_stop_loss_by_cur_day,   time='10:00')   # 日内亏损检测
    run_daily(s2_volume_check,           time='13:30')     # 成交量检测
    run_daily(s2_print_rank_after_close, time='15:01')

    # —— 出入金占位（不触发真实入出金，仅示例）——
    g.ENABLE_CASH_IO_ON_HOTRELOAD = True
    g.CASH_IO_PLAN = {0: -1, 1: -1}
    g._cash_io_done_date = None
    _apply_cash_inout_once(context)

    # —— 年度治理（目标权重+标记）——
    g.TARGET_WEIGHTS = {
        0: g.global_strategy_proportion,
        1: g.momentum_strategy_proportion,
    }
    g._annual_rebalanced_year = None
    g._year_end_cleared_year  = None
    
    run_daily(nov_end_liquidate_for_dec_guard, time='14:50')  # 11月最后一个交易日清仓
    run_daily(dec_firstday_rebalance_guard,   time='09:20')   # 12月第一个交易日再平衡



def _build_strategies(context):
    g.strategys = {}
    g.strategys['全球选基策略'] = GlobalFundSelectionStrategy(context, 0, '全球选基策略')
    g.strategys['ETF轮动策略']  = EtfRotationStrategy(context, 1, 'ETF轮动策略')      # 二选一

# ========================= 顶层调度回调 =========================
def after_market_close(context):
    for st in g.strategys.values():
        st.after_market_close(context)
# ========================= 公共基类（仅放通用“交易封装”） =========================
class BaseStrategy:
    def __init__(self, context, subportfolio_index, name, proportion):
        self.subportfolio_index = subportfolio_index
        self.name = name
        self.start_cash = context.portfolio.starting_cash * proportion

    def my_trade(self, context):
        pass

    def after_market_close(self, context):
        sub = context.portfolio.subportfolios[self.subportfolio_index]
        ret = (sub.total_value / self.start_cash - 1) * 100 if self.start_cash else 0
        record(**{self.name + '收益率(%)': ret})
        self.print_holdings(context)

    def print_holdings(self, context):
        sub = context.portfolio.subportfolios[self.subportfolio_index]
        pt = PrettyTable(["策略", "代码", "名称", "买入日", "买入价", "现价", "收益%", "数量", "市值"])
        if sub.long_positions:
            for s in list(sub.long_positions):
                p = sub.long_positions[s]
                pt.add_row([
                    self.name, p.security[:6], get_security_info(p.security).display_name,
                    p.init_time.date(), f"{p.avg_cost:.3f}", f"{p.price:.3f}",
                    f"{(p.price / p.avg_cost - 1) * 100:.2f}%", p.total_amount,
                    f"{p.value / 10000:.3f}万"
                ])
            log.info("\n" + str(pt))
        else:
            log.info(f"{self.name} 无持仓")

    def _safe_last_price(self, security):
        return _safe_last_price_global(security)

    # —— 目标“金额”下单（整手 + 最小金额 + 成交后上报） ——
    def order_target_value_ex(self, context, security, target_value):
        return order_target_value_send(context, self.subportfolio_index, security, target_value)


# ========================= 策略1：全球选基策略（独立工具） =========================
class GlobalFundSelectionStrategy(BaseStrategy):
    def __init__(self, context, subportfolio_index: int = 3, name: str = '全球选基策略'):
        # 继承 BaseStrategy（会自动记录子账户索引与名称，并在成交时统一 send_it）
        BaseStrategy.__init__(self, context, subportfolio_index, name, g.global_strategy_proportion)

        # —— 与原策略一致的配置 —— #
        self.stock_sum = 1                      # 同时持有的ETF数量（1=单核轮动）
        self.m_days    = 25                     # 动量参考天数
        # 境外
        self.etf_pool = [
            # —— 按成立时间升序 ——
            "510180.XSHG",  # 上证180ETF｜成立：2006-04-13（华安） :contentReference[oaicite:0]{index=0}
            "159915.XSHE",  # 创业板ETF｜成立：2011-09-20（易方达） :contentReference[oaicite:1]{index=1}
            "510410.XSHG",  # 上证自然资源ETF｜成立：2012-04-10（博时） :contentReference[oaicite:2]{index=2}
            "513100.XSHG",  # 纳指ETF｜成立：2013-04-25（国泰） :contentReference[oaicite:3]{index=3}
            "518880.XSHG",  # 黄金ETF｜成立：2013-07-18（华安） :contentReference[oaicite:4]{index=4}
            "513030.XSHG",  # 德国DAX ETF｜成立：2014-08-08（华安） :contentReference[oaicite:5]{index=5}
            "501018.XSHG",  # 南方原油（LOF）｜成立：2016-06-15（南方） :contentReference[oaicite:6]{index=6}
            "512290.XSHG",  # 生物医药ETF｜成立：2019-04-18（国泰） :contentReference[oaicite:7]{index=7}
            "512480.XSHG",  # 半导体ETF｜成立：2019-05-08（国联安） :contentReference[oaicite:8]{index=8}
            "513520.XSHG",  # 日经225ETF｜成立：2019-06-12（华夏） :contentReference[oaicite:9]{index=9}
            "512710.XSHG",  # 军工龙头ETF｜成立：2019-07-23（富国） :contentReference[oaicite:10]{index=10}
            "159985.XSHE",  # 豆粕期货ETF｜成立：2019-09-24（华夏） :contentReference[oaicite:11]{index=11}
            "515650.XSHG",  # 消费50ETF｜成立：2019-10-14（富国） :contentReference[oaicite:12]{index=12}
            "159980.XSHE",  # 有色期货ETF｜成立：2019-10-24（大成） :contentReference[oaicite:13]{index=13}
            "515070.XSHG",  # 人工智能ETF｜成立：2019-12-09（华夏） :contentReference[oaicite:14]{index=14}
            "516160.XSHG",  # 新能源ETF｜成立：2021-01-22（南方） :contentReference[oaicite:15]{index=15}
            "159851.XSHE",  # 金融科技ETF｜成立：2021-03-04（华宝） :contentReference[oaicite:16]{index=16}
            "513690.XSHG",  # 港股红利/恒生高股息ETF｜成立：2021-05-11（博时） :contentReference[oaicite:17]{index=17}
            "513130.XSHG",  # 恒生科技ETF｜成立：2021-05-24（华泰柏瑞） :contentReference[oaicite:18]{index=18}
            "159637.XSHE",  # 新能源车龙头ETF｜成立：2022-08-19（东财） :contentReference[oaicite:19]{index=19}
            "159692.XSHE",  # 证券ETF（东财）｜成立：2023-05-05 :contentReference[oaicite:20]{index=20}
            "511090.XSHG",  # 30年期国债ETF｜成立：2023-05-19（鹏扬） :contentReference[oaicite:21]{index=21}
            "588120.XSHG",  # 科创100ETF｜成立：2023-09-06（国泰） :contentReference[oaicite:23]{index=23}
            "159550.XSHE",  # 互联网ETF（沪港深）｜成立：2025-06-18（东财） :contentReference[oaicite:24]{index=24}
        ]

    # ---------- 内部工具：过滤“已上市且样本足够”的代码 ---------- #
    def _eligible_codes(self, context):
        """尽量避免回测早期样本不足导致一片 NaN；留一点上市缓冲天数。"""
        today = context.current_dt.date()
        need_days = self.m_days + 5  # 给一点“自然日缓冲”，简单稳妥
        eligible = []
        for code in self.etf_pool:
            try:
                info = get_security_info(code)
                if (today - info.start_date).days >= need_days:
                    eligible.append(code)
            except Exception:
                # 无法获取信息的，先跳过
                pass
        # 若没有任何满足条件者，退回原池（至少能打印出分数与原因）
        return eligible if eligible else list(self.etf_pool)

    # ---------- 打分与候选：与原策略同名 filter()，但更健壮 ---------- #
    def filter(self, context):
        """
        计算池内所有 ETF 的 ann*r2 得分，并打印【全量降序】列表；
        返回过滤后的候选（0 < score < 6，按 score 降序的代码列表）。
        —— 与原策略一致的接口/风格，但补充了健壮性保护与上市日筛查。
        """
        pool = self._eligible_codes(context)
        data = pd.DataFrame(index=pool, columns=["annualized_returns", "r2", "score"], dtype=float)
        cd = get_current_data()

        for etf in pool:
            try:
                # 仅取 close，减少缺失导致的空表概率
                hist = attribute_history(etf, self.m_days, "1d", ["close"], skip_paused=False, fq='pre')
                if hist is None or hist.empty or len(hist["close"]) < self.m_days:
                    data.loc[etf, "score"] = np.nan
                    continue

                # 正确读取 last_price / 名称（cd[code]）
                try:
                    d = cd[etf]                # 若不存在会抛 KeyError
                    last_px = d.last_price
                    sec_name = d.name
                except Exception:
                    last_px = None
                    sec_name = etf

                # 价格无效 → 记 NaN
                if last_px is None or not np.isfinite(last_px) or last_px <= 0:
                    data.loc[etf, "score"] = np.nan
                    continue

                closes = hist["close"].values
                prices = np.append(closes, last_px)

                # 越界保护（需至少 4 根用于“近3日跌幅”判断）
                if len(prices) < max(self.m_days, 4):
                    data.loc[etf, "score"] = np.nan
                    continue

                # —— 动量回归（加权最小二乘）——
                y = np.log(prices)
                x = np.arange(len(y))
                w = np.linspace(1, 2, len(y))
                slope, intercept = np.polyfit(x, y, 1, w=w)
                ann = math.exp(slope * 250) - 1.0

                resid = y - (slope * x + intercept)
                ss_res = float(np.sum(w * resid * resid))
                ss_tot = float(np.sum(w * (y - np.mean(y)) ** 2))
                r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

                score = ann * r2

                # —— 近3日任意一日累计跌幅超过 5% → 置 0 —— #
                r1 = prices[-1] / prices[-2]
                r2_ = prices[-2] / prices[-3]
                r3  = prices[-3] / prices[-4]
                if min(r1, r2_, r3) < 0.95:
                    score = 0.0

                data.loc[etf, ["annualized_returns", "r2", "score"]] = [ann, r2, score]

            except Exception:
                # 任意异常 → 记 NaN，避免打断全池评估
                data.loc[etf, "score"] = np.nan

        # —— 打印全量得分（降序）——
        try:
            rows = []
            for etf in pool:
                try:
                    nm = cd[etf].name
                except Exception:
                    nm = etf
                sc = data.loc[etf, "score"]
                # 排序时把 NaN 当作 -inf
                sort_key = float(sc) if (isinstance(sc, (int, float)) and sc == sc) else float("-inf")
                rows.append((etf, nm, sc, sort_key))
            rows.sort(key=lambda x: x[3], reverse=True)
            top = rows[:8]  # ★ 只取前5
            log.info("【ETF 得分排名】rank | 代码 | 名称 | score")
            for idx, (code, name, score, _) in enumerate(top, start=1):
                s = "NaN" if (score is None or not isinstance(score, (int, float)) or score != score) else f"{score:.4f}"
                log.info(f"{idx:>3} | {code} | {name} | {s}")
        except Exception as e:
            log.error(f"打印ETF得分列表失败：{e}")


        # —— 候选过滤：0 < score < 6 ——（与你原策略一致）—— #
        data = data.query("0 < score < 6").sort_values(by="score", ascending=False)

        # 返回按得分降序的代码列表
        return data.index.tolist()

    # ---------- 交易逻辑：统一走你的下单封装（最小变动） ---------- #
    def my_trade(self, context):
        """
        交易流程（与原风格一致）：
        1) 用 filter(context) 拿候选列表（已按得分降序 & 安全区间过滤）；
        2) 目标数 = self.stock_sum（默认 1）；
        3) 先卖出不在目标内的持仓；
        4) 将剩余可用资金等额分配给目标标的，调用 BaseStrategy.order_target_value_ex 以走整手+最小金额保护与 send_it。
        """
        sub = context.portfolio.subportfolios[self.subportfolio_index]
        rank_list = self.filter(context)
        targets   = rank_list[:max(1, int(self.stock_sum))]

        # —— 先卖：不在目标内的全部调为 0 —— #
        for s in list(sub.positions):
            if s not in targets:
                self.order_target_value_ex(context, s, 0)

        # —— 再买：等额分配给缺口标的 —— #
        to_buy = [s for s in targets if (s not in sub.positions or sub.positions[s].total_amount == 0)]
        if not to_buy:
            return

        invest_cash = sub.total_value * (1 - RESERVE_CASH_RATIO)
        need_value  = max(0.0, invest_cash - sub.positions_value)
        if need_value <= 0:
            return

        per_value = need_value / len(to_buy)
        for s in to_buy:
            self.order_target_value_ex(context, s, per_value)

# —— 收盘后仅打印分数/股票/排名（不触发交易）——
def s1_print_rank_after_close(context):
    try:
        # 调用 filter(context) 即会打印全池降序分数（含 rank）
        g.strategys['全球选基策略'].filter(context)
        log.info("（仅打印排名，不触发下单）")
    except Exception as e:
        log.error(f"收盘后打印排名失败：{e}")

# —— 调度入口 —— #
def s1_my_trade(context):
    g.strategys['全球选基策略'].my_trade(context)

# ========================= 策略2：ETF轮动（逻辑不变，轻量提速版） =========================
class EtfRotationStrategy(BaseStrategy):
    def __init__(self, context, subportfolio_index: int = 1, name: str = 'ETF轮动策略'):
        BaseStrategy.__init__(self, context, subportfolio_index, name, g.momentum_strategy_proportion)

        # —— 参数与标的池：保持你原逻辑口径 —— #
        self.m_days   = getattr(g, 'm_days', 25)
        self.m_score  = getattr(g, 'm_score', 5)
        self.etf_pool = getattr(g, 'etf_pool_3', [
            # 商品
            '501018.XSHG',  # 南方原油
            '159980.XSHE',  # 有色ETF
            '518880.XSHG',  # 黄金ETF
            # 跨境
            '513520.XSHG',  # 日经ETF
            '513100.XSHG',  # 纳指100
            # 港股
            '513020.XSHG',  # 港股科技
            # 国内
            '510180.XSHG',  # 上证180
            '588120.XSHG',  # 科创板
            '159915.XSHE',  # 创业板
            # 债券
            '511090.XSHG',  # 30年国债ETF
        ])
        self.enable_stop_loss_by_cur_day = getattr(g, 'enable_stop_loss_by_cur_day', True)
        self.stoploss_limit_by_cur_day   = getattr(g, 'stoploss_limit_by_cur_day', -0.03)

        # —— 轻量缓存：当日 current_data、attribute_history、RSRS 计算 —— #
        self.context = context
        self._cached_cur = None
        self._cached_cur_date = None
        self._ah_cache_date = None
        self._ah_cache = {}        # key: (code, days, tuple(fields)) -> DataFrame
        self._rs_cache_date = None
        self._rs_cache = {         # 同日缓存
            'slope18': {},         # key: code -> float
            'beta_250_20': {}      # key: code -> float
        }

    # -------------------- 轻量工具：当日缓存 --------------------
    def _cur(self):
        """当日只取一次 get_current_data()"""
        d = self.context.current_dt.date()
        if self._cached_cur_date != d:
            self._cached_cur = get_current_data()
            self._cached_cur_date = d
        return self._cached_cur

    def _ah(self, code, days, fields):
        """同日缓存的 attribute_history（不改数据口径）"""
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

    def _rs_reset_if_newday(self):
        d = self.context.current_dt.date()
        if self._rs_cache_date != d:
            self._rs_cache_date = d
            self._rs_cache = {'slope18': {}, 'beta_250_20': {}}

    # -------------------- 排名（保持你原筛选链路） --------------------
    def _rank(self, context):
        rank_list = []
        current_data = self._cur()
        for etf in self.etf_pool:
            try:
                df = self._ah(etf, self.m_days, ["close", "high"])
                if df is None or df.empty or len(df["close"]) < self.m_days:
                    continue
                prices = np.append(df["close"].values, current_data[etf].last_price)

                if min(prices[-1] / prices[-2],
                       prices[-2] / prices[-3],
                       prices[-3] / prices[-4]) < 0.95:
                    log.info(f"{etf} {self.get_stock_name(etf)} 近3日跌幅超过5%, 已排除")
                    continue

                if self.enable_stop_loss_by_cur_day:
                    ratio = self.cal_cur_to_open_ratio(etf)
                    if ratio <= self.stoploss_limit_by_cur_day:
                        log.info(f"{etf} {self.get_stock_name(etf)} 进入跌幅达到 {ratio * 100:.2f}%, 已排除")
                        continue

                rank_list.append(etf)
            except Exception:
                continue

        rank_list = self.filter_moment_rank(rank_list, self.m_days, 0, self.m_score)
        rank_list = self.filter_volume(context, rank_list)
        rank_list = self.filter_rsrs(rank_list)
        return rank_list

    # -------------------- 交易：保持你的决策路径 --------------------
    def my_trade(self, context):
        self.context = context  # 确保缓存可用
        sub = context.portfolio.subportfolios[self.subportfolio_index]

        # 1) 拿候选
        rank_list = self._rank(context)

        # 2) 选不出来就清仓
        if not rank_list:
            for s in list(sub.positions):
                if s in self.etf_pool:
                    log.info("👿👿👿👿👿 ETF轮动没有一个能打的, 清仓")
                    self.order_target_value_ex(context, s, 0)
            return

        # 3) 取动量最高的一个
        select_etf = rank_list[0]

        # 4) 当前持仓（池内仅取一个）
        current_etf = None
        for s in sub.positions:
            if s in self.etf_pool:
                current_etf = s
                break

        # 5) 策略资金口径（原式保持）
        if hasattr(g, 'portfolio_value_proportion') and len(g.portfolio_value_proportion) > 2:
            strategy_cash = context.portfolio.total_value * g.portfolio_value_proportion[2]
        else:
            strategy_cash = sub.total_value * (1 - RESERVE_CASH_RATIO)

        # 6) 调仓/建仓
        if current_etf and current_etf != select_etf:
            log.info(f"ETF轮动调仓: {current_etf} -> {select_etf}")
            self.order_target_value_ex(context, current_etf, 0)
            self.order_target_value_ex(context, select_etf, strategy_cash)
        elif (not current_etf) and strategy_cash > 0:
            log.info(f"ETF轮动建仓: {select_etf}")
            self.order_target_value_ex(context, select_etf, strategy_cash)

    # -------------------- 可选：收盘打印 --------------------
    def after_market_close(self, context):
        self.context = context
        super().after_market_close(context)

    # -------------------- 工具：名称/日内涨跌 --------------------
    @staticmethod
    def get_stock_name(security):
        try:
            stock_info = get_security_info(security)
            return stock_info.display_name
        except Exception:
            return "未上市"

    def cal_cur_to_open_ratio(self, security):
        cur = self._cur()
        last_price = cur[security].last_price
        day_open = cur[security].day_open
        return (last_price - day_open) / day_open

    # -------------------- 动量计算（原逻辑；仅用 _cur 缓存） --------------------
    def filter_moment_rank(self, stock_pool, days, ll, hh, show_print=True):
        scores_data = pd.DataFrame(index=stock_pool, columns=["annualized_returns", "r2", "score"])
        cur = self._cur()
        print_data = {}

        for code in stock_pool:
            try:
                hist_data = self._ah(code, days, ["close", "high"])
                if hist_data is None or hist_data.empty:
                    continue

                prices = np.append(hist_data["close"].values, cur[code].last_price)
                log_prices = np.log(prices)
                x_values = np.arange(len(log_prices))
                weights = np.linspace(1, 2, len(log_prices))

                slope, intercept = np.polyfit(x_values, log_prices, 1, w=weights)
                annualized_return = math.exp(slope * 250) - 1
                scores_data.loc[code, "annualized_returns"] = annualized_return

                ss_res = np.sum(weights * (log_prices - (slope * x_values + intercept)) ** 2)
                ss_tot = np.sum(weights * (log_prices - np.mean(log_prices)) ** 2)
                r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
                scores_data.loc[code, "r2"] = r2

                momentum_score = annualized_return * r2
                scores_data.loc[code, "score"] = momentum_score

                if min(prices[-1] / prices[-2], prices[-2] / prices[-3], prices[-3] / prices[-4]) < 0.95:
                    scores_data.loc[code, "score"] = 0
                print_data[code] = scores_data.loc[code, "score"]

            except Exception as e:
                print(f"计算{code}动量得分失败: {e}")
                scores_data.loc[code, "score"] = 0

        valid_etfs = scores_data.query(f"{ll} < score < {hh}").sort_values("score", ascending=False)
        rank_list = valid_etfs.index.tolist()
        if show_print and rank_list:
            _ = [f"{i} {self.get_stock_name(i)} ({print_data[i]:.4f})" for i in rank_list[:4]]
            print(f"动量评分排名: {' > '.join(_)}")
        return rank_list

    # -------------------- 成交量过滤（最小改动：fast_bt 开关） --------------------
    def filter_volume(self, context, stock_list, days=7, volume_threshold=2, check_only=True, check_price=False):
        self.context = context
        fast_bt = getattr(g, 'fast_bt', False)  # 回测设 True；实盘保持 False

        def _is_price_below_open(security):
            cur = self._cur()
            return cur[security].last_price < cur[security].day_open

        def _get_avg_volume_map(codes):
            avg_map = {}
            for code in codes:
                try:
                    hist = self._ah(code, days, ['volume'])
                    if hist is None or hist.empty or len(hist) < days:
                        continue
                    avg_map[code] = hist['volume'].mean()
                except Exception:
                    pass
            return avg_map

        def _today_volume_ratio(code, avg_vol):
            if not avg_vol or avg_vol <= 0:
                return 0.0

            if fast_bt:
                # 回测近似：昨日日线量 / 近 N 日均量（不改阈值与决策）
                try:
                    hist = self._ah(code, days, ['volume'])
                    if hist is None or hist.empty:
                        return 0.0
                    last_vol = float(hist['volume'].iloc[-1])
                    return last_vol / float(avg_vol)
                except Exception:
                    return 0.0
            else:
                # 原逻辑：1m 聚合 → 当日量 / 近 N 日均量
                try:
                    dfm = get_price(code,
                                    start_date=context.current_dt.date(),
                                    end_date=context.current_dt,
                                    frequency='1m',
                                    fields=['volume'],
                                    skip_paused=False, fq='pre',
                                    panel=False, fill_paused=False)
                    if dfm is None or dfm.empty:
                        return 0.0
                    cur_vol = float(dfm['volume'].sum())
                    return cur_vol / float(avg_vol)
                except Exception:
                    return 0.0

        res = []
        avg_map = _get_avg_volume_map(stock_list)

        for stock in stock_list:
            avg_vol = avg_map.get(stock)
            if avg_vol is None:
                res.append(stock)
                continue

            ratio = _today_volume_ratio(stock, avg_vol)
            if ratio > volume_threshold:
                print(f"👾👾👾👾👾 {stock} {self.get_stock_name(stock)} 近{days}日成交量异常, 为均值的{ratio:.4f}倍, {'不纳入选择' if check_only else '执行卖出'}")
                if check_only:
                    continue
                else:
                    position = context.portfolio.positions.get(stock)
                    if not position or position.closeable_amount == 0:
                        continue
                    if position.init_time.date() == context.current_dt.date():
                        continue
                    if check_price and not _is_price_below_open(stock):
                        continue
                    self.order_target_value_ex(context, stock, 0)
            else:
                res.append(stock)

        return res

    # -------------------- RSRS 过滤（同日缓存，计算口径不变） --------------------
    def filter_rsrs(self, stock_list):
        self._rs_reset_if_newday()
        if not stock_list:
            return []

        def _get_slope(code, days=18):
            hit = self._rs_cache['slope18'].get(code)
            if hit is not None:
                return hit
            try:
                hist_data = self._ah(code, days, ['high', 'low'])
                if hist_data is None or hist_data.empty or len(hist_data) < days:
                    return None
                slope = np.polyfit(hist_data['low'].values, hist_data['high'].values, 1)[0]
                self._rs_cache['slope18'][code] = slope
                return slope
            except Exception as e:
                print(f"计算{code} RSRS斜率失败: {e}")
                return None

        def _get_beta(code, lookback_days=250, window=20):
            hit = self._rs_cache['beta_250_20'].get(code)
            if hit is not None:
                return hit
            try:
                hist_data = self._ah(code, lookback_days, ['high', 'low'])
                if hist_data is None or hist_data.empty or len(hist_data) < lookback_days:
                    return None
                lows  = hist_data['low'].values
                highs = hist_data['high'].values
                slope_list = []
                for i in range(len(hist_data) - window + 1):
                    lw = lows[i:i+window]
                    hh = highs[i:i+window]
                    if len(lw) < window or len(hh) < window:
                        continue
                    if np.any(np.isnan(lw)) or np.any(np.isnan(hh)):
                        continue
                    if np.any(np.isinf(lw)) or np.any(np.isinf(hh)):
                        continue
                    if np.std(lw) == 0 or np.std(hh) == 0:
                        continue
                    slope = np.polyfit(lw, hh, 1)[0]
                    slope_list.append(slope)
                if len(slope_list) < 2:
                    beta = None
                else:
                    mean_s = float(np.mean(slope_list))
                    std_s  = float(np.std(slope_list))
                    beta   = mean_s - 2 * std_s
                self._rs_cache['beta_250_20'][code] = beta
                return beta
            except Exception as e:
                print(f"计算{code} RSRS Beta失败: {e}")
                return None

        def _check_with_strength(code):
            _slope = _get_slope(code, 18)
            _beta  = _get_beta(code, 250, 20)
            if _slope is None or _beta is None:
                return None, 0.0
            _strength = (_slope - _beta) / abs(_beta) if _beta != 0 else 0.0
            return _slope > _beta, _strength

        def _check_above_ma(code, days=20):
            try:
                hist = self._ah(code, days, ["close"])
                if hist is None or len(hist) < days:
                    return False
                current_price = self._cur()[code].last_price
                return current_price >= hist["close"].mean()
            except Exception as e:
                print(f"计算{code} {days}日均线失败: {e}")
                return False

        res = []
        for stock in stock_list:
            stock_pass, stock_strength = _check_with_strength(stock)
            if stock_pass:
                if stock_strength > 0.15:
                    res.append(stock)
                elif stock_strength > 0.03 and _check_above_ma(stock, 5):
                    res.append(stock)
                elif _check_above_ma(stock, 10):
                    res.append(stock)
        return res

    # -------------------- 日内止损（原逻辑不变） --------------------
    def stop_loss_by_cur_day(self, context, ratio=-0.03):
        self.context = context
        sub = context.portfolio.subportfolios[self.subportfolio_index]
        holdings = [s for s in sub.positions if s in self.etf_pool]
        for stock in holdings:
            cur_ratio = self.cal_cur_to_open_ratio(stock)
            if cur_ratio is None:
                continue
            if cur_ratio < ratio:
                print(f"{stock} {self.get_stock_name(stock)} 距离开盘跌幅 {cur_ratio * 100:.2f}% 清仓处理")
                self.order_target_value_ex(context, stock, 0)

    # -------------------- 成交量检测（原逻辑不变） --------------------
    def volume_check(self, context, days=7, volume_threshold=2, check_price=True):
        self.context = context
        sub = context.portfolio.subportfolios[self.subportfolio_index]
        holdings = [s for s in sub.positions if s in self.etf_pool]
        self.filter_volume(context,
                           stock_list=holdings,
                           days=days,
                           volume_threshold=volume_threshold,
                           check_only=False,
                           check_price=check_price)

    def print_top_after_close(self, context, top_n: int = 5):
        try:
            _ = self.filter_moment_rank(self.etf_pool, self.m_days, 0, self.m_score, show_print=True)
            ts = context.current_dt.strftime('%Y-%m-%d %H:%M')
            log.info(f"【{ts} {self.name}】动量Top已输出（来自 filter_moment_rank 的打印）")
        except Exception as e:
            log.error(f"{self.name} 收盘打印失败：{e}")


# —— 策略2调度包装（保持你的外部接口） ——
def s2_my_trade(context):               g.strategys['ETF轮动策略'].my_trade(context)
def s2_print_rank_after_close(context): g.strategys['ETF轮动策略'].print_top_after_close(context, top_n=5)
def s2_stop_loss_by_cur_day(context):
    st = g.strategys['ETF轮动策略']
    st.stop_loss_by_cur_day(context, ratio=st.stoploss_limit_by_cur_day)
def s2_volume_check(context):
    g.strategys['ETF轮动策略'].volume_check(context, days=7, volume_threshold=2, check_price=True)

# ========================= 年度治理：出入金&再平衡&年末清仓 =========================

def _apply_cash_inout_once(context):
    if not g.ENABLE_CASH_IO_ON_HOTRELOAD:
        return
    today = context.current_dt.date()
    if getattr(g, '_cash_io_done_date', None) == today:
        return
    for pidx, amt in g.CASH_IO_PLAN.items():
        if not amt:
            continue
        try:
            sub = context.portfolio.subportfolios[pidx]
        except Exception as e:
            log.error(f'[出入金] 子账户索引 {pidx} 不存在：{e}')
            continue
        before_cash = sub.available_cash
        before_val  = sub.total_value
        if amt < 0 and abs(amt) > before_cash:
            log.warn(f'[出入金] 子账户{pidx} 计划出金 {amt:.2f} 超过可用现金 {before_cash:.2f}，将尝试执行，可能失败。')
        try:
            inout_cash(amt, pindex=pidx)
        except Exception as e:
            log.error(f'[出入金] 子账户{pidx} 执行失败：{e}')
            continue
        sub_after = context.portfolio.subportfolios[pidx]
        after_cash = sub_after.available_cash
        after_val  = sub_after.total_value
        io_type = '入金' if amt > 0 else '出金'
        log.info(f'[出入金] 子账户{pidx} {io_type} {amt:.2f} 元 | 现金: {before_cash:.2f}→{after_cash:.2f} | 总资产: {before_val:.2f}→{after_val:.2f}')
    g._cash_io_done_date = today


def _annual_rebalance_by_inout(context, target_weights, min_amount=200.0):
    subs = context.portfolio.subportfolios
    total_value = float(builtins.sum(float(sp.total_value) for sp in subs))
    if total_value <= 0:
        log.warn('[年度再平衡] 组合总资产为0，跳过。')
        return

    desired_pos, desired_neg = {}, {}
    for i in sorted(target_weights.keys()):
        w = max(0.0, float(target_weights.get(i, 0.0)))
        tgt_val = total_value * w
        delta   = float(tgt_val - float(subs[i].total_value))
        if delta > 0:
            desired_pos[i] = delta
        elif delta < 0:
            desired_neg[i] = delta

    if not desired_pos and not desired_neg:
        log.info('[年度再平衡] 已在目标附近，无需调整。')
        return

    capped_neg, feasible_out = {}, 0.0
    for i, amt in desired_neg.items():  # amt < 0
        cap = -float(subs[i].available_cash)
        capped = max(float(amt), cap)
        capped_neg[i] = capped
        feasible_out += -capped

    if feasible_out <= 0:
        log.warn('[年度再平衡] 可用现金不足，无法现金法再平衡。')
        return

    sum_pos = float(builtins.sum(map(float, desired_pos.values())))
    if sum_pos <= 0:
        log.warn('[年度再平衡] 无入金需求，跳过。')
        return

    scale_in = min(1.0, feasible_out / sum_pos)
    final_pos = {i: float(v * scale_in) for i, v in desired_pos.items()}
    sum_in = float(builtins.sum(map(float, final_pos.values())))
    if sum_in <= 0:
        log.warn('[年度再平衡] 缩放后入金为0，跳过。')
        return

    sum_out_cap = float(feasible_out)
    scale_out   = sum_in / sum_out_cap if sum_out_cap > 0 else 0.0
    final_neg   = {i: float(v * scale_out) for i, v in capped_neg.items()}  # 仍为负

    def _round2(x):
        return float(round(x, 2))

    # 出金
    for i, amt in final_neg.items():
        amt = _round2(amt)
        if abs(amt) >= min_amount:
            before_cash = float(subs[i].available_cash); before_val = float(subs[i].total_value)
            try:
                inout_cash(amt, pindex=i)
            except Exception as e:
                log.error(f'[年度再平衡] 子账户{i} 出金失败：{e}')
                continue
            after_cash = float(subs[i].available_cash); after_val = float(subs[i].total_value)
            log.info(f'[年度再平衡] 子账户{i} 出金 {amt:.2f} 元 | 现金: {before_cash:.2f}→{after_cash:.2f} | 总资产: {before_val:.2f}→{after_val:.2f}')

    # 入金
    for i, amt in final_pos.items():
        amt = _round2(amt)
        if amt >= min_amount:
            before_cash = float(subs[i].available_cash); before_val = float(subs[i].total_value)
            try:
                inout_cash(amt, pindex=i)
            except Exception as e:
                log.error(f'[年度再平衡] 子账户{i} 入金失败：{e}')
                continue
            after_cash = float(subs[i].available_cash); after_val = float(subs[i].total_value)
            log.info(f'[年度再平衡] 子账户{i} 入金 {amt:.2f} 元 | 现金: {before_cash:.2f}→{after_cash:.2f} | 总资产: {before_val:.2f}→{after_val:.2f}')

    log.info('[年度再平衡] 现金法执行完毕（按可出金上限缩放）。')

def _clear_all_subportfolios_positions(context):
    """对所有子账户逐一清仓（用全局封装，下单后发信）。"""
    for pidx, sub in enumerate(context.portfolio.subportfolios):
        for s in list(sub.long_positions):     # ← 改这里：用 long_positions
            od = order_target_value_send(context, pidx, s, 0.0)
            log.info(f"[年末清仓] 提交卖单 p{pidx} {s} → target=0, order={bool(od)}")

def annual_rebalance_guard(context):
    """第一个交易日(09:20)按目标权重资金再平衡；用标记防重复。"""
    y = context.current_dt.year
    if getattr(g, '_annual_rebalanced_year', None) == y:
        return
    if _is_first_trading_day_of_year(context):
        log.info(f'[年度再平衡] 触发 {y}-第1个交易日 资金再平衡。')
        _annual_rebalance_by_inout(context, g.TARGET_WEIGHTS, min_amount=200.0)
        g._annual_rebalanced_year = y
        log.info(f'[年度再平衡] {y} 年度再平衡完成。')

# —— 工具：求 12 月首个交易日 & 11 月末个交易日（稳健版）——
def _first_td_of_december(y: int):
    tds = get_trade_days(start_date=f"{y}-12-01", end_date=f"{y}-12-31")
    if tds is None or len(tds) == 0: 
        return None
    return pd.Timestamp(tds[0]).date()

def _last_td_of_november(y: int):
    first_dec = _first_td_of_december(y)
    if not first_dec:
        return None
    # 取“12月首个交易日”的前一自然日为上界，再取此前所有交易日的最后一个
    prev_day = pd.Timestamp(first_dec) - pd.Timedelta(days=1)
    tds = get_trade_days(start_date=f"{y}-01-01", end_date=prev_day.date())
    if tds is None or len(tds) == 0:
        return None
    return pd.Timestamp(tds[-1]).date()

def _is_first_trading_day_of_december(context):
    y = context.current_dt.year
    d = _first_td_of_december(y)
    return (d is not None) and (context.current_dt.date() == d)

def _is_last_trading_day_of_november(context):
    y = context.current_dt.year
    d = _last_td_of_november(y)
    return (d is not None) and (context.current_dt.date() == d)

def nov_end_liquidate_for_dec_guard(context):
    y = context.current_dt.year
    tag = f"{y}-11"
    if getattr(g, '_nov_end_cleared_tag', None) == tag:
        return
    if _is_last_trading_day_of_november(context):
        log.info(f'[12月计划] 触发 {tag} 最后一个交易日清仓（为 12 月再平衡预热）。')
        _clear_all_subportfolios_positions(context)
        g._nov_end_cleared_tag = tag
        log.info(f'[12月计划] {tag} 清仓完成。')

def dec_firstday_rebalance_guard(context):
    y = context.current_dt.year
    tag = f"{y}-12"
    if getattr(g, '_dec_rebalanced_tag', None) == tag:
        return
    if _is_first_trading_day_of_december(context):
        log.info(f'[12月计划] 触发 {tag} 第一个交易日资金再平衡。')
        _annual_rebalance_by_inout(context, g.TARGET_WEIGHTS, min_amount=200.0)
        g._dec_rebalanced_tag = tag
        log.info(f'[12月计划] {tag} 再平衡完成。')
        

# ========================= 公共工具区：外部信号 & 下单封装 =========================
def send_it(context, stock, action, num):
    """成交上报（仅受 g.transdir_flag 控制；加超时，失败不阻塞交易）。"""
    if not g.transdir_flag:
        return

def _post_trade_signal(context, order, intended_value):
    """统一触发 send_it；intended_value>0 视作 buy，否则 sell。"""
    if not order:
        return
    filled = getattr(order, 'filled', 0)
    sec    = getattr(order, 'security', None)
    if filled and sec:
        action = 'buy' if intended_value > 0 else 'sell'
        send_it(context, sec, action, filled)


def _safe_last_price_global(security):
    cd = get_current_data()
    if security not in cd:
        return None
    p = cd[security].last_price
    if p is None or np.isnan(p) or p <= 0:
        return None
    return p


# === PATCH: 仅替换此函数 ===
def order_target_value_send(context, pindex, security, target_value):
    """
    优先用 JQ 的按金额下单接口；若失败再回退为按整手股数下单。
    修复“策略2能打分但不下单”的静默问题。
    """
    sub = context.portfolio.subportfolios[pindex]
    px  = _safe_last_price_global(security)
    cur_amount = sub.positions[security].total_amount if (security in sub.positions) else 0

    # ---- 卖出：直接按金额 0 ----
    if target_value <= 0:
        try:
            od = order_target_value(security, 0, pindex=pindex)
        except Exception as e:
            log.error(f"[卖出失败] p{pindex} {security} 金额=0：{e}")
            od = None
        _post_trade_signal(context, od, -cur_amount * (px or 0))
        return od

    # ---- 买入：优先按金额接口（避免被整手/最小金额卡住）----
    try:
        od = order_target_value(security, float(target_value), pindex=pindex)
        if od:
            _post_trade_signal(context, od, float(target_value) - cur_amount * (px or 0))
            return od
        else:
            log.warn(f"[金额下单返回空] p{pindex} {security} → 回退整手。")
    except Exception as e:
        log.warn(f"[金额下单异常] p{pindex} {security}：{e} → 回退整手。")

    # ---- 回退：按整手股数下单 ----
    if px is None or not np.isfinite(px) or px <= 0:
        log.warn(f"[放弃买入] p{pindex} {security} 价格无效(px={px})。")
        return None
    target_amount = int(float(target_value) / px / ROUND_LOT) * ROUND_LOT
    if target_amount <= 0:
        log.warn(f"[放弃买入] p{pindex} {security} 目标金额不足一手({ROUND_LOT})。")
        return None

    try:
        od = order_target(security, target_amount, pindex=pindex)
    except Exception as e:
        log.error(f"[整手下单失败] p{pindex} {security}：{e}")
        return None

    _post_trade_signal(context, od, (target_amount - cur_amount) * px)
    return od
