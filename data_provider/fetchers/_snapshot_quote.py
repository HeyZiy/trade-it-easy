# -*- coding: utf-8 -*-
"""
===================================
快照型实时行情 — 公共装配（内部 seam）
===================================

akshare 与 efinance 的全量快照端点共用一个形状：
全量拉取 → TtlSnapshotCache → 未命中时节流重试 → 熔断记账 →
失败存哨兵空表（TTL 内不再请求）→ 按 code 列定位行 → 端点各自
的列映射构建 UnifiedRealtimeQuote。

机制归本模块（缓存/节流/重试/熔断/哨兵/行定位），数据归端点：
- cache / throttle / fetch / circuit_breaker 全部注入，测试整层假数据；
- row_builder 是端点唯一的本地逻辑：一行快照 + 代码 → 报价；
- 列映射、code 列名、补零规则、来源标签由调用方给出。

约定：抓取成功即记熔断成功（空结果也是成功的空市场，存哨兵）；
重试耗尽记失败并存哨兵——TTL 内同端点不再反复请求。
"""

import logging
import time
from typing import Callable, Optional, Tuple, Union

import pandas as pd

from data_provider._crosscut import Throttle, TtlSnapshotCache, classify_http_error  # noqa: F401  (re-export)
from data_provider.types import UnifiedRealtimeQuote, get_realtime_circuit_breaker

logger = logging.getLogger(__name__)


def snapshot_realtime_quote(
    stock_code: str,
    *,
    source_key: str,
    source_label: str,
    cache: TtlSnapshotCache,
    throttle: Throttle,
    fetch: Callable[[], pd.DataFrame],
    row_builder: Callable[[pd.Series, str], UnifiedRealtimeQuote],
    code_columns: Union[str, Tuple[str, ...]],
    code_zfill: int = 0,
    attempts: int = 2,
    backoff: Optional[Callable[[int], float]] = None,
    circuit_breaker=None,
) -> Optional[UnifiedRealtimeQuote]:
    """快照端点取单票实时行情；任何失败路径返回 None（fail-soft，调用方走 failover）。

    stock_code     查询代码（6 位）
    source_key     熔断键（端点自声明）
    source_label   日志/错误信息里的端点名
    cache          端点快照缓存（全量表，Ttl 内共享）
    throttle       请求节流（每次外呼前 wait）
    fetch          全量拉取 callable（端点 SDK 调用，可含超时包装）
    row_builder    (快照行, 6 位代码) → UnifiedRealtimeQuote（端点列映射）
    code_columns   行定位的 code 列名（依次尝试表内存在的列）
    code_zfill     行定位补零位数（如 ETF 表 6 位补零；0 = 不补）
    attempts       全量刷新重试次数（耗尽记熔断失败并存哨兵）
    backoff        重试退避函数，缺省 min(2**attempt, 5) 秒
    circuit_breaker 注入熔断器（缺省取全局实时熔断单点）
    """
    cb = circuit_breaker if circuit_breaker is not None else get_realtime_circuit_breaker()
    if not cb.is_available(source_key):
        logger.warning(f"[熔断] 数据源 {source_key} 处于熔断状态，跳过")
        return None
    if backoff is None:
        backoff = lambda attempt: min(2 ** attempt, 5.0)

    try:
        now = time.time()
        df = cache.get(now)
        if df is None:
            df = _refresh_snapshot(cache, cb, source_key, throttle,
                                   fetch, attempts, backoff, now)
        if df is None or df.empty:
            logger.warning(f"[实时行情] {cache.label} 快照为空，跳过 {stock_code}")
            return None
        row = _locate_row(df, code_columns, stock_code, code_zfill)
        if row is None:
            logger.warning(f"[API返回] {source_label} 未找到 {stock_code} 的实时行情")
            return None
        return row_builder(row, stock_code)
    except Exception as e:
        logger.error(f"[API错误] 获取 {stock_code} 实时行情({source_label})失败: {e}")
        cb.record_failure(source_key, str(e))
        return None


def _refresh_snapshot(cache: TtlSnapshotCache, cb, source_key: str,
                      throttle: Throttle, fetch: Callable[[], pd.DataFrame],
                      attempts: int, backoff: Callable[[int], float],
                      now: float) -> pd.DataFrame:
    """全量刷新：节流 → 重试 → 熔断记账；耗尽存哨兵空表（TTL 内不再请求）。"""
    logger.info(f"[缓存未命中] 触发全量刷新 {cache.label}")
    df: Optional[pd.DataFrame] = None
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            throttle.wait()
            started = time.time()
            df = fetch()
            elapsed = time.time() - started
            logger.info(f"[API返回] {cache.label} 成功: {len(df)} 行, "
                        f"耗时 {elapsed:.2f}s (attempt {attempt}/{attempts})")
            cb.record_success(source_key)
            break
        except Exception as e:
            last_error = e
            logger.warning(f"[API错误] {cache.label} 获取失败 "
                           f"(attempt {attempt}/{attempts}): {e}")
            time.sleep(backoff(attempt))

    if df is None:
        logger.error(f"[API错误] {cache.label} 最终失败: {last_error}")
        cb.record_failure(source_key, str(last_error))
        df = pd.DataFrame()
    cache.store(df, now)
    return df


def _locate_row(df: pd.DataFrame, code_columns: Union[str, Tuple[str, ...]],
                stock_code: str, code_zfill: int) -> Optional[pd.Series]:
    """按 code 列定位行；多候选列依次尝试，支持两侧补零对齐。"""
    columns = (code_columns,) if isinstance(code_columns, str) else code_columns
    target = str(stock_code).strip()
    if code_zfill:
        target = target.zfill(code_zfill)
    for col in columns:
        if col not in df.columns:
            continue
        series = df[col].astype(str).str.strip()
        if code_zfill:
            series = series.str.zfill(code_zfill)
        rows = df[series == target]
        if not rows.empty:
            return rows.iloc[0]
    return None
