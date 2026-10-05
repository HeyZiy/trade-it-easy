# -*- coding: utf-8 -*-
"""码族→市场路由钉死测试（fetcher 层）。

背景：5dafb0a 之前，新上交所 53/55 ETF 码族在 tushare 被默认成深市
（静默查空→failover）、在 amazingdata 被拒查（raise→failover）。
本文件把各数据源对码族的处置逐族钉死，防同类回归；
判定权威已单点化至 data_provider/codes.py 的 market_suffix，
fetcher 层只做格式适配与能力声明。
"""
import pytest

from data_provider.bars import _etf_sym
from data_provider.types import DataFetchError
from data_provider.fetchers.akshare_fetcher import _to_sina_tx_symbol
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
    """Tushare 后缀路由：53/55 必须落沪市（5dafb0a 事故类，曾被「默认深市」吞掉）。"""

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


class TestTushareFailClosed:
    """码族外显式拒绝（fail-closed）：「默认深市」分支已删除。"""

    @pytest.mark.parametrize("code", ["110038", "500001", "123456"])
    def test_unknown_family_raises(self, code):
        with pytest.raises(DataFetchError):
            _tushare_convert(code)


class TestAkshareSinaTxSymbol:
    """新浪/腾讯符号路由：53/55 落沪；B 股与北交所按市场归属表。"""

    @pytest.mark.parametrize("code,expected", [
        ("600519", "sh600519"),
        ("000001", "sz000001"),
        ("920748", "bj920748"),
        ("510300", "sh510300"),
        ("530100", "sh530100"),
        ("558001", "sh558001"),
        ("159915", "sz159915"),
        ("900948", "sh900948"),
        ("200596", "sz200596"),
        ("600519.SH", "sh600519"),
    ])
    def test_symbols(self, code, expected):
        assert _to_sina_tx_symbol(code) == expected

    def test_unknown_family_raises(self):
        with pytest.raises(ValueError):
            _to_sina_tx_symbol("110038")


class TestBarsEtfSym:
    """bars 新浪符号：显式前缀优先；裸码按市场归属表（53/55 落沪）。"""

    @pytest.mark.parametrize("code,expected", [
        ("510300", "sh510300"),
        ("530100", "sh530100"),
        ("558001", "sh558001"),
        ("159915", "sz159915"),
        ("sh530100", "sh530100"),
        ("sz159915", "sz159915"),
    ])
    def test_symbols(self, code, expected):
        assert _etf_sym(code) == expected

    def test_unknown_family_raises(self):
        with pytest.raises(ValueError):
            _etf_sym("110038")
