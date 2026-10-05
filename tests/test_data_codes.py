# -*- coding: utf-8 -*-
"""codes.py 钉死测试：归一化矩阵 / ETF 码族 / 字母 ticker / 市场归类。"""
import pytest

from data_provider.codes import (
    classify_market, is_a_stock_code, is_etf_code, is_us_stock_code,
    market_suffix, normalize_stock_code,
)


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


class TestMarketSuffix:
    """码族→市场归属唯一权威：逐族钉死（与 codes.py 码族表一一对应）。"""

    @pytest.mark.parametrize("code,expected", [
        # 沪 ETF：51/52/53/55/56/58（53/55 为 2025 新码族，5dafb0a 故障类）
        ("510300", "SH"), ("512100", "SH"), ("530100", "SH"),
        ("558001", "SH"), ("561560", "SH"), ("588000", "SH"),
        # 深 ETF：15/16/18
        ("159915", "SZ"), ("161725", "SZ"), ("184801", "SZ"),
        # 沪股票：600/601/603/605/688/689
        ("600519", "SH"), ("601318", "SH"), ("603288", "SH"),
        ("605358", "SH"), ("688981", "SH"), ("689009", "SH"),
        # 深股票：000/001/002/003/300/301
        ("000001", "SZ"), ("001979", "SZ"), ("002594", "SZ"),
        ("003816", "SZ"), ("300750", "SZ"), ("301269", "SZ"),
        # 北交所：43/83/87/88/920
        ("430047", "BJ"), ("832566", "BJ"), ("871981", "BJ"),
        ("889966", "BJ"), ("920748", "BJ"),
        # B 股
        ("900948", "SH"), ("200596", "SZ"),
    ])
    def test_families(self, code, expected):
        assert market_suffix(code) == expected

    @pytest.mark.parametrize("code", ["110038", "500001", "123456", ""])
    def test_unknown_family_is_none(self, code):
        """码族外（可转债 11x / 老封基 50x / 乱码）一律 None——fail-closed 契约。"""
        assert market_suffix(code) is None

    def test_prefixed_forms(self):
        assert market_suffix("600519.SH") == "SH"
        assert market_suffix("sh600519") == "SH"


class TestIsAStockCode:
    """A 股股票判定（本体迁自 src/mx/position_utils.py）。"""

    @pytest.mark.parametrize("code", [
        "600519", "601318", "603288", "605358", "688981", "689009",
        "000001", "001979", "002594", "003816", "300750", "301269",
        "430047", "832566", "871981", "889966", "920748",
    ])
    def test_stock_families(self, code):
        assert is_a_stock_code(code)

    @pytest.mark.parametrize("code", [
        "510300", "159915",     # ETF
        "900948", "200596",     # B 股
        "110038", "123456", "",  # 可转债 / 码族外
    ])
    def test_non_stock(self, code):
        assert not is_a_stock_code(code)
