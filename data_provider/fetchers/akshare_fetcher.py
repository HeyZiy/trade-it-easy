# -*- coding: utf-8 -*-
"""
===================================
AkshareFetcher - 主数据源 (Priority 1)
===================================

数据来源：
1. 东方财富爬虫（通过 akshare 库） - 默认数据源
2. 新浪财经接口 - 备选数据源
3. 腾讯财经接口 - 备选数据源

特点：免费、无需 Token、数据全面
风险：爬虫机制易被反爬封禁

防封禁策略：
1. Throttle 节流：请求间隔补足 + 随机休眠 2-5 秒
2. 新浪/腾讯直连请求携带随机 User-Agent
3. 使用 tenacity 实现指数退避重试
4. 熔断器机制：连续失败后自动冷却

增强数据：
- 实时行情：量比、换手率、市盈率、市净率、总市值、流通市值
- 筹码分布：获利比例、平均成本、筹码集中度
"""

import logging
import os
import random
import time
from typing import Optional, Dict, Any, Tuple, List

import pandas as pd
import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from data_provider._crosscut import Throttle, TtlSnapshotCache, classify_http_error

from data_provider.codes import is_etf_code, is_us_stock_code, market_suffix
from data_provider.fetchers.base import BaseFetcher
from data_provider.fetchers._snapshot_quote import snapshot_realtime_quote
from data_provider.fetchers._direct_quote import direct_realtime_quote
from data_provider.stats import calc_market_stats
from data_provider.types import (
    KIND_FUND_FLOW, KIND_SECTOR_QUOTE, KIND_STOCK_DAILY,
    DataFetchError, RateLimitError, STANDARD_COLUMNS,
    UnifiedRealtimeQuote,
    get_realtime_circuit_breaker, safe_float, safe_int,
)


logger = logging.getLogger(__name__)

SINA_REALTIME_ENDPOINT = "hq.sinajs.cn/list"
TENCENT_REALTIME_ENDPOINT = "qt.gtimg.cn/q"


# User-Agent 池，供新浪/腾讯直连请求的 headers 随机轮换
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]


# 全量快照缓存（TtlSnapshotCache，见 data_provider/_crosscut.py）
# TTL 设为 20 分钟 (1200秒)：
# - 批量分析场景：通常 30 只股票在 5 分钟内分析完，20 分钟足够覆盖
# - 实时性要求：股票分析不需要秒级实时数据，20 分钟延迟可接受
# - 防封禁：减少 API 调用频率
_realtime_cache = TtlSnapshotCache(ttl=1200, label="A股实时行情(东财)")
_etf_realtime_cache = TtlSnapshotCache(ttl=1200, label="ETF实时行情(东财)")


def _to_sina_tx_symbol(stock_code: str) -> str:
    """Convert 6-digit A-share code to sh/sz/bj prefixed symbol for Sina/Tencent APIs.

    市场归属唯一权威 codes.market_suffix；码族外抛 ValueError（fail-closed），
    该腿按失败记录，交给内层 东财→新浪→腾讯 failover。
    """
    base = (stock_code.strip().split(".")[0] if "." in stock_code else stock_code).strip()
    suffix = market_suffix(base)
    if suffix is None:
        raise ValueError(f"无法确定 {base} 的市场归属（码族外），拒绝构造新浪/腾讯符号")
    return suffix.lower() + base


def _direct_headers(referer: str) -> dict:
    """直连请求头：随机 UA + 端点 Referer（新浪/腾讯两腿共用形状）。"""
    return {'Referer': referer, 'User-Agent': random.choice(USER_AGENTS)}


