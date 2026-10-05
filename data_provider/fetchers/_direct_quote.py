# -*- coding: utf-8 -*-
"""
===================================
直连型实时行情 — 公共装配（内部 seam）
===================================

新浪/腾讯单票直连端点共用一个形状：
节流 → GET → 状态码/空载荷/引号段/字段数四级校验 → 熔断记账 →
按分隔符切字段 → 端点各自的字段下标解析构建 UnifiedRealtimeQuote；
任何失败路径构造统一格式的失败消息并记熔断失败，返回 None。

机制归本模块，数据归端点：
- url/headers/delimiter/min_fields 由调用方给出（端点差异）；
- parse 是端点唯一的本地逻辑：(字段列表, 6 位代码) → 报价；
- 路由层的熔断可用性检查（is_available）归 get_realtime_quote，
  本模块不做重复检查。

与 _snapshot_quote（全量快照形）并列：那是「拉全表查行」，这是「一票一请求」。
"""

import logging
import time
from typing import Callable, List, Optional

import requests

from data_provider._crosscut import Throttle, classify_http_error
from data_provider.types import UnifiedRealtimeQuote, get_realtime_circuit_breaker

logger = logging.getLogger(__name__)


def build_direct_failure_message(source_name: str, endpoint: str, stock_code: str,
                                 symbol: str, category: str, detail: str,
                                 elapsed: float, error_type: str) -> str:
    """直连端点失败消息的统一格式（新浪/腾讯各腿共用）。"""
    return (
        f"{source_name} 实时行情接口失败: endpoint={endpoint}, stock_code={stock_code}, "
        f"symbol={symbol}, category={category}, error_type={error_type}, "
        f"elapsed={elapsed:.2f}s, detail={detail}"
    )


def direct_realtime_quote(
    stock_code: str,
    *,
    source_key: str,
    source_name: str,
    endpoint: str,
    symbol: str,
    url: str,
    headers: dict,
    delimiter: str,
    min_fields: int,
    parse: Callable[[List[str], str], UnifiedRealtimeQuote],
    throttle: Throttle,
    timeout: float = 10,
    encoding: str = "gbk",
    circuit_breaker=None,
) -> Optional[UnifiedRealtimeQuote]:
    """直连端点取单票实时行情；任何失败路径返回 None（fail-soft，调用方走 failover）。

    stock_code   查询代码（6 位）
    source_key   熔断键（端点自声明）
    source_name  失败消息里的端点名（如 新浪/腾讯）
    endpoint     日志用的端点主机+路径（不含 symbol）
    symbol       带市场前缀的请求符号（sh600519）
    url          完整请求 URL
    headers      请求头（端点自带 Referer/UA 轮换）
    delimiter    字段分隔符（新浪 ","，腾讯 "~"）
    min_fields   最少字段数（低于视为载荷残缺）
    parse        (字段列表, 6 位代码) → UnifiedRealtimeQuote（端点下标表）
    """
    cb = circuit_breaker if circuit_breaker is not None else get_realtime_circuit_breaker()
    api_start = time.time()

    def _fail(category: str, detail: str, error_type: str, elapsed: float,
              level=logging.WARNING) -> None:
        message = build_direct_failure_message(
            source_name, endpoint, stock_code, symbol,
            category, detail, elapsed, error_type)
        logger.log(level, message)
        cb.record_failure(source_key, message)

    try:
        logger.info(f"[API调用] {source_name}接口获取 {stock_code} 实时行情: "
                    f"endpoint={endpoint}, symbol={symbol}")
        throttle.wait()
        response = requests.get(url, headers=headers, timeout=timeout)
        response.encoding = encoding
        elapsed = time.time() - api_start

        if response.status_code != 200:
            _fail("http_status", f"HTTP {response.status_code}",
                  "HTTPStatus", elapsed)
            return None

        content = response.text.strip()
        if '=""' in content or not content:
            _fail("empty_response", "empty quote payload",
                  "EmptyResponse", elapsed)
            return None

        data_start = content.find('"')
        data_end = content.rfind('"')
        if data_start == -1 or data_end == -1:
            _fail("malformed_payload", "quote payload missing quotes",
                  "MalformedPayload", elapsed)
            return None

        fields = content[data_start + 1:data_end].split(delimiter)
        if len(fields) < min_fields:
            _fail("insufficient_fields", f"field_count={len(fields)}",
                  "InsufficientFields", elapsed)
            return None

        cb.record_success(source_key)
        return parse(fields, stock_code)

    except Exception as e:
        elapsed = time.time() - api_start
        category, detail = classify_http_error(e)
        _fail(category, detail, type(e).__name__, elapsed, level=logging.ERROR)
        return None
