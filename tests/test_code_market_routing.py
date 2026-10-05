# -*- coding: utf-8 -*-
"""码族→市场路由钉死测试（fetcher 层）。

背景：5dafb0a 之前，新上交所 53/55 ETF 码族在 tushare 被默认成深市
（静默查空→failover）、在 amazingdata 被拒查（raise→failover）。
本文件把各数据源对码族的处置逐族钉死，防同类回归；
判定权威在 data_provider/codes.py。
"""
import pytest

from data_provider.fetchers.amazingdata_fetcher import _code_to_tgw_format
from data_provider.fetchers.tushare_fetcher import TushareFetcher


def _tushare_convert(code: str) -> str:
    """_convert_stock_code 不读实例状态，用跳过 __init__ 的裸实例调用（不碰 token/config）。"""
    return TushareFetcher._convert_stock_code(object.__new__(TushareFetcher), code)


class TestAmazingDataTgwFormat:
    """TGW 通道：SH/SZ 股票+ETF 受支持；北交所不在能力声明内（返回 None，非 bug）。"""

    @pytest.mark.parametrize("code,expected", [
        ("600519", "600519.SH"),
        ("000001", "000001.SZ"),
        ("510300", "510300.SH"),
        ("159915", "159915.SZ"),
        ("530100", "530100.SH"),  # 2025 新沪 ETF 码族 53
        ("558001", "558001.SH"),  # 科创债 ETF 码族 55
        ("sh600519", "600519.SH"),
    ])
    def test_supported_families(self, code, expected):
        assert _code_to_tgw_format(code) == expected

    @pytest.mark.parametrize("code", ["920748", "430047"])
    def test_bse_unsupported(self, code):
        assert _code_to_tgw_format(code) is None


class TestTushareConvertStockCode:
    """Tushare 后缀路由：53/55 必须落沪市（修复点曾被「默认深市」吞掉）。"""

    @pytest.mark.parametrize("code,expected", [
        ("600519", "600519.SH"),
        ("000001", "000001.SZ"),
        ("920748", "920748.BJ"),
        ("510300", "510300.SH"),
        ("159915", "159915.SZ"),
        ("530100", "530100.SH"),
        ("558001", "558001.SH"),
        ("600519.SH", "600519.SH"),
    ])
    def test_convert(self, code, expected):
        assert _tushare_convert(code) == expected