def _parse_sina_fields(fields: List[str], stock_code: str) -> UnifiedRealtimeQuote:
    """新浪快照字段下标解析（品种无关，基金代码同构）。

    0:名称 1:今开 2:昨收 3:最新价 4:最高 5:最低 8:成交量(股) 9:成交额(元)。
    """
    price = safe_float(fields[3])
    pre_close = safe_float(fields[2])
    change_pct = None
    change_amount = None
    if price and pre_close and pre_close > 0:
        change_amount = price - pre_close
        change_pct = (change_amount / pre_close) * 100
    quote = UnifiedRealtimeQuote(
        code=stock_code,
        name=fields[0],
        source="akshare_sina",
        price=price,
        change_pct=change_pct,
        change_amount=change_amount,
        volume=safe_int(fields[8]),
        amount=safe_float(fields[9]),
        open_price=safe_float(fields[1]),
        high=safe_float(fields[4]),
        low=safe_float(fields[5]),
        pre_close=pre_close,
    )
    logger.info(f"[实时行情-新浪] @SYM@ {quote.name}: 价格={quote.price}, "
                f"涨跌={quote.change_pct}, 成交量={quote.volume}")
    return quote


def _parse_tencent_fields(fields: List[str], stock_code: str) -> UnifiedRealtimeQuote:
    """腾讯快照字段下标解析（品种无关，基金代码同构）。

    1:名称 3:最新价 4:昨收 5:今开 6:成交量(手→股) 31:涨跌额 32:涨跌幅
    33:最高 34:最低 38:换手率 39:市盈率 43:振幅 44/45:市值(亿→元) 46:市净率 49:量比。
    注意：44/45 是 safe_float 后直接乘 1e8，载荷出现非数值会 TypeError
    （真实载荷约 88 字段且为数值；短载荷异常由 _direct_quote 外层 fail-soft 兜底）。
    """
    quote = UnifiedRealtimeQuote(
        code=stock_code,
        name=fields[1] if len(fields) > 1 else "",
        source="tencent",
        price=safe_float(fields[3]),
        change_pct=safe_float(fields[32]),
        change_amount=safe_float(fields[31]) if len(fields) > 31 else None,
        volume=safe_int(fields[6]) * 100 if fields[6] else None,
        open_price=safe_float(fields[5]),
        high=safe_float(fields[33]) if len(fields) > 33 else None,
        low=safe_float(fields[34]) if len(fields) > 34 else None,
        pre_close=safe_float(fields[4]),
        turnover_rate=safe_float(fields[38]) if len(fields) > 38 else None,
        amplitude=safe_float(fields[43]) if len(fields) > 43 else None,
        volume_ratio=safe_float(fields[49]) if len(fields) > 49 else None,
        pe_ratio=safe_float(fields[39]) if len(fields) > 39 else None,
        pb_ratio=safe_float(fields[46]) if len(fields) > 46 else None,
        circ_mv=safe_float(fields[44]) * 100000000 if len(fields) > 44 and fields[44] else None,
        total_mv=safe_float(fields[45]) * 100000000 if len(fields) > 45 and fields[45] else None,
    )
    logger.info(f"[实时行情-腾讯] {stock_code} {quote.name}: 价格={quote.price}, "
                f"涨跌={quote.change_pct}%, 量比={quote.volume_ratio}, "
                f"换手率={quote.turnover_rate}%")
    return quote


def _build_em_stock_quote(row: pd.Series, stock_code: str) -> UnifiedRealtimeQuote:
    """东财 A 股快照一行 → 统一报价（列映射是该端点的本地数据）。"""
    quote = UnifiedRealtimeQuote(
        code=stock_code,
        name=str(row.get('名称', '')),
        source="akshare_em",
        price=safe_float(row.get('最新价')),
        change_pct=safe_float(row.get('涨跌幅')),
        change_amount=safe_float(row.get('涨跌额')),
        volume=safe_int(row.get('成交量')),
        amount=safe_float(row.get('成交额')),
        volume_ratio=safe_float(row.get('量比')),
        turnover_rate=safe_float(row.get('换手率')),
        amplitude=safe_float(row.get('振幅')),
        open_price=safe_float(row.get('今开')),
        high=safe_float(row.get('最高')),
        low=safe_float(row.get('最低')),
        pe_ratio=safe_float(row.get('市盈率-动态')),
        pb_ratio=safe_float(row.get('市净率')),
        total_mv=safe_float(row.get('总市值')),
        circ_mv=safe_float(row.get('流通市值')),
        change_60d=safe_float(row.get('60日涨跌幅')),
        high_52w=safe_float(row.get('52周最高')),
        low_52w=safe_float(row.get('52周最低')),
    )
    logger.info(f"[实时行情-东财] {stock_code} {quote.name}: 价格={quote.price}, "
                f"涨跌={quote.change_pct}%, 量比={quote.volume_ratio}, "
                f"换手率={quote.turnover_rate}%")
    return quote


