# -*- coding: utf-8 -*-
"""直连实时行情公共装配测试 — 假 HTTP（状态码/空载荷/引号段/字段数/熔断/成功解析）。"""

import pandas as pd
import pytest
import requests

from data_provider._crosscut import Throttle
from data_provider.fetchers._direct_quote import (
    build_direct_failure_message,
    direct_realtime_quote,
)
from data_provider.types import UnifiedRealtimeQuote


class _FakeBreaker:
    def __init__(self):
        self.successes = []
        self.failures = []

    def record_success(self, source_key):
        self.successes.append(source_key)

    def record_failure(self, source_key, message):
        self.failures.append((source_key, message))


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.encoding = None


def _sina_payload(price="1866.000", pre_close="1870.000"):
    """32 字段新浪载荷：0 名称 / 1 今开 / 2 昨收 / 3 最新价 / 4 高 / 5 低 / 8 量 / 9 额。"""
    f = ["x"] * 32
    f[0] = "贵州茅台"
    f[1] = "1860.000"
    f[2] = pre_close
    f[3] = price
    f[4] = "1880.000"
    f[5] = "1850.000"
    f[8] = "3000000"
    f[9] = "5600000000"
    return 'var hq_str_sh600519="' + ",".join(f) + '";'


def _parse_stub(fields, code):
    return UnifiedRealtimeQuote(code=code, name=fields[0], source="test",
                                price=float(fields[3]))


@pytest.fixture
def breaker():
    return _FakeBreaker()


def _quote(monkeypatch, breaker, payload, status=200, delimiter=",",
           min_fields=32, parse=_parse_stub):
    monkeypatch.setattr(requests, "get",
                        lambda url, headers=None, timeout=None: _FakeResponse(status, payload))
    return direct_realtime_quote(
        "600519", source_key="test", source_name="测试",
        endpoint="test.endpoint/list", symbol="sh600519",
        url="http://test.endpoint/list=sh600519",
        headers={"Referer": "http://test"}, delimiter=delimiter,
        min_fields=min_fields, parse=parse,
        throttle=Throttle(0, 0), circuit_breaker=breaker)


def test_success_parses_fields(breaker, monkeypatch):
    quote = _quote(monkeypatch, breaker, _sina_payload())
    assert quote is not None and quote.name == "贵州茅台"
    assert quote.price == 1866.0 and quote.code == "600519"
    assert breaker.successes == ["test"] and breaker.failures == []


def test_http_status_failure_records_breaker(breaker, monkeypatch):
    assert _quote(monkeypatch, breaker, _sina_payload(), status=403) is None
    assert breaker.successes == [] and len(breaker.failures) == 1
    assert "http_status" in breaker.failures[0][1]


def test_empty_payload_failure(breaker, monkeypatch):
    assert _quote(monkeypatch, breaker, 'var hq_str_sh600519="";') is None
    assert "empty_response" in breaker.failures[0][1]


def test_missing_quotes_failure(breaker, monkeypatch):
    assert _quote(monkeypatch, breaker, "no quotes here") is None
    assert "malformed_payload" in breaker.failures[0][1]


def test_insufficient_fields_failure(breaker, monkeypatch):
    short = 'var hq_str_sh600519="' + ",".join(["x"] * 10) + '";'
    assert _quote(monkeypatch, breaker, short) is None
    assert "insufficient_fields" in breaker.failures[0][1]


def test_request_exception_classified_and_recorded(breaker, monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise requests.exceptions.ConnectTimeout("Read timed out")
    monkeypatch.setattr(requests, "get", boom)
    quote = direct_realtime_quote(
        "600519", source_key="test", source_name="测试",
        endpoint="test.endpoint/list", symbol="sh600519",
        url="http://test.endpoint/list=sh600519",
        headers={"Referer": "http://test"}, delimiter=",",
        min_fields=32, parse=_parse_stub,
        throttle=Throttle(0, 0), circuit_breaker=breaker)
    assert quote is None
    assert breaker.failures and "timeout" in breaker.failures[0][1]


def test_failure_message_format():
    message = build_direct_failure_message(
        "新浪", "hq.sinajs.cn/list", "600519", "sh600519",
        "http_status", "HTTP 403", 1.5, "HTTPStatus")
    assert message.startswith("新浪 实时行情接口失败")
    assert "endpoint=hq.sinajs.cn/list" in message
    assert "symbol=sh600519" in message and "elapsed=1.50s" in message


def test_fetcher_legs_build_real_urls(monkeypatch):
    """回归：端点腿 URL 必须用常量内插。曾因 f-string 漏 {} 把
    'TENCENT_REALTIME_ENDPOINT=sz000776' 整串当主机名，DNS 解析失败。
    """
    from data_provider.fetchers.akshare_fetcher import AkshareFetcher
    fetcher = AkshareFetcher(0, 0)

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(200, payload)

    payload = _sina_payload()
    monkeypatch.setattr(requests, "get", fake_get)
    quote = fetcher._get_stock_realtime_quote_sina("000776")
    assert quote is not None and quote.name == "贵州茅台"
    assert captured["url"] == "http://hq.sinajs.cn/list=sz000776"

    f = ["x"] * 47
    f[1] = "广发证券"
    f[3] = "16.66"
    f[4] = "16.60"
    f[5] = "16.62"
    f[6] = "123456"
    f[44] = "100.0"
    f[45] = "120.0"
    payload = 'v_sz000776="' + "~".join(f) + '";'
    quote = fetcher._get_stock_realtime_quote_tencent("000776")
    assert quote is not None and quote.name == "广发证券"
    assert captured["url"] == "http://qt.gtimg.cn/q=sz000776"


def test_tencent_delimiter_shape(breaker, monkeypatch):
    """腾讯 ~ 分隔 45 字段载荷走同一装配（分隔符是端点数据）。"""
    f = ["x"] * 45
    f[1] = "贵州茅台"
    f[3] = "1866.000"
    payload = 'v_sh600519="' + "~".join(f) + '";'
    quote = _quote(monkeypatch, breaker, payload, delimiter="~",
                   min_fields=45,
                   parse=lambda fs, c: UnifiedRealtimeQuote(
                       code=c, name=fs[1], source="tencent",
                       price=float(fs[3])))
    assert quote is not None and quote.name == "贵州茅台" and quote.price == 1866.0
