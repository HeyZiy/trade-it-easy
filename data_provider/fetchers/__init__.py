# -*- coding: utf-8 -*-
"""
===================================
数据源实现子包
===================================

BaseFetcher 抽象基类 + 各数据源实现。

访问约定：
- 统一接口（get_daily_data / get_realtime_quote / get_market_stats）
  经编排层（manager.py / bars.py / realtime.py）调用；
- 特性接口（get_belong_board / get_sector_quotes 等
  数据源独有能力）由调用方直接 import 对应 Fetcher。
- AmazingData 公共查询按能力从 amazingdata/ 导入，共享 client 管理 SDK 对象。
"""

from .base import BaseFetcher
from .akshare_fetcher import AkshareFetcher
from .efinance_fetcher import EfinanceFetcher
from .tushare_fetcher import TushareFetcher
from .amazingdata_fetcher import AmazingDataFetcher, tgw_configured

__all__ = [
    "BaseFetcher",
    "AkshareFetcher",
    "EfinanceFetcher",
    "TushareFetcher",
    "AmazingDataFetcher",
    "tgw_configured",
]
