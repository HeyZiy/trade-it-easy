# -*- coding: utf-8 -*-
"""
===================================
数据源策略管理器 DataFetcherManager
===================================

持有已实例化的 fetcher（带凭证），把入口调用转成「需求 + 数据源集合」的编排调用：
- get_daily_data：个股日线 → daily.fetch_stock_daily（failover + 新鲜度校验 + 换手率回补）
- get_realtime_quote：实时报价 → A 股委托 realtime.merge_realtime_quotes 跨源合并
- get_market_stats / get_main_fund_flow：路由.routing.query_first（多源取首个非空）

分层：codes/classify_market（市场归类）→ routing（筛源 + failover）→ daily/realtime（策略）
→ 本类（构造与持有 fetcher 集合）。ETF/指数日线见 bars.py。
"""
import logging
from typing import Optional, List, Dict, Any

import pandas as pd

from data_provider.fetchers.base import BaseFetcher
from .codes import normalize_stock_code, classify_market
from .daily import fetch_stock_daily
from .routing import query_first
from .types import (
    KIND_BELONG_BOARD, KIND_FUND_FLOW, KIND_SECTOR_QUOTE,
    KIND_STOCK_DAILY, Need,
)
from .realtime import merge_realtime_quotes

logger = logging.getLogger(__name__)


def get_fetcher():
    """构造 DataFetcherManager 实例（失败返回 None）。供入口层统一调用，避免各模块重复构造。"""
    try:
        return DataFetcherManager()
    except Exception:
        return None



