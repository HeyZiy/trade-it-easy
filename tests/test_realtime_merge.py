# -*- coding: utf-8 -*-
"""realtime.merge_realtime_quotes 纯函数测试（零网络）。

锁四语义：主源选择 / 补充最多一次 / 全失败降级 / 优先级串含未声明源名的汇总 warning。
fake fetcher 只带 REALTIME_VARIANTS 声明——merge 索引从声明现场构建，
不存在类名字符串匹配（_SOURCE_SPECS 删除后的回归红线）。"""

import logging

from data_provider.manager import realtime_quotes
from data_provider.realtime import merge_realtime_quotes
from data_provider.types import UnifiedRealtimeQuote


def _full(code, source):
    """全字段报价（无补充需求）。"""
    return UnifiedRealtimeQuote(
        code=code, name="测试股", source=source,
        price=10.0, change_pct=1.0, change_amount=0.1,
        volume=1000, amount=1e7, volume_ratio=1.2, turnover_rate=3.0,
        amplitude=2.0, open_price=9.9, high=10.2, low=9.8, pre_close=9.9,
        pe_ratio=20.0, pb_ratio=2.0, total_mv=1e10, circ_mv=8e9,
    )


def _basic(code, source):
    """只有基础价格：_SUPPLEMENT_FIELDS 全部缺失。"""
    return UnifiedRealtimeQuote(code=code, name="测试股", source=source,
                                price=10.0, change_pct=1.0)


class _FakeFetcher:
    """按变体返回预制结果；kwargs 与变体声明一致（与生产调用形状同构）。"""

    def __init__(self, name, variants, results):
        self.name = name
        self.REALTIME_VARIANTS = variants
        self._results = results  # 变体名 → quote / None / Exception
        self.calls = []

    def get_realtime_quote(self, stock_code, **kwargs):
        for vname, vkwargs in self.REALTIME_VARIANTS.items():
            if kwargs == vkwargs:
                self.calls.append(vname)
                r = self._results.get(vname)
                if isinstance(r, Exception):
                    raise r
                return r
        return None


def _fetcher_a(result):
    return _FakeFetcher("A", {"s1": {"source": "em"}}, {"s1": result})


def _fetcher_b(result):
    return _FakeFetcher("B", {"s2": {}}, {"s2": result})


class TestPrimarySelection:
    def test_first_valid_full_source_returns_immediately(self):
        a, b = _fetcher_a(_full("600000", "s1")), _fetcher_b(_full("600000", "s2"))
        q = merge_realtime_quotes("600000", [a, b], ["s1", "s2"])
        assert q.source == "s1" and q.price == 10.0
        assert b.calls == []  # 字段齐 → 不多打一个源

    def test_failed_primary_falls_to_next(self):
        a = _fetcher_a(RuntimeError("炸了"))
        b = _fetcher_b(_full("600000", "s2"))
        q = merge_realtime_quotes("600000", [a, b], ["s1", "s2"])
        assert q.source == "s2"


class TestSupplementOnce:
    def test_secondary_fills_missing_fields_keeps_primary(self):
        a = _fetcher_a(_basic("600000", "s1"))
        b = _fetcher_b(_full("600000", "s2"))
        q = merge_realtime_quotes("600000", [a, b], ["s1", "s2"])
        assert q.source == "s1"           # primary 身份不被覆盖
        assert q.volume_ratio == 1.2      # 缺失字段被补
        assert q.turnover_rate == 3.0

    def test_second_supplement_attempt_stops_without_merging(self, caplog):
        # s1 主源缺补充字段；s2 补充源同样只带基础字段（补不了任何东西）；
        # s3 不应再被合并（补充尝试上限一次）
        a = _fetcher_a(_basic("600000", "s1"))
        b = _FakeFetcher("B", {"s2": {"source": "sina"}}, {"s2": _basic("600000", "s2")})
        c = _FakeFetcher("C", {"s3": {"source": "tencent"}}, {"s3": _full("600000", "s3")})
        q = merge_realtime_quotes("600000", [a, b, c], ["s1", "s2", "s3"])
        assert q.source == "s1"
        assert q.volume_ratio is None     # s3 的全量数据没有进来
        assert c.calls == ["s3"]          # 被调用作了第二次补充尝试（触发上限即止）


