# -*- coding: utf-8 -*-
"""realtime.merge_realtime_quotes 纯函数测试（零网络）。

锁四语义：主源选择 / 补充最多一次 / 全失败降级 / 优先级串含未声明源名的汇总 warning。
fake fetcher 只带 REALTIME_VARIANTS 声明——merge 索引从声明现场构建，
不存在类名字符串匹配（_SOURCE_SPECS 删除后的回归红线）。"""

import logging

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
