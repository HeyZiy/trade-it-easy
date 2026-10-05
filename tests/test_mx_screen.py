# -*- coding: utf-8 -*-
"""MX 选数名解析下沉测试：MXService._rows_to_codes / screen_codes。

行→(code,name) 解析下沉 src/mx 后零网络可测。
"""

from src.mx.service import MXService


class _FakeService:
    """只提供 screen_stocks 的假自对象（screen_codes 不碰其他成员）。"""

    def __init__(self, rows=None, total=0, error=None):
        self._rows, self._total, self._error = rows, total, error

    def screen_stocks(self, keyword, page_no=1, page_size=20):
        if self._error:
            raise self._error
        return self._rows, self._total

    _rows_to_codes = staticmethod(MXService._rows_to_codes)


class TestRowsToCodes:
    def test_plain_columns(self):
        rows = [{"代码": "600519", "股票简称": "贵州茅台",
                 "收盘价 2026-09-25": "1700.0"}]
        assert MXService._rows_to_codes(rows) == [("600519", "贵州茅台")]

    def test_market_abbr_column_excluded(self):
        # "市场代码简称" 含"代码"与"简称"双关键词，须整列跳过
        rows = [{"股票代码": "000001.SZ", "市场代码简称": "SZ",
                 "股票简称 2026-09-25": "平安银行"}]
        assert MXService._rows_to_codes(rows) == [("000001", "平安银行")]

    def test_suffix_normalized(self):
        rows = [{"代码": "920748.BJ"}, {"代码": "SH600000"}]
        assert MXService._rows_to_codes(rows) == [
            ("920748", "920748"), ("600000", "600000")]

    def test_missing_code_row_skipped(self):
        rows = [{"股票简称": "无代码"}, {"代码": "  "}]
        assert MXService._rows_to_codes(rows) == []


class TestScreenCodes:
    def test_parsed_list(self):
        fake = _FakeService(rows=[{"代码": "600519", "股票简称": "贵州茅台"}],
                            total=1)
        assert MXService.screen_codes(fake, "kw") == [("600519", "贵州茅台")]

    def test_empty_rows_fail_soft(self):
        assert MXService.screen_codes(_FakeService(), "kw") == []

    def test_exception_fail_soft(self):
        fake = _FakeService(error=RuntimeError("boom"))
        assert MXService.screen_codes(fake, "kw") == []