class TestDegrade:
    def test_all_sources_empty_returns_none(self):
        a, b = _fetcher_a(None), _fetcher_b(None)
        assert merge_realtime_quotes("600000", [a, b], ["s1", "s2"]) is None

    def test_all_raise_returns_none_with_warning(self, caplog):
        a = _fetcher_a(RuntimeError("超时"))
        with caplog.at_level(logging.WARNING):
            assert merge_realtime_quotes("600000", [a], ["s1"]) is None
        assert "所有数据源均失败" in caplog.text

    def test_single_source_failure_is_info_only(self, caplog):
        """单源失败是 failover 的一步：只记 INFO，后续源拿到就不告警。"""
        a = _fetcher_a(RuntimeError("超时"))
        b = _fetcher_b(_full("600000", "s2"))
        with caplog.at_level(logging.INFO):
            q = merge_realtime_quotes("600000", [a, b], ["s1", "s2"])
        assert q.source == "s2"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("[s1] 取数失败" in r.getMessage() for r in caplog.records)

    def test_all_missed_warns_once_with_reasons(self, caplog):
        """全部源都没给可用报价：只发一条 WARNING，并带上各源原因。"""
        a = _fetcher_a(None)
        b = _fetcher_b(UnifiedRealtimeQuote(code="600000", source="s2"))  # price=None
        with caplog.at_level(logging.INFO):
            assert merge_realtime_quotes("600000", [a, b], ["s1", "s2"]) is None
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        msg = warns[0].getMessage()
        assert "所有数据源均失败" in msg
        assert "[s1] 无此标的" in msg and "[s2] 报价缺基础数据" in msg


class TestPriorityConsistency:
    def test_undeclared_source_name_warns_once(self, caplog):
        a = _fetcher_a(_full("600000", "s1"))
        with caplog.at_level(logging.WARNING):
            q = merge_realtime_quotes("600000", [a], ["typo_src", "s1"])
        assert q.source == "s1"
        warns = [r for r in caplog.records if "未声明的源名" in r.message]
        assert len(warns) == 1
        assert "typo_src" in warns[0].message

    def test_variant_index_built_from_declarations_not_class_names(self):
        # fetcher.name 与源名完全无关也能路由——_SOURCE_SPECS 时代靠类名字符串匹配的回归红线
        a = _FakeFetcher("WhateverFetcher", {"any_src": {"source": "em"}},
                         {"any_src": _full("600000", "any_src")})
        q = merge_realtime_quotes("600000", [a], ["any_src"])
        assert q.source == "any_src"


# ── manager.realtime_quotes：批量实时取数唯一口径 ──

class _FakeManager:
    """get_realtime_quote(code) → quote / None / raise（无 kwargs，与生产调用形状同构）。"""

    def __init__(self, results):
        self._results = results  # code → quote / None / Exception

    def get_realtime_quote(self, code):
        r = self._results.get(code)
        if isinstance(r, Exception):
            raise r
        return r


def _rt_quote(code, price, name="测试股"):
    return UnifiedRealtimeQuote(code=code, name=name, source="s1", price=price)


class TestRealtimeQuotes:
    def test_valid_prices_and_names_collected(self):
        fm = _FakeManager({"600000": _rt_quote("600000", 10.0),
                           "000001": _rt_quote("000001", 3.5, name="平安银行")})
        prices, names = realtime_quotes(["600000", "000001"], manager=fm)
        assert prices == {"600000": 10.0, "000001": 3.5}
        assert names == {"600000": "测试股", "000001": "平安银行"}

    def test_invalid_prices_dropped_but_names_kept(self):
        # 0 / 负数 / nan / inf / None 全部剔除；简称无条件收（价格无效也收）
        quotes = {"600000": _rt_quote("600000", 0.0),
                  "000001": _rt_quote("000001", -1.0),
                  "600036": _rt_quote("600036", float("nan")),
                  "600030": _rt_quote("600030", float("inf")),
                  "600009": _rt_quote("600009", None)}
        prices, names = realtime_quotes(list(quotes), manager=_FakeManager(quotes))
        assert prices == {}
        assert set(names) == set(quotes)

    def test_none_quote_and_exception_skipped_fail_soft(self, caplog):
        fm = _FakeManager({"600000": None,
                           "000001": RuntimeError("超时"),
                           "600036": _rt_quote("600036", 9.9)})
        with caplog.at_level(logging.WARNING):
            prices, _ = realtime_quotes(["600000", "000001", "600036"], manager=fm)
        assert prices == {"600036": 9.9}  # 单只失败不中断整批
        assert "实时报价获取失败 000001" in caplog.text

    def test_empty_name_not_collected(self):
        fm = _FakeManager({"600000": _rt_quote("600000", 10.0, name="")})
        prices, names = realtime_quotes(["600000"], manager=fm)
        assert prices == {"600000": 10.0} and names == {}

    def test_default_manager_resolved_from_get_fetcher(self, monkeypatch):
        fm = _FakeManager({"600000": _rt_quote("600000", 10.0)})
        monkeypatch.setattr("data_provider.manager.get_fetcher", lambda: fm)
        prices, _ = realtime_quotes(["600000"])
        assert prices == {"600000": 10.0}