def _build_em_etf_quote(row: pd.Series, stock_code: str) -> UnifiedRealtimeQuote:
    """东财 ETF 快照一行 → 统一报价（列名与股票快照不同：开盘价/最高价/最低价）。"""
    quote = UnifiedRealtimeQuote(
        code=stock_code,
        name=str(row.get('名称', '')),
        source="akshare_em",
        price=safe_float(row.get('最新价')),
        change_pct=safe_float(row.get('涨跌幅')),
        change_amount=safe_float(row.get('涨跌额')),
        volume=safe_int(row.get('成交量')),
        amount=safe_float(row.get('成交额')),
        volume_ratio=safe_float(row.get('量比')),
        turnover_rate=safe_float(row.get('换手率')),
        amplitude=safe_float(row.get('振幅')),
        open_price=safe_float(row.get('开盘价')),
        high=safe_float(row.get('最高价')),
        low=safe_float(row.get('最低价')),
        total_mv=safe_float(row.get('总市值')),
        circ_mv=safe_float(row.get('流通市值')),
        high_52w=safe_float(row.get('52周最高')),
        low_52w=safe_float(row.get('52周最低')),
    )
    logger.info(f"[实时行情-东财ETF] {stock_code} {quote.name}: 价格={quote.price}, "
                f"涨跌={quote.change_pct}%, 换手率={quote.turnover_rate}%")
    return quote


