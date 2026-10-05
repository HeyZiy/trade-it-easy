# -*- coding: utf-8 -*-
"""codes.py 守卫谓词钉死测试（字母 ticker 的唯一判定实现）。"""
import pytest

from data_provider.codes import classify_market, is_us_stock_code


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
