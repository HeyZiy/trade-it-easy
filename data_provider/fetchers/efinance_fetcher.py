# -*- coding: utf-8 -*-
"""
===================================
EfinanceFetcher - 优先数据源 (Priority 0)
===================================

数据来源：东方财富爬虫（通过 efinance 库）
特点：免费、无需 Token、数据全面、API 简洁
仓库：https://github.com/Micro-sheep/efinance

与 AkshareFetcher 类似，但 efinance 库：
1. API 更简洁易用
2. 支持批量获取数据
3. 更稳定的接口封装

防封禁策略：
1. Throttle 自适应节流：请求间隔补足 + 随机休眠 1.5-3.0 秒 + 连续错误退避
2. 使用 tenacity 实现指数退避重试
3. 熔断器机制：连续失败后自动冷却
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Optional, Dict, Any, Tuple

import pandas as pd
import requests  # 引入 requests 以捕获异常
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from data_provider._crosscut import Throttle, TtlSnapshotCache, classify_http_error

# Timeout (seconds) for efinance library calls that go through eastmoney APIs
# with no built-in timeout.  Prevents indefinite hangs when hosts are unreachable.
try:
    _EF_CALL_TIMEOUT = int(os.environ.get("EFINANCE_CALL_TIMEOUT", "30"))
except (ValueError, TypeError):
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "EFINANCE_CALL_TIMEOUT is not a valid integer, using default 30s"
    )
    _EF_CALL_TIMEOUT = 30

# Connection pool settings for improved stability
try:
    _EF_MAX_RETRIES = int(os.environ.get("EFINANCE_MAX_RETRIES", "3"))
except (ValueError, TypeError):
    _EF_MAX_RETRIES = 3

try:
    _EF_RETRY_MIN_WAIT = int(os.environ.get("EFINANCE_RETRY_MIN_WAIT", "2"))
except (ValueError, TypeError):
    _EF_RETRY_MIN_WAIT = 2

try:
    _EF_RETRY_MAX_WAIT = int(os.environ.get("EFINANCE_RETRY_MAX_WAIT", "30"))
except (ValueError, TypeError):
    _EF_RETRY_MAX_WAIT = 30

# Global session with connection pooling for better stability
_session: Optional[requests.Session] = None
_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    """Get or create a global requests session with connection pooling.
    
    Using a shared session enables HTTP keep-alive and connection reuse,
    which significantly reduces RemoteDisconnected errors when making
    multiple requests to the same host.
    """
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                _session = requests.Session()
                # Configure connection pooling
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=10,      # Number of connection pools to cache
                    pool_maxsize=20,          # Max connections to save in the pool
                    max_retries=requests.adapters.Retry(
                        total=2,              # Total retries for connection errors
                        backoff_factor=0.5,   # Backoff between retries
                        status_forcelist=[500, 502, 503, 504],  # Retry on these status codes
                    ),
                )
                _session.mount('https://', adapter)
                _session.mount('http://', adapter)
    return _session


from data_provider.fetchers.base import BaseFetcher
from data_provider.fetchers._snapshot_quote import snapshot_realtime_quote
from data_provider.stats import calc_market_stats
from data_provider.types import (
    KIND_BELONG_BOARD, KIND_SECTOR_QUOTE, KIND_STOCK_DAILY,
    DataFetchError, RateLimitError, STANDARD_COLUMNS,
    UnifiedRealtimeQuote,
    get_realtime_circuit_breaker, safe_float, safe_int,
)
from data_provider.codes import is_bse_code, is_st_stock, is_kc_cy_stock, normalize_stock_code, is_etf_code, is_us_stock_code


logger = logging.getLogger(__name__)

EASTMONEY_HISTORY_ENDPOINT = "push2his.eastmoney.com/api/qt/stock/kline/get"


# 全量快照缓存（TtlSnapshotCache，见 data_provider/_crosscut.py）
# TTL 设为 10 分钟 (600秒)：批量分析场景下避免重复拉取
_realtime_cache = TtlSnapshotCache(ttl=600, label="实时行情(efinance)")
_etf_realtime_cache = TtlSnapshotCache(ttl=600, label="ETF实时行情(efinance)")


# 列名映射：efinance 可能返回中英两种列名（中英别名并列）
_EFINANCE_COLS = {
    '股票名称': 'name', 'name': 'name',
    '最新价': 'price', 'price': 'price',
    '涨跌幅': 'change_pct', 'change_pct': 'change_pct',
    '涨跌额': 'change_amount', 'change_amount': 'change_amount',
    '成交量': 'volume', 'volume': 'volume',
    '成交额': 'amount', 'amount': 'amount',
    '换手率': 'turnover_rate', 'turnover_rate': 'turnover_rate',
    '振幅': 'amplitude', 'amplitude': 'amplitude',
    '最高': 'high', 'high': 'high',
    '最低': 'low', 'low': 'low',
    '开盘': 'open_price', 'open': 'open_price',
    '量比': 'volume_ratio', 'volume_ratio': 'volume_ratio',
    '市盈率': 'pe_ratio', 'pe_ratio': 'pe_ratio',
    '总市值': 'total_mv', 'total_mv': 'total_mv',
    '流通市值': 'circ_mv', 'circ_mv': 'circ_mv',
}

# ETF 快照列较少：无量比/估值
_EFINANCE_ETF_COLS = {
    '股票名称': 'name', 'name': 'name',
    '最新价': 'price', 'price': 'price',
    '涨跌幅': 'change_pct', 'change_pct': 'change_pct',
    '涨跌额': 'change_amount', 'change_amount': 'change_amount',
    '成交量': 'volume', 'volume': 'volume',
    '成交额': 'amount', 'amount': 'amount',
    '换手率': 'turnover_rate', 'turnover_rate': 'turnover_rate',
    '振幅': 'amplitude', 'amplitude': 'amplitude',
    '最高': 'high', 'high': 'high',
    '最低': 'low', 'low': 'low',
    '开盘': 'open_price', 'open': 'open_price',
}


def _build_efinance_quote(row: pd.Series, stock_code: str) -> UnifiedRealtimeQuote:
    """efinance A 股快照一行 → 统一报价（列映射为该端点本地数据）。"""
    quote = UnifiedRealtimeQuote(
        code=stock_code,
        name=str(row.get(_EFINANCE_COLS['股票名称'], '')),
        source="efinance",
        price=safe_float(row.get(_EFINANCE_COLS['最新价'])),
        change_pct=safe_float(row.get(_EFINANCE_COLS['涨跌幅'])),
        change_amount=safe_float(row.get(_EFINANCE_COLS['涨跌额'])),
        volume=safe_int(row.get(_EFINANCE_COLS['成交量'])),
        amount=safe_float(row.get(_EFINANCE_COLS['成交额'])),
        turnover_rate=safe_float(row.get(_EFINANCE_COLS['换手率'])),
        amplitude=safe_float(row.get(_EFINANCE_COLS['振幅'])),
        high=safe_float(row.get(_EFINANCE_COLS['最高'])),
        low=safe_float(row.get(_EFINANCE_COLS['最低'])),
        open_price=safe_float(row.get(_EFINANCE_COLS['开盘'])),
        volume_ratio=safe_float(row.get(_EFINANCE_COLS['量比'])),
        pe_ratio=safe_float(row.get(_EFINANCE_COLS['市盈率'])),
        total_mv=safe_float(row.get(_EFINANCE_COLS['总市值'])),
        circ_mv=safe_float(row.get(_EFINANCE_COLS['流通市值'])),
    )
    logger.info(f"[实时行情-efinance] {stock_code} {quote.name}: 价格={quote.price}, "
                f"涨跌={quote.change_pct}%, 量比={quote.volume_ratio}, "
                f"换手率={quote.turnover_rate}%")
    return quote


def _build_efinance_etf_quote(row: pd.Series, stock_code: str) -> UnifiedRealtimeQuote:
    """efinance ETF 快照一行 → 统一报价（列名同股票但缺量比/估值列）。"""
    quote = UnifiedRealtimeQuote(
        code=stock_code,
        name=str(row.get(_EFINANCE_ETF_COLS['股票名称'], '')),
        source="efinance",
        price=safe_float(row.get(_EFINANCE_ETF_COLS['最新价'])),
        change_pct=safe_float(row.get(_EFINANCE_ETF_COLS['涨跌幅'])),
        change_amount=safe_float(row.get(_EFINANCE_ETF_COLS['涨跌额'])),
        volume=safe_int(row.get(_EFINANCE_ETF_COLS['成交量'])),
        amount=safe_float(row.get(_EFINANCE_ETF_COLS['成交额'])),
        turnover_rate=safe_float(row.get(_EFINANCE_ETF_COLS['换手率'])),
        amplitude=safe_float(row.get(_EFINANCE_ETF_COLS['振幅'])),
        high=safe_float(row.get(_EFINANCE_ETF_COLS['最高'])),
        low=safe_float(row.get(_EFINANCE_ETF_COLS['最低'])),
        open_price=safe_float(row.get(_EFINANCE_ETF_COLS['开盘'])),
    )
    logger.info(f"[ETF实时行情-efinance] {stock_code} {quote.name}: "
                f"价格={quote.price}, 涨跌={quote.change_pct}%, 换手率={quote.turnover_rate}%")
    return quote


def _ef_call_with_timeout(func, *args, timeout=None, **kwargs):
    """Run an efinance library call in a thread with a timeout.

    efinance internally uses requests/urllib3 with no timeout, so when
    eastmoney hosts are unreachable the call can hang for many minutes.
    This helper caps the *calling thread's* wait time.  Note: Python threads
    cannot be forcibly killed, so the worker thread may continue running in
    the background until the OS-level TCP timeout fires or the process exits.
    This is acceptable — the calling thread returns promptly on timeout.
    """
    if timeout is None:
        timeout = _EF_CALL_TIMEOUT
    # Do NOT use 'with ThreadPoolExecutor(...)' here: the context manager calls
    # shutdown(wait=True) on __exit__, which would re-block on the hung thread.
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(func, *args, **kwargs)
        return future.result(timeout=timeout)
    finally:
        # wait=False: calling thread returns immediately; worker cleans up later
        executor.shutdown(wait=False)


class EfinanceFetcher(BaseFetcher):
    """
    Efinance 数据源实现
    
    优先级：0（最高，优先于 AkshareFetcher）
    数据来源：东方财富网（通过 efinance 库封装）
    仓库：https://github.com/Micro-sheep/efinance
    
    主要 API：
    - ef.stock.get_quote_history(): 获取历史 K 线数据
    - ef.stock.get_base_info(): 获取股票基本信息
    - ef.stock.get_realtime_quotes(): 获取实时行情
    
    关键策略：
    - Throttle 自适应节流：间隔补足 + 随机休眠 1.5-3.0 秒 + 连续错误退避
    - 失败后指数退避重试（最多3次）
    """
    
    name = "EfinanceFetcher"
    # 降低优先级，让 AkshareFetcher 优先（Akshare 更稳定，支持新浪财经备选）
    priority = int(os.getenv("EFINANCE_PRIORITY", "1"))
    # 东财 K 线含换手率列
    SUPPORTS_COLUMNS = {'date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg', 'turnover_rate'}

    # 日线均仅 A 股（美股明确拒绝，见 _fetch_raw_data）；板块行情/所属板块亦为 A 股口径
    SUPPORTS = frozenset({
        (KIND_STOCK_DAILY, "cn"),
        (KIND_SECTOR_QUOTE, "cn"), (KIND_BELONG_BOARD, "cn"),
    })

    # 实时端点自声明（源名唯一权威，merge_realtime_quotes 按此建索引）
    REALTIME_VARIANTS = {"efinance": {}}
    
    def __init__(self, sleep_min: float = 1.5, sleep_max: float = 3.0):
        """
        初始化 EfinanceFetcher
        
        Args:
            sleep_min: 节流最小休眠时间（秒）
            sleep_max: 节流最大休眠时间（秒）
        """
        self._throttle = Throttle(sleep_min, sleep_max, adaptive=True)

    @staticmethod
    def _build_history_failure_message(
        stock_code: str,
        beg_date: str,
        end_date: str,
        exc: Exception,
        elapsed: float,
        is_etf: bool = False,
    ) -> Tuple[str, str]:
        category, detail = classify_http_error(exc)
        instrument_type = "ETF" if is_etf else "stock"
        message = (
            "Eastmoney 历史K线接口失败: "
            f"endpoint={EASTMONEY_HISTORY_ENDPOINT}, stock_code={stock_code}, "
            f"market_type={instrument_type}, range={beg_date}~{end_date}, "
            f"category={category}, error_type={type(exc).__name__}, elapsed={elapsed:.2f}s, detail={detail}"
        )
        return category, message

    @retry(
        stop=stop_after_attempt(_EF_MAX_RETRIES),  # 使用环境变量配置，默认3次
        wait=wait_exponential(multiplier=1, min=_EF_RETRY_MIN_WAIT, max=_EF_RETRY_MAX_WAIT),
        retry=retry_if_exception_type((
            ConnectionError,
            TimeoutError,
            requests.exceptions.RequestException,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.HTTPError,
        )),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,  # 重试耗尽后抛出原始异常
    )
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 efinance 获取原始数据
        
        根据代码类型自动选择 API：
        - 字母 ticker（美股不在支持范围）：不支持，抛出异常
        - 普通股票：使用 ef.stock.get_quote_history()
        - ETF 基金：使用 ef.stock.get_quote_history()（ETF 是交易所证券，使用股票 K 线接口）
        
        流程：
        1. 判断代码类型（字母 ticker/股票/ETF）
        2. 节流休眠（Throttle.wait）
        3. 调用对应的 efinance API
        4. 处理返回数据
        """
        # 字母 ticker：美股不在支持范围，直接拒收
        if is_us_stock_code(stock_code):
            raise DataFetchError(f"EfinanceFetcher 不支持字母 ticker {stock_code}（美股不在支持范围）")
        
        # 根据代码类型选择不同的获取方法
        if is_etf_code(stock_code):
            return self._fetch_etf_data(stock_code, start_date, end_date)
        else:
            return self._fetch_stock_data(stock_code, start_date, end_date)
    
    def _fetch_stock_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取普通 A 股历史数据
        
        数据来源：ef.stock.get_quote_history()
        
        API 参数说明：
        - stock_codes: 股票代码
        - beg: 开始日期，格式 'YYYYMMDD'
        - end: 结束日期，格式 'YYYYMMDD'
        - klt: 周期，101=日线
        - fqt: 复权方式，1=前复权
        """
        import efinance as ef
        
        self._throttle.wait()
        
        # 格式化日期（efinance 使用 YYYYMMDD 格式）
        beg_date = start_date.replace('-', '')
        end_date_fmt = end_date.replace('-', '')
        
        logger.info(f"[API调用] ef.stock.get_quote_history(stock_codes={stock_code}, "
                   f"beg={beg_date}, end={end_date_fmt}, klt=101, fqt=1)")
        
        api_start = time.time()
        try:
            # 调用 efinance 获取 A 股日线数据
            # klt=101 获取日线数据
            # fqt=1 获取前复权数据
            df = _ef_call_with_timeout(
                ef.stock.get_quote_history,
                stock_codes=stock_code,
                beg=beg_date,
                end=end_date_fmt,
                klt=101,  # 日线
                fqt=1,    # 前复权
                timeout=60,
            )
            
            api_elapsed = time.time() - api_start
            
            # 记录返回数据摘要
            if df is not None and not df.empty:
                logger.info(
                    "[API返回] Eastmoney 历史K线成功: "
                    f"endpoint={EASTMONEY_HISTORY_ENDPOINT}, stock_code={stock_code}, "
                    f"range={beg_date}~{end_date_fmt}, rows={len(df)}, elapsed={api_elapsed:.2f}s"
                )
                logger.info(f"[API返回] 列名: {list(df.columns)}")
                if '日期' in df.columns:
                    logger.info(f"[API返回] 日期范围: {df['日期'].iloc[0]} ~ {df['日期'].iloc[-1]}")
                logger.debug(f"[API返回] 最新3条数据:\n{df.tail(3).to_string()}")
                # 记录成功，减少错误计数
                self._throttle.record_success()
            else:
                logger.warning(
                    "[API返回] Eastmoney 历史K线为空: "
                    f"endpoint={EASTMONEY_HISTORY_ENDPOINT}, stock_code={stock_code}, "
                    f"range={beg_date}~{end_date_fmt}, elapsed={api_elapsed:.2f}s"
                )
            
            return df
            
        except Exception as e:
            api_elapsed = time.time() - api_start
            # 记录错误，用于自适应流控
            self._throttle.record_error()
            category, failure_message = self._build_history_failure_message(
                stock_code=stock_code,
                beg_date=beg_date,
                end_date=end_date_fmt,
                exc=e,
                elapsed=api_elapsed,
            )

            if category == "rate_limit_or_anti_bot":
                logger.warning(failure_message)
                raise RateLimitError(f"efinance 可能被限流: {failure_message}") from e

            logger.error(failure_message)
            raise DataFetchError(f"efinance 获取数据失败: {failure_message}") from e
    
    def _fetch_etf_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取 ETF 基金历史数据

        Exchange-traded ETFs have OHLCV data just like regular stocks, so we use
        ef.stock.get_quote_history (the stock K-line API) which returns full
        open/high/low/close/volume data.

        Previously this method used ef.fund.get_quote_history which only returns
        NAV data (单位净值/累计净值) without volume or OHLC, causing:
        - Issue #541: 'got an unexpected keyword argument beg'
        - Issue #527: ETF volume/turnover always showing 0

        Args:
            stock_code: ETF code, e.g. '512400', '159883', '515120'
            start_date: Start date, format 'YYYY-MM-DD'
            end_date: End date, format 'YYYY-MM-DD'

        Returns:
            ETF historical OHLCV DataFrame
        """
        import efinance as ef

        self._throttle.wait()

        # Format dates (efinance uses YYYYMMDD)
        beg_date = start_date.replace('-', '')
        end_date_fmt = end_date.replace('-', '')

        logger.info(f"[API调用] ef.stock.get_quote_history(stock_codes={stock_code}, "
                     f"beg={beg_date}, end={end_date_fmt}, klt=101, fqt=1)  [ETF]")

        api_start = time.time()
        try:
            # ETFs are exchange-traded securities; use the stock API to get full OHLCV data
            df = _ef_call_with_timeout(
                ef.stock.get_quote_history,
                stock_codes=stock_code,
                beg=beg_date,
                end=end_date_fmt,
                klt=101,  # daily
                fqt=1,    # forward-adjusted
                timeout=60,
            )

            api_elapsed = time.time() - api_start

            if df is not None and not df.empty:
                logger.info(
                    "[API返回] Eastmoney 历史K线成功 [ETF]: "
                    f"endpoint={EASTMONEY_HISTORY_ENDPOINT}, stock_code={stock_code}, "
                    f"range={beg_date}~{end_date_fmt}, rows={len(df)}, elapsed={api_elapsed:.2f}s"
                )
                logger.info(f"[API返回] 列名: {list(df.columns)}")
                if '日期' in df.columns:
                    logger.info(f"[API返回] 日期范围: {df['日期'].iloc[0]} ~ {df['日期'].iloc[-1]}")
                logger.debug(f"[API返回] 最新3条数据:\n{df.tail(3).to_string()}")
            else:
                logger.warning(
                    "[API返回] Eastmoney 历史K线为空 [ETF]: "
                    f"endpoint={EASTMONEY_HISTORY_ENDPOINT}, stock_code={stock_code}, "
                    f"range={beg_date}~{end_date_fmt}, elapsed={api_elapsed:.2f}s"
                )

            return df

        except Exception as e:
            api_elapsed = time.time() - api_start
            category, failure_message = self._build_history_failure_message(
                stock_code=stock_code,
                beg_date=beg_date,
                end_date=end_date_fmt,
                exc=e,
                elapsed=api_elapsed,
                is_etf=True,
            )

            if category == "rate_limit_or_anti_bot":
                logger.warning(failure_message)
                raise RateLimitError(f"efinance 可能被限流: {failure_message}") from e

            logger.error(failure_message)
            raise DataFetchError(f"efinance 获取 ETF 数据失败: {failure_message}") from e
    
    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化 efinance 数据
        
        efinance 返回的列名（中文）：
        股票名称, 股票代码, 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
        
        需要映射到标准列名：
        date, open, high, low, close, volume, amount, pct_chg
        """
        df = df.copy()
        
        # Column mapping (efinance Chinese column names -> standard English column names)
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
            '股票代码': 'code',
            '股票名称': 'name',
        }
        
        # 重命名列
        df = df.rename(columns=column_mapping)
        
        # Fallback: if OHLC columns are missing (e.g. very old data path), fill from close
        if 'close' in df.columns and 'open' not in df.columns:
            df['open'] = df['close']
            df['high'] = df['close']
            df['low'] = df['close']
            
        # Fill volume and amount if missing
        if 'volume' not in df.columns:
            df['volume'] = 0
        if 'amount' not in df.columns:
            df['amount'] = 0

        
        # 如果没有 code 列，手动添加
        if 'code' not in df.columns:
            df['code'] = stock_code
        
        # 只保留需要的列
        keep_cols = ['code'] + STANDARD_COLUMNS
        existing_cols = [col for col in keep_cols if col in df.columns]
        df = df[existing_cols]
        
        return df
    
    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """
        获取实时行情数据
        
        数据来源：ef.stock.get_realtime_quotes()
        ETF 数据源：ef.stock.get_realtime_quotes(['ETF'])
        
        Args:
            stock_code: 股票代码
            
        Returns:
            UnifiedRealtimeQuote 对象，获取失败返回 None
        """
        # ETF 需要单独请求 ETF 实时行情接口
        if is_etf_code(stock_code):
            return self._get_etf_realtime_quote(stock_code)

        import efinance as ef
        # 装配（缓存/节流重试/熔断/哨兵/行定位）在 _snapshot_quote 单点；
        # 本方法只提供端点数据：抓取函数 + 列映射。
        return snapshot_realtime_quote(
            stock_code, source_key="efinance", source_label="efinance(A股快照)",
            cache=_realtime_cache, throttle=self._throttle,
            fetch=lambda: _ef_call_with_timeout(ef.stock.get_realtime_quotes),
            row_builder=_build_efinance_quote,
            code_columns=("股票代码", "code"))

    def _get_etf_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """ETF 实时行情：efinance 默认实时接口仅返回股票，需显式 ['ETF']。

        装配在 _snapshot_quote 单点；行定位补零 6 位（ETF 表 code 列为 6 位字符串）。
        """
        import efinance as ef
        return snapshot_realtime_quote(
            stock_code, source_key="efinance_etf", source_label="efinance(ETF快照)",
            cache=_etf_realtime_cache, throttle=self._throttle,
            fetch=lambda: _ef_call_with_timeout(ef.stock.get_realtime_quotes, ['ETF']),
            row_builder=_build_efinance_etf_quote,
            code_columns="股票代码", code_zfill=6)

    def get_market_stats(self) -> Optional[Dict[str, Any]]:
        """
        获取市场涨跌统计 (efinance)
        """
        import efinance as ef

        try:
            self._throttle.wait()

            current_time = time.time()
            df = _realtime_cache.get(current_time)
            if df is None:
                logger.info("[API调用] ef.stock.get_realtime_quotes() 获取市场统计...")
                df = _ef_call_with_timeout(ef.stock.get_realtime_quotes)
                _realtime_cache.store(df, current_time)

            if df is None or df.empty:
                logger.warning("[API返回] 市场统计数据为空")
                return None

            return calc_market_stats(df)
        except Exception as e:
            logger.error(f"[efinance] 获取市场统计失败: {e}")
            return None
        

    def get_sector_quotes(self) -> Optional[pd.DataFrame]:
        """获取全量行业板块实时行情（东财），含板块名称/涨跌幅列。

        供卖出规则做"板块明显走弱/主线退潮"判断，比涨跌榜（top/bottom n）
        覆盖面更全。失败返回 None。
        """
        import efinance as ef

        try:
            self._throttle.wait()

            logger.info("[API调用] ef.stock.get_realtime_quotes(['行业板块']) 获取板块行情...")
            df = _ef_call_with_timeout(ef.stock.get_realtime_quotes, ['行业板块'])
            if df is None or df.empty:
                logger.warning("[efinance] 板块行情数据为空")
                return None
            return df
        except Exception as e:
            logger.error(f"[efinance] 获取全量板块行情失败: {e}")
            return None

    def get_sector_pct_map(self) -> Optional[Dict[str, float]]:
        """全量行业板块当日涨跌幅 {板块名: 涨跌幅%}（东财实时）。失败返回 None。"""
        df = self.get_sector_quotes()
        if df is None or df.empty:
            return None
        name_col = "股票名称" if "股票名称" in df.columns else "name"
        pct_col = "涨跌幅" if "涨跌幅" in df.columns else "pct_chg"
        if name_col not in df.columns or pct_col not in df.columns:
            logger.warning(f"[efinance] 板块行情缺少名称/涨跌幅列: {list(df.columns)}")
            return None
        result: Dict[str, float] = {}
        for _, row in df.iterrows():
            pct = pd.to_numeric(row[pct_col], errors="coerce")
            if pd.notna(pct):
                result[str(row[name_col])] = float(pct)
        return result or None

    def get_base_info(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取股票基本信息
        
        数据来源：ef.stock.get_base_info()
        包含：市盈率、市净率、所处行业、总市值、流通市值、ROE、净利率等
        
        Args:
            stock_code: 股票代码
            
        Returns:
            包含基本信息的字典，获取失败返回 None
        """
        import efinance as ef
        
        try:
            # 防封禁：节流
            self._throttle.wait()
            
            logger.info(f"[API调用] ef.stock.get_base_info(stock_codes={stock_code}) 获取基本信息...")
            import time as _time
            api_start = _time.time()
            
            info = _ef_call_with_timeout(ef.stock.get_base_info, stock_code)
            
            api_elapsed = _time.time() - api_start
            logger.info(f"[API返回] ef.stock.get_base_info 成功, 耗时 {api_elapsed:.2f}s")
            
            if info is None:
                logger.warning(f"[API返回] 未获取到 {stock_code} 的基本信息")
                return None
            
            # 转换为字典
            if isinstance(info, pd.Series):
                return info.to_dict()
            elif isinstance(info, pd.DataFrame):
                if not info.empty:
                    return info.iloc[0].to_dict()
            
            return None
            
        except Exception as e:
            logger.error(f"[API错误] 获取 {stock_code} 基本信息失败: {e}")
            return None
    
    def get_belong_board(self, stock_code: str) -> Optional[pd.DataFrame]:
        """
        获取股票所属板块
        
        数据来源：ef.stock.get_belong_board()
        
        Args:
            stock_code: 股票代码
            
        Returns:
            所属板块 DataFrame，获取失败返回 None
        """
        import efinance as ef
        
        try:
            # 防封禁：节流
            self._throttle.wait()
            
            logger.info(f"[API调用] ef.stock.get_belong_board(stock_code={stock_code}) 获取所属板块...")
            import time as _time
            api_start = _time.time()
            
            df = _ef_call_with_timeout(ef.stock.get_belong_board, stock_code)
            
            api_elapsed = _time.time() - api_start
            
            if df is not None and not df.empty:
                logger.info(f"[API返回] ef.stock.get_belong_board 成功: 返回 {len(df)} 个板块, 耗时 {api_elapsed:.2f}s")
                return df
            else:
                logger.warning(f"[API返回] 未获取到 {stock_code} 的板块信息")
                return None
            
        except FuturesTimeoutError:
            logger.warning(f"[超时] ef.stock.get_belong_board({stock_code}) 超过 {_EF_CALL_TIMEOUT}s，跳过")
            return None
        except Exception as e:
            logger.error(f"[API错误] 获取 {stock_code} 所属板块失败: {e}")
            return None

    def get_belong_board_name(self, stock_code: str) -> Optional[str]:
        """个股所属行业板块名称（取首个板块）。失败/缺失返回 None。"""
        df = self.get_belong_board(stock_code)
        if df is not None and not df.empty and "板块名称" in df.columns:
            sector = str(df["板块名称"].iloc[0]).strip()
            if sector:
                return sector
        return None
    
    def get_enhanced_data(self, stock_code: str, days: int = 60) -> Dict[str, Any]:
        """
        获取增强数据（历史K线 + 实时行情 + 基本信息）
        
        Args:
            stock_code: 股票代码
            days: 历史数据天数
            
        Returns:
            包含所有数据的字典
        """
        result = {
            'code': stock_code,
            'daily_data': None,
            'realtime_quote': None,
            'base_info': None,
            'belong_board': None,
        }
        
        # 获取日线数据
        try:
            df = self.get_daily_data(stock_code, days=days)
            result['daily_data'] = df
        except Exception as e:
            logger.error(f"获取 {stock_code} 日线数据失败: {e}")
        
        # 获取实时行情
        result['realtime_quote'] = self.get_realtime_quote(stock_code)
        
        # 获取基本信息
        result['base_info'] = self.get_base_info(stock_code)
        
        # 获取所属板块
        result['belong_board'] = self.get_belong_board(stock_code)
        
        return result


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    fetcher = EfinanceFetcher()
    
    # 测试普通股票
    print("=" * 50)
    print("测试普通股票数据获取 (efinance)")
    print("=" * 50)
    try:
        df = fetcher.get_daily_data('600519')  # 茅台
        print(f"[股票] 获取成功，共 {len(df)} 条数据")
        print(df.tail())
    except Exception as e:
        print(f"[股票] 获取失败: {e}")
    
    # 测试 ETF 基金
    print("\n" + "=" * 50)
    print("测试 ETF 基金数据获取 (efinance)")
    print("=" * 50)
    try:
        df = fetcher.get_daily_data('512400')  # 有色龙头ETF
        print(f"[ETF] 获取成功，共 {len(df)} 条数据")
        print(df.tail())
    except Exception as e:
        print(f"[ETF] 获取失败: {e}")
    
    # 测试实时行情
    print("\n" + "=" * 50)
    print("测试实时行情获取 (efinance)")
    print("=" * 50)
    try:
        quote = fetcher.get_realtime_quote('600519')
        if quote:
            print(f"[实时行情] {quote.name}: 价格={quote.price}, 涨跌幅={quote.change_pct}%")
        else:
            print("[实时行情] 未获取到数据")
    except Exception as e:
        print(f"[实时行情] 获取失败: {e}")
    
    # 测试基本信息
    print("\n" + "=" * 50)
    print("测试基本信息获取 (efinance)")
    print("=" * 50)
    try:
        info = fetcher.get_base_info('600519')
        if info:
            print(f"[基本信息] 市盈率={info.get('市盈率(动)', 'N/A')}, 市净率={info.get('市净率', 'N/A')}")
        else:
            print("[基本信息] 未获取到数据")
    except Exception as e:
        print(f"[基本信息] 获取失败: {e}")

    # 测试市场统计 
    print("\n" + "=" * 50)
    print("Testing get_market_stats (efinance)")
    print("=" * 50)
    try:
        stats = fetcher.get_market_stats()
        if stats:
            print(f"Market Stats successfully computed:")
            print(f"Up: {stats['up_count']} (Limit Up: {stats['limit_up_count']})")
            print(f"Down: {stats['down_count']} (Limit Down: {stats['limit_down_count']})")
            print(f"Flat: {stats['flat_count']}")
            print(f"Total Amount: {stats['total_amount']:.2f} 亿 (Yi)")
        else:
            print("Failed to compute market stats.")
    except Exception as e:
        print(f"Failed to compute market stats: {e}")
