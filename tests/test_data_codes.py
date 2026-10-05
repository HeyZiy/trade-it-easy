# -*- coding: utf-8 -*-
"""codes.py 钉死测试：归一化矩阵 / ETF 码族 / 字母 ticker / 市场归类。"""
import pytest

from data_provider.codes import classify_market, is_etf_code, is_us_stock_code, normalize_stock_code


class TestIsUsStockCode:
    """字母 ticker 守卫：全仓唯一实现，SPX/DJI 等指数符号同样按字母 ticker 拒收。"""

    @pytest.mark.parametrize("code", ["AAPL", "BRK.B", "DJI", "SPX", "TSLA", "aapl"])
    def test_alpha_tickers_accepted(self, code):
        assert is_us_stock_code(code)

    @pytest.mark.parametrize("code", ["600519", "600519.SH", "000001", "00700.HK", "hk00700", "510300"])
    def test_cn_hk_codes_rejected(self, code):
        assert not is_us_stock_code(code)


class TestClassifyMarket:
    """市场归类只剩 cn（美股/港股不在支持范围，港股代码流入后自然以查无数据失败）。"""

    def test_hk_codes_classify_cn(self):
        assert classify_market("hk00700") == "cn"

    def test_cn(self):
        assert classify_market("600519") == "cn"


class TestNormalizeStockCode:
    """归一化矩阵：前缀 / 后缀 / 裸码各格式钉死。"""

    @pytest.mark.parametrize("raw,expected", [
        ("600519", "600519"),
        ("SH600519", "600519"),
        ("sz000001", "000001"),
        ("BJ920748", "920748"),
        ("600519.SH", "600519"),
        ("000001.SZ", "000001"),
        ("920748.BJ", "920748"),
    ])
    def test_exchange_markers_stripped(self, raw, expected):
        assert normalize_stock_code(raw) == expected

    def test_alpha_ticker_passthrough(self):
        assert normalize_stock_code("AAPL") == "AAPL"


class TestIsEtfCode:
    """ETF 码族判定：含 2025 年新上交所码族 53/55（5dafb0a 故障类）。"""

    @pytest.mark.parametrize("code", [
        "510300", "512100", "530100", "558001", "561560", "588000",  # 沪 51/52/53/55/56/58
        "159915", "161725", "184801",  # 深 15/16/18
    ])
    def test_etf_families(self, code):
        assert is_etf_code(code)

    @pytest.mark.parametrize("code", ["600519", "000001", "920748", "110038"])
    def test_non_etf(self, code):
        assert not is_etf_code(code)