class AkshareFetcher(BaseFetcher):
    """
    Akshare 数据源实现
    
    优先级：1（最高）
    数据来源：东方财富网爬虫
    
    关键策略：
    - 每次请求前 Throttle 节流（间隔补足 + 随机休眠 2.0-5.0 秒）
    - 失败后指数退避重试（最多3次）
    """
    
    name = "AkshareFetcher"
    # 提升为最高优先级（有新浪财经作为备选，更稳定）
    priority = int(os.getenv("AKSHARE_PRIORITY", "0"))
    # 东财/新浪日线均含换手率列
    SUPPORTS_COLUMNS = {'date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg', 'turnover_rate'}

    # 日线覆盖 A股；主力资金流/行业板块行情仅 A股。字母 ticker 明确拒绝（美股不在支持范围）；
    # 港股不在支持范围，港股代码流入 A 股接口自然以查无数据失败
    SUPPORTS = frozenset({
        (KIND_STOCK_DAILY, "cn"),
        (KIND_FUND_FLOW, "cn"), (KIND_SECTOR_QUOTE, "cn"),
    })

    # 实时端点自声明（源名唯一权威，merge_realtime_quotes 按此建索引）；
    # akshare_qq 为 tencent 别名，与历史优先级串兼容
    REALTIME_VARIANTS = {
        "akshare_em": {"source": "em"},
        "akshare_sina": {"source": "sina"},
        "tencent": {"source": "tencent"},
        "akshare_qq": {"source": "tencent"},
    }
    def __init__(self, sleep_min: float = 2.0, sleep_max: float = 5.0):
        """
        初始化 AkshareFetcher
        
        Args:
            sleep_min: 节流最小休眠时间（秒）
            sleep_max: 节流最大休眠时间（秒）
        """
        self._throttle = Throttle(sleep_min, sleep_max)
    
    @retry(
        stop=stop_after_attempt(3),  # 最多重试3次
        wait=wait_exponential(multiplier=1, min=2, max=30),  # 指数退避：2, 4, 8... 最大30秒
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 Akshare 获取原始数据
        
        根据代码类型自动选择 API：
        - 字母 ticker（美股不在支持范围）：不支持，抛出异常
        - ETF 基金：使用 ak.fund_etf_hist_em()
        - 普通 A 股：使用 ak.stock_zh_a_hist()
        
        流程：
        1. 判断代码类型（字母 ticker/ETF/A股）
        2. 节流休眠（Throttle.wait）
        3. 调用对应的 akshare API
        4. 处理返回数据
        """
        # 根据代码类型选择不同的获取方法
        if is_us_stock_code(stock_code):
            # 字母 ticker（含 SPX/DJI 等指数符号）：美股不在支持范围，直接拒收
            raise DataFetchError(
                f"AkshareFetcher 不支持字母 ticker {stock_code}（美股不在支持范围）"
            )
        elif is_etf_code(stock_code):
            return self._fetch_etf_data(stock_code, start_date, end_date)
        else:
            return self._fetch_stock_data(stock_code, start_date, end_date)
    
    def _fetch_stock_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取普通 A 股历史数据

        策略：
        1. 优先尝试东方财富接口 (ak.stock_zh_a_hist)
        2. 失败后尝试新浪财经接口 (ak.stock_zh_a_daily)
        3. 最后尝试腾讯财经接口 (ak.stock_zh_a_hist_tx)
        """
        # 尝试列表：东财（默认）> 新浪 > 腾讯
        methods = [
            (self._fetch_stock_data_em, "东方财富"),
            (self._fetch_stock_data_sina, "新浪财经"),
            (self._fetch_stock_data_tx, "腾讯财经"),
        ]

        last_error = None

        for fetch_method, source_name in methods:
            try:
                logger.info(f"[数据源] 尝试使用 {source_name} 获取 {stock_code}...")
                df = fetch_method(stock_code, start_date, end_date)

                if df is not None and not df.empty:
                    logger.info(f"[数据源] {source_name} 获取成功")
                    return df
            except Exception as e:
                last_error = e
                logger.warning(f"[数据源] {source_name} 获取失败: {e}")
                # 继续尝试下一个

        # 所有都失败
        raise DataFetchError(f"Akshare 所有渠道获取失败: {last_error}")

    def _fetch_stock_data_em(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取普通 A 股历史数据 (东方财富)
        数据来源：ak.stock_zh_a_hist()
        """
        import akshare as ak

        self._throttle.wait()

        logger.info(f"[API调用] ak.stock_zh_a_hist(symbol={stock_code}, ...)")

        try:
            import time as _time
            api_start = _time.time()

            df = ak.stock_zh_a_hist(
                symbol=stock_code,
                period="daily",
                start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', ''),
                adjust="qfq"
            )

            api_elapsed = _time.time() - api_start

            if df is not None and not df.empty:
                logger.info(f"[API返回] ak.stock_zh_a_hist 成功: {len(df)} 行, 耗时 {api_elapsed:.2f}s")
                return df
            else:
                logger.warning(f"[API返回] ak.stock_zh_a_hist 返回空数据")
                return pd.DataFrame()

        except Exception as e:
            error_msg = str(e).lower()
            if any(keyword in error_msg for keyword in ['banned', 'blocked', '频率', 'rate', '限制']):
                raise RateLimitError(f"Akshare(EM) 可能被限流: {e}") from e
            raise e

    def _fetch_stock_data_sina(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取普通 A 股历史数据 (新浪财经)
        数据来源：ak.stock_zh_a_daily()
        """
        import akshare as ak

        # 转换代码格式：sh600000, sz000001, bj920748
        symbol = _to_sina_tx_symbol(stock_code)

        self._throttle.wait()

        df = ak.stock_zh_a_daily(
                symbol=symbol,
                start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', ''),
                adjust="qfq"
            )

        # 标准化新浪数据列名
        # 新浪返回：date, open, high, low, close, volume, amount, outstanding_share, turnover
        if df is not None and not df.empty:
            # 确保日期列存在
            if 'date' in df.columns:
                df = df.rename(columns={'date': '日期'})

            # 映射其他列以匹配 _normalize_data 的期望
            # _normalize_data 期望：日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 换手率
            rename_map = {
                'open': '开盘', 'high': '最高', 'low': '最低',
                'close': '收盘', 'volume': '成交量', 'amount': '成交额',
                'turnover': '换手率',
            }
            df = df.rename(columns=rename_map)

            # 新浪 turnover 列是小数比率（0.104325 = 10.43%），转为百分数
            if '换手率' in df.columns:
                df['换手率'] = df['换手率'] * 100

            # 计算涨跌幅（新浪接口可能不返回）
            if '收盘' in df.columns:
                df['涨跌幅'] = df['收盘'].pct_change() * 100
                df['涨跌幅'] = df['涨跌幅'].fillna(0)

            return df
        return pd.DataFrame()

    def _fetch_stock_data_tx(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取普通 A 股历史数据 (腾讯财经)
        数据来源：ak.stock_zh_a_hist_tx()
        """
        import akshare as ak

        # 转换代码格式：sh600000, sz000001, bj920748
        symbol = _to_sina_tx_symbol(stock_code)

        self._throttle.wait()

        df = ak.stock_zh_a_hist_tx(
                symbol=symbol,
                start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', ''),
                adjust="qfq"
            )

        # 标准化腾讯数据列名
        # 腾讯返回：date, open, close, high, low, volume, amount
        if df is not None and not df.empty:
            rename_map = {
                'date': '日期', 'open': '开盘', 'high': '最高',
                'low': '最低', 'close': '收盘', 'volume': '成交量',
                'amount': '成交额'
            }
            df = df.rename(columns=rename_map)

            # 腾讯数据通常包含 '涨跌幅'，如果没有则计算
            if 'pct_chg' in df.columns:
                df = df.rename(columns={'pct_chg': '涨跌幅'})
            elif '收盘' in df.columns:
                df['涨跌幅'] = df['收盘'].pct_change() * 100
                df['涨跌幅'] = df['涨跌幅'].fillna(0)

            return df
        return pd.DataFrame()

    def _fetch_etf_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取 ETF 基金历史数据
        
        数据来源：ak.fund_etf_hist_em()
        
        Args:
            stock_code: ETF 代码，如 '512400', '159883'
            start_date: 开始日期，格式 'YYYY-MM-DD'
            end_date: 结束日期，格式 'YYYY-MM-DD'
            
        Returns:
            ETF 历史数据 DataFrame
        """
        import akshare as ak
        
        self._throttle.wait()
        
        logger.info(f"[API调用] ak.fund_etf_hist_em(symbol={stock_code}, period=daily, "
                   f"start_date={start_date.replace('-', '')}, end_date={end_date.replace('-', '')}, adjust=qfq)")
        
        try:
            import time as _time
            api_start = _time.time()
            
            # 调用 akshare 获取 ETF 日线数据
            df = ak.fund_etf_hist_em(
                symbol=stock_code,
                period="daily",
                start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', ''),
                adjust="qfq"  # 前复权
            )
            
            api_elapsed = _time.time() - api_start
            
            # 记录返回数据摘要
            if df is not None and not df.empty:
                logger.info(f"[API返回] ak.fund_etf_hist_em 成功: 返回 {len(df)} 行数据, 耗时 {api_elapsed:.2f}s")
                logger.info(f"[API返回] 列名: {list(df.columns)}")
                logger.info(f"[API返回] 日期范围: {df['日期'].iloc[0]} ~ {df['日期'].iloc[-1]}")
                logger.debug(f"[API返回] 最新3条数据:\n{df.tail(3).to_string()}")
            else:
                logger.warning(f"[API返回] ak.fund_etf_hist_em 返回空数据, 耗时 {api_elapsed:.2f}s")
            
            return df
            
        except Exception as e:
            error_msg = str(e).lower()
            
            # 检测反爬封禁
            if any(keyword in error_msg for keyword in ['banned', 'blocked', '频率', 'rate', '限制']):
                logger.warning(f"检测到可能被封禁: {e}")
                raise RateLimitError(f"Akshare 可能被限流: {e}") from e
            
            raise DataFetchError(f"Akshare 获取 ETF 数据失败: {e}") from e

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化 Akshare 数据
        
        Akshare 返回的列名（中文）：
        日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
        
        需要映射到标准列名：
        date, open, high, low, close, volume, amount, pct_chg
        """
        df = df.copy()
        
        # 列名映射（Akshare 中文列名 -> 标准英文列名）
        column_mapping = {
            '日期': 'date',
            '开盘': 'open',
            '收盘': 'close',
            '最高': 'high',
            '最低': 'low',
            '成交量': 'volume',
            '成交额': 'amount',
            '涨跌幅': 'pct_chg',
            '换手率': 'turnover_rate',
        }
        
        # 重命名列
        df = df.rename(columns=column_mapping)
        
        # 添加股票代码列
        df['code'] = stock_code
        
        # 只保留需要的列
        keep_cols = ['code'] + STANDARD_COLUMNS
        existing_cols = [col for col in keep_cols if col in df.columns]
        df = df[existing_cols]
        
        return df
    
    def get_realtime_quote(self, stock_code: str, source: str = "em") -> Optional[UnifiedRealtimeQuote]:
        """
        获取实时行情数据（支持多数据源）

        数据源优先级（可配置）：
        1. em: 东方财富（akshare ak.stock_zh_a_spot_em）- 数据最全，含量比/PE/PB/市值等
        2. sina: 新浪财经（akshare ak.stock_zh_a_spot）- 轻量级，基本行情
        3. tencent: 腾讯直连接口 - 单股票查询，负载小

        Args:
            stock_code: 股票/ETF代码
            source: 数据源类型，可选 "em", "sina", "tencent"

        Returns:
            UnifiedRealtimeQuote 对象，获取失败返回 None
        """
        circuit_breaker = get_realtime_circuit_breaker()

        # 按代码类型与 source 参数路由：ETF 与股票同构（sina/tencent 直连腿
        # 对基金代码同样返回行情，见 Q3 诚实化修正——原来 ETF 无视 source 恒走东财）
        if is_us_stock_code(stock_code):
            # 字母 ticker：美股不在支持范围，直接拒收
            logger.debug(f"[API跳过] {stock_code} 是字母 ticker，Akshare 不支持（美股不在支持范围）")
            return None
        if source == "sina":
            source_key = "akshare_sina"
        elif source == "tencent":
            source_key = "akshare_tencent"
        else:
            source_key = "akshare_etf" if is_etf_code(stock_code) else "akshare_em"
        if not circuit_breaker.is_available(source_key):
            logger.warning(f"[熔断] 数据源 {source_key} 处于熔断状态，跳过")
            return None
        if source == "sina":
            return self._get_stock_realtime_quote_sina(stock_code)
        if source == "tencent":
            return self._get_stock_realtime_quote_tencent(stock_code)
        if is_etf_code(stock_code):
            return self._get_etf_realtime_quote(stock_code)
        return self._get_stock_realtime_quote_em(stock_code)
    
    def _get_stock_realtime_quote_em(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """
        获取普通 A 股实时行情数据（东方财富数据源）

        数据来源：ak.stock_zh_a_spot_em()
        优点：数据最全，含量比、换手率、市盈率、市净率、总市值、流通市值等
        缺点：全量拉取，数据量大，容易超时/限流

        装配（缓存/节流重试/熔断/哨兵/行定位）在 _snapshot_quote 单点；
        本方法只提供端点数据：抓取函数 + 列映射。
        """
        import akshare as ak
        return snapshot_realtime_quote(
            stock_code, source_key="akshare_em", source_label="东财(A股快照)",
            cache=_realtime_cache, throttle=self._throttle,
            fetch=ak.stock_zh_a_spot_em,
            row_builder=_build_em_stock_quote,
            code_columns="代码")
    
    def _get_stock_realtime_quote_sina(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """实时行情：新浪直连（单票请求，快而字段少）。

        装配（状态码/空载荷/引号段/字段数/熔断记账/失败消息）在 _direct_quote
        单点；本方法只提供端点数据与字段下标解析。
        """
        symbol = _to_sina_tx_symbol(stock_code)
        return direct_realtime_quote(
            stock_code, source_key="akshare_sina", source_name="新浪",
            endpoint=SINA_REALTIME_ENDPOINT, symbol=symbol,
            url=f"http://{SINA_REALTIME_ENDPOINT}={symbol}",
            headers=_direct_headers("http://finance.sina.com.cn"),
            delimiter=",", min_fields=32,
            parse=_parse_sina_fields,
            throttle=self._throttle)

    def _get_stock_realtime_quote_tencent(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """实时行情：腾讯直连（单票请求，含换手率）。

        装配同新浪腿，在 _direct_quote 单点；字段下标解析在 _parse_tencent_fields。
        """
        symbol = _to_sina_tx_symbol(stock_code)
        return direct_realtime_quote(
            stock_code, source_key="akshare_tencent", source_name="腾讯",
            endpoint=TENCENT_REALTIME_ENDPOINT, symbol=symbol,
            url=f"http://{TENCENT_REALTIME_ENDPOINT}={symbol}",
            headers=_direct_headers("http://finance.qq.com"),
            delimiter="~", min_fields=45,
            parse=_parse_tencent_fields,
            throttle=self._throttle)

    def _get_etf_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """
        获取 ETF 基金实时行情数据（东方财富全量快照 ak.fund_etf_spot_em）

        包含：最新价、涨跌幅、成交量、成交额、换手率等。
        装配在 _snapshot_quote 单点；ETF 列名与股票快照不同（开盘价/最高价/最低价），
        由 _build_em_etf_quote 承接。
        """
        import akshare as ak
        return snapshot_realtime_quote(
            stock_code, source_key="akshare_etf", source_label="东财(ETF快照)",
            cache=_etf_realtime_cache, throttle=self._throttle,
            fetch=ak.fund_etf_spot_em,
            row_builder=_build_em_etf_quote,
            code_columns="代码")
    
    def get_market_stats(self) -> Optional[Dict[str, Any]]:
        """
        获取市场涨跌统计

        数据源优先级：
        1. 东财接口 (ak.stock_zh_a_spot_em)
        2. 新浪接口 (ak.stock_zh_a_spot)
        """
        import akshare as ak

        # 优先东财接口
        try:
            self._throttle.wait()

            logger.info("[API调用] ak.stock_zh_a_spot_em() 获取市场统计...")
            df = ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                return calc_market_stats(df)
        except Exception as e:
            logger.warning(f"[Akshare] 东财接口获取市场统计失败: {e}，尝试新浪接口")

        # 东财失败后，尝试新浪接口
        try:
            self._throttle.wait()

            logger.info("[API调用] ak.stock_zh_a_spot() 获取市场统计(新浪)...")
            df = ak.stock_zh_a_spot()
            if df is not None and not df.empty:
                return calc_market_stats(df)
        except Exception as e:
            logger.error(f"[Akshare] 新浪接口获取市场统计也失败: {e}")

        return None


    def get_sector_pct_map(self) -> Dict[str, float]:
        """获取全量行业板块当日涨跌幅 {板块名: 涨跌幅%}。

        数据源优先级：
        1. 新浪接口 (ak.stock_sector_spot) —— 主机稳定，默认优先
        2. 东财接口 (ak.stock_board_industry_name_em) —— 板块名与个股所属板块同口径
        全部失败返回空字典，板块类规则由调用方跳过。
        """
        import akshare as ak

        def _to_map(df: pd.DataFrame, name_col: str, pct_col: str) -> Dict[str, float]:
            if df is None or df.empty or name_col not in df.columns or pct_col not in df.columns:
                return {}
            result: Dict[str, float] = {}
            for _, row in df.iterrows():
                pct = pd.to_numeric(row[pct_col], errors="coerce")
                if pd.notna(pct):
                    result[str(row[name_col])] = float(pct)
            return result

        try:
            self._throttle.wait()

            logger.info("[API调用] ak.stock_sector_spot() 获取全量板块行情(新浪)...")
            df = ak.stock_sector_spot(indicator='行业')
            result = _to_map(df, '板块', '涨跌幅')
            if result:
                return result

        except Exception as e:
            logger.warning(f"[Akshare] 新浪接口获取板块行情失败: {e}，尝试东财接口")

        try:
            self._throttle.wait()

            logger.info("[API调用] ak.stock_board_industry_name_em() 获取全量板块行情...")
            df = ak.stock_board_industry_name_em()
            return _to_map(df, '板块名称', '涨跌幅')

        except Exception as e:
            logger.error(f"[Akshare] 东财接口获取板块行情也失败: {e}")
            return {}
    def get_main_fund_flow(self, stock_code: str, days: int = 5) -> Optional[pd.DataFrame]:
        """
        获取主力资金流向数据

        Args:
            stock_code: 股票代码（如 '002357'）
            days: 获取天数（默认 5 个交易日）

        Returns:
            DataFrame 包含以下列：
            - date: 日期
            - main_net_inflow: 主力净流入（元，正数=净流入，负数=净流出）
            - 或 None（数据源不支持或获取失败）
        """
        import akshare as ak

        # 解析市场标识：akshare 内部 market_map = {sh:1, sz:0, bj:0}，
        # 深市与北交所最终是同一个 secid（0.xxxxxx），故 6 开头的走 sh、其余走 sz 即覆盖全市场。
        market = "sh" if stock_code.startswith("6") else "sz"

        try:
            self._throttle.wait()

            logger.info(f"[API调用] ak.stock_individual_fund_flow() 获取 {stock_code} 主力资金流...")
            df = ak.stock_individual_fund_flow(stock=stock_code, market=market)

            if df is None or df.empty:
                logger.debug(f"[Akshare] {stock_code} 主力资金流数据为空")
                return None

            # 标准化列名
            # 注意：东财接口同时返回「主力净流入-净额」与「主力净流入-净占比」，
            # 两者都含"主力/净流入"关键词，必须排除占比列，否则会重名成两列
            # main_net_inflow，调用方取列得到 DataFrame 而非 Series。
            column_mapping = {}
            for col in df.columns:
                col_str = str(col).strip()
                if '日期' in col_str or 'date' in col_str.lower():
                    column_mapping[col] = 'date'
                elif '主力' in col_str and '净流入' in col_str and '占比' not in col_str:
                    column_mapping[col] = 'main_net_inflow'

            df = df.rename(columns=column_mapping)

            # 确保必需的列存在
            if 'date' not in df.columns or 'main_net_inflow' not in df.columns:
                logger.debug(f"[Akshare] {stock_code} 主力资金流数据格式异常: {df.columns.tolist()}")
                return None

            # 转换数据类型
            df['date'] = pd.to_datetime(df['date'])
            df['main_net_inflow'] = pd.to_numeric(df['main_net_inflow'], errors='coerce')

            # 单位：东财 fflow/daykline 的 f52 原始即为「元」，akshare 原样透传，不做换算

            # 取最近 N 天，按日期升序返回
            df = df.sort_values('date', ascending=False).head(days).sort_values('date').reset_index(drop=True)

            logger.info(f"[Akshare] {stock_code} 获取主力资金流成功: {len(df)} 天")
            return df[['date', 'main_net_inflow']]

        except Exception as e:
            logger.debug(f"[Akshare] {stock_code} 获取主力资金流失败: {e}")
            return None
