# -*- coding: utf-8 -*-
"""乐咕 PE 取数：限速、429 退避重试、覆盖缺口直接报错、缓存命中不触网。"""
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from research.studies.timing import web_report as wrp
from research.tools.etf_data import fetch_em
from akshare.stock_feature.stock_a_indicator import get_cookie_csrf  # noqa: F401  (patch target)
import akshare.stock_feature.stock_a_indicator as indicator


@pytest.fixture
def fast_pe(monkeypatch):
    monkeypatch.setattr(wrp, "_PE_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(wrp, "_PE_LAST", [0.0])
    monkeypatch.setattr(wrp, "sleep", lambda *_: None)
    monkeypatch.setattr(fetch_em, "_legulegu_token", lambda: "token")
    monkeypatch.setattr(indicator, "get_cookie_csrf", lambda **_: {})


def response(status, payload):
    return SimpleNamespace(status_code=status, headers={},
                           json=lambda: payload, raise_for_status=lambda: None)


def test_429_then_success_retries(fast_pe, monkeypatch, tmp_path):
    calls = []
    data = [{"date": "2024-01-02", "ttmPe": 12.0}, {"date": "2024-01-03", "ttmPe": 13.0}]

    def fake_get(url, **kwargs):
        calls.append(kwargs["params"]["indexCode"])
        return response(429, {}) if len(calls) == 1 else response(200, {"data": data})

    monkeypatch.setattr(requests, "get", fake_get)
    pe, as_of = wrp.load_pe("000300", tmp_path / "pe")
    assert len(calls) == 2
    assert as_of == "2024-01-03"
    assert pe.loc["2024-01-02"] == 12.0
    assert (tmp_path / "pe" / "pe_000300.csv").is_file()


def test_empty_data_fails_loud_without_retry(fast_pe, monkeypatch, tmp_path):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(1)
        return response(200, {"data": []})

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(ValueError, match="乐咕无指数 000852"):
        wrp.load_pe("000852", tmp_path / "pe")
    assert len(calls) == 1


def test_persistent_429_gives_up_after_retries(fast_pe, monkeypatch, tmp_path):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(1)
        return response(429, {})

    monkeypatch.setattr(requests, "get", fake_get)
    with pytest.raises(ValueError, match="乐咕 PE 取数失败"):
        wrp.load_pe("399006", tmp_path / "pe")
    assert len(calls) == wrp._PE_RETRIES


def test_cache_hit_avoids_network(fast_pe, monkeypatch, tmp_path):
    pe_dir = tmp_path / "pe"
    pe_dir.mkdir()
    pd.DataFrame({"date": ["2024-01-02"], "pe": [11.0]}).to_csv(pe_dir / "pe_000300.csv", index=False)

    def boom(*_a, **_k):
        raise AssertionError("缓存命中不应触网")

    monkeypatch.setattr(requests, "get", boom)
    pe, as_of = wrp.load_pe("000300", pe_dir)
    assert as_of == "2024-01-02"
    assert pe.loc["2024-01-02"] == 11.0


def test_invalid_index_code_rejected(tmp_path):
    with pytest.raises(ValueError, match="6 位数字"):
        wrp.load_pe("12ab", tmp_path / "pe")