class DataFetcherManager:
    """
    数据源策略管理器
    
    职责：
    1. 管理多个数据源（按优先级排序）
    2. 自动故障切换（Failover）
    3. 提供统一的数据获取接口
    
    切换策略：
    - 优先使用高优先级数据源
    - 失败后自动切换到下一个
    - 所有数据源都失败时抛出异常
    """
    
    def __init__(self, fetchers: Optional[List[BaseFetcher]] = None):
        """
        初始化管理器
        
        Args:
            fetchers: 数据源列表（可选，默认按优先级自动创建）
        """
        self._fetchers: List[BaseFetcher] = []
        
        if fetchers:
            # 按优先级排序
            self._fetchers = sorted(fetchers, key=lambda f: f.priority)
        else:
            # 默认数据源将在首次使用时延迟加载
            self._init_default_fetchers()


    def _init_default_fetchers(self) -> None:
        """
        初始化默认数据源列表

        优先级动态调整逻辑：
        - 如果配置了 TUSHARE_TOKEN：Tushare 优先级提升为 -1（仅次于 AmazingData）
        - 否则按默认优先级：
          -2. AmazingDataFetcher (Priority -2) - 配置了 TGW 凭证时启用（最高）
          -1. TushareFetcher (Priority -1) - 配置了 Token 且初始化成功时（仅次于 AmazingData）
           0. AkshareFetcher (Priority 0)
           1. EfinanceFetcher (Priority 1)
           2. TushareFetcher (Priority 2)
        """
        from data_provider.fetchers.efinance_fetcher import EfinanceFetcher
        from data_provider.fetchers.akshare_fetcher import AkshareFetcher
        from data_provider.fetchers.tushare_fetcher import TushareFetcher
        # 创建所有数据源实例（优先级在各 Fetcher 的 __init__ 中确定）
        efinance = EfinanceFetcher()
        akshare = AkshareFetcher()
        tushare = TushareFetcher()  # 会根据 Token 配置自动调整优先级

        # 初始化数据源列表
        self._fetchers = [
            efinance,
            akshare,
            tushare,
        ]

        # 配置了 TGW 凭证时启用 AmazingData（优先数据源）
        try:
            from data_provider.fetchers.amazingdata_fetcher import AmazingDataFetcher, tgw_configured

            if tgw_configured():
                amazing = AmazingDataFetcher()
                self._fetchers.append(amazing)
                logger.info("已启用 AmazingDataFetcher（星耀数智，TGW 凭证已配置）")
            else:
                logger.debug("未配置 TGW 凭证，跳过 AmazingDataFetcher")
        except Exception as e:
            logger.warning(f"AmazingDataFetcher 初始化失败，已跳过: {e}")

        # 按优先级排序（Tushare 如果配置了 Token 且初始化成功，优先级为 -1，仅次于 AmazingData）
        self._fetchers.sort(key=lambda f: f.priority)

        # 构建优先级说明
        priority_info = ", ".join([f"{f.name}(P{f.priority})" for f in self._fetchers])
        logger.info(f"已初始化 {len(self._fetchers)} 个数据源（按优先级）: {priority_info}")

    
    def get_daily_data(
        self,
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 30
    ) -> pd.DataFrame:
        """
        获取日线数据（自动切换数据源）
        
        数据源筛选与切换：
        1. 按市场归类（codes.classify_market）+ 各源能力声明（BaseFetcher.SUPPORTS）筛出候选，
           不按数据源类名判断
        2. 候选中从最高优先级数据源开始尝试
        3. 捕获异常后自动切换到下一个
        4. 记录每个数据源的失败原因
        5. 所有数据源失败后抛出详细异常
        
        Args:
            stock_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            days: 获取天数
            
        Returns:
            DataFrame: 标准化日线数据（命中哪个数据源见日志）
            
        Raises:
            DataFetchError: 所有数据源都失败时抛出
        """
        code = normalize_stock_code(stock_code)
        need = Need(KIND_STOCK_DAILY, code, classify_market(code))
        return fetch_stock_daily(
            need, self._fetchers, start_date=start_date, end_date=end_date, days=days
        )
    


    
    def get_realtime_quote(self, stock_code: str):
        """
        获取实时行情数据（自动故障切换）
        
        取数路径：
        1. A 股 → 委托 realtime.merge_realtime_quotes 按 source_priority 跨源合并
        2. 全部失败返回 None（降级兜底）
        
        Args:
            stock_code: 股票代码
            
        Returns:
            UnifiedRealtimeQuote 对象，所有数据源都失败则返回 None
        """
        # Normalize code (strip SH/SZ prefix etc.)
        stock_code = normalize_stock_code(stock_code)

        from src.config import get_config

        config = get_config()

        # 如果实时行情功能被禁用，直接返回 None
        if not config.enable_realtime_quote:
            logger.debug(f"[实时行情] 功能已禁用，跳过 {stock_code}")
            return None

        # 跨源合并（A 股按 source_priority）抽离为与 manager 解耦的纯函数。
        # 未来剪除日线杂活后，调用方只需持有 fetcher 集合即可直接调用，不再依赖本类。
        source_priority = config.realtime_source_priority.split(',')
        return merge_realtime_quotes(stock_code, self._fetchers, source_priority)

    def get_market_stats(self) -> Dict[str, Any]:
        """获取市场涨跌统计（自动切换数据源）；全部源无数据返回 {}。"""
        stats = query_first("市场统计", self._fetchers, "get_market_stats")
        if stats is None:
            logger.warning("[市场统计] 无可用数据源")
            return {}
        return stats


    def get_main_fund_flow(self, stock_code: str, days: int = 5) -> Optional[pd.DataFrame]:
        """
        获取个股主力资金流向（自动切换数据源）。

        当前仅 AkshareFetcher 声明 (fund_flow, cn)，其余源由能力筛选直接排除。

        Args:
            stock_code: 股票代码
            days: 统计天数（默认 5 个交易日）

        Returns:
            标准化 DataFrame（列：date / main_net_inflow，单位元）；全部无数据时返回 None
        """
        code = normalize_stock_code(stock_code)
        df = query_first(
            f"主力资金流 {code}", self._fetchers, "get_main_fund_flow", code, days=days,
            need=Need(KIND_FUND_FLOW, code, classify_market(code)),
        )
        return df

    def get_sector_pct_map(self) -> Dict[str, float]:
        """全量行业板块当日涨跌幅 {板块名: 涨跌幅%}（多源 failover）；全部无数据返回 {}。

        市场级数据，无单一标的，Need.code 传空串。
        """
        result = query_first(
            "板块行情", self._fetchers, "get_sector_pct_map",
            need=Need(KIND_SECTOR_QUOTE, "", "cn"),
        )
        return result if isinstance(result, dict) else {}

    def get_belong_board(self, stock_code: str) -> Optional[str]:
        """个股所属行业板块名称（多源 failover）；全部无数据返回 None。"""
        code = normalize_stock_code(stock_code)
        return query_first(
            f"所属板块 {code}", self._fetchers, "get_belong_board_name", code,
            need=Need(KIND_BELONG_BOARD, code, classify_market(code)),
        )



