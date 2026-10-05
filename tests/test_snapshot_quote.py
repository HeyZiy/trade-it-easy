# -*- coding: utf-8 -*-
"""快照行情公共装配测试 — 假数据走 seam（缓存命中/重试/熔断/哨兵/行定位/分类器）。"""

import pandas as pd
import pytest
import requests

from data_provider._crosscut import Throttle, TtlSnapshotCache, classify_http_error
from data_provider.fetchers._snapshot_quote import snapshot_realtime_quote
from data_provider.types import UnifiedRealtimeQuote


class _FakeBreaker:
    """熔断器假件：记账调用全收，可置为熔断态（API 与 types.CircuitBreaker 同形）。"""

    def __init__(self, available: bool = True):
        self.available = available
        self.successes: list = []
        self.failures: list = []

    def is_available(self, source_key):
        return self.available

    def record_success(self, source_key):
        self.successes.append(source_key)

    def record_failure(self, source_key, message):
        self.failures.append((source_key, message))


class _FakeCache:
    """快照缓存假件：显式控制命中行为，比真 TTL 更可断言。"""

    def __init__(self):
        self.stored = None
        self.gets = 0
        self.saved = 0
        self.label = "测试快照"

    def get(self, now=None):
        self.gets += 1
        return self.stored

    def store(self, df, timestamp=None):
        self.saved += 1
        self.stored = df


def _row_builder(row, code):
    return UnifiedRealtimeQuote(code=code, name=str(row.get("名称", "")),
                                source="test", price=float(row["最新价"]))


def _df(codes, price=10.0):
    return pd.DataFrame({"代码": list(codes),
                         "名称": [f"股{c}" for c in codes],
                         "最新价": [price] * len(codes)})


def _seam(cache, fetch, breaker, stock_code="600001", **kw):
    kw.setdefault("attempts", 2)
    kw.setdefault("code_columns", "代码")
    kw.setdefault("backoff", lambda attempt: 0.0)
    return snapshot_realtime_quote(
        stock_code, source_key="test", source_label="测试快照",
        cache=cache, throttle=Throttle(0, 0), fetch=fetch,
        row_builder=_row_builder, circuit_breaker=breaker, **kw)


def test_cache_hit_skips_fetch():
    cache = _FakeCache()
    cache.stored = _df(["600001"])
    calls = []
    breaker = _FakeBreaker()
    quote = _seam(cache, lambda: (calls.append(1) or _df(["600001"])), breaker)
    assert quote is not None and quote.price == 10.0
    assert quote.name == "股600001"
    assert calls == []                 # 命中零外呼
    assert breaker.successes == []     # 命中不记账


def test_miss_fetches_records_success_and_caches():
    cache = _FakeCache()
    calls = []
    breaker = _FakeBreaker()
    quote = _seam(cache, lambda: (calls.append(1) or _df(["600001"])), breaker)
    assert quote is not None
    assert len(calls) == 1
    assert breaker.successes == ["test"]
    assert cache.saved == 1            # 刷新结果落缓存
    _seam(cache, lambda: (calls.append(1) or _df(["600001"])), breaker)
    assert len(calls) == 1             # 第二次走缓存


def test_fetch_exhaustion_stores_sentinel():
    cache = _FakeCache()
    calls = []
    breaker = _FakeBreaker()

    def fetch():
        calls.append(1)
        raise requests.exceptions.ConnectionError("RemoteDisconnected")

    assert _seam(cache, fetch, breaker, attempts=2) is None
    assert len(calls) == 2             # 重试 ×2
    assert len(breaker.failures) == 1  # 耗尽记一次失败
    _seam(cache, fetch, breaker)
    assert len(calls) == 2             # 哨兵生效，TTL 内不再外呼


def test_empty_market_stores_sentinel_without_failure():
    """空结果 = 成功的空市场：记成功、存哨兵，不记失败。"""
    cache = _FakeCache()
    breaker = _FakeBreaker()
    assert _seam(cache, lambda: pd.DataFrame(), breaker) is None
    assert breaker.successes == ["test"] and breaker.failures == []
    assert cache.saved == 1


def test_row_missing_returns_none_without_failure():
    cache = _FakeCache()
    breaker = _FakeBreaker()
    assert _seam(cache, lambda: _df(["600002"]), breaker) is None
    assert breaker.failures == []


def test_fused_breaker_skips_fetch():
    cache = _FakeCache()
    calls = []
    breaker = _FakeBreaker(available=False)
    assert _seam(cache, lambda: (calls.append(1) or _df(["600001"])), breaker) is None
    assert calls == []


def test_zfill_row_matching():
    """ETF 表 6 位补零对齐（efinance ETF 形状）。"""
    cache = _FakeCache()
    df = pd.DataFrame({"股票代码": ["001592"], "名称": ["科技ETF"],
                       "最新价": [2.34]})
    breaker = _FakeBreaker()
    quote = _seam(cache, lambda: df, breaker, stock_code="1592",
                  code_columns=("股票代码", "code"), code_zfill=6)
    assert quote is not None and quote.price == 2.34
    assert quote.code == "1592"        # quote.code 用原始入参


@pytest.mark.parametrize("message,category", [
    ("RemoteDisconnected('Remote end closed connection without response')",
     "remote_disconnect"),
    ("HTTPSConnectionPool(host='x'): Read timed out", "timeout"),
    ("HTTP Error 403: Forbidden", "rate_limit_or_anti_bot"),
    ("HTTP Error 429: Too Many Requests", "rate_limit_or_anti_bot"),
    ("some socket blowup", "unknown_request_error"),
])
def test_classify_http_error(message, category):
    assert classify_http_error(RuntimeError(message))[0] == category


def test_classify_timeout_instance():
    assert classify_http_error(
        requests.exceptions.ConnectTimeout("timed out"))[0] == "timeout"
