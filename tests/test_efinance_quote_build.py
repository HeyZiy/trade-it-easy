# -*- coding: utf-8 -*-
"""efinance 快照行 → 统一报价 builder 回归测试。

bug 背景：builder 曾用中英映射表的英文值当查询键（row.get('price')），
而 efinance（0.5.8/0.5.9）实际返回中文列，导致生产 9 天全字段 None 无人发现。
fixture 列名取自 efinance 真实快照（20 列）。
"""

import pandas as pd

from data_provider.fetchers.efinance_fetcher import (
    _build_efinance_etf_quote,
    _build_efinance_quote,
)

_SNAPSHOT_ROW = {
    '股票代码': '510300',
    '股票名称': '沪深300ETF华泰柏瑞',
    '涨跌幅': -1.34,
    '最新价': 4.123,
    '最高': 4.192,
    '最低': 4.123,
    '今开': 4.186,
    '涨跌额': -0.056,
    '换手率': 3.96,
    '量比': 0.59,
    '动态市盈率': '-',
    '成交量': 14039888,
    '成交额': 5847854734.0,
    '昨日收盘': 4.179,
    '总市值': 146147930600,
    '流通市值': 146147930600,
    '行情ID': '1.510300',
    '市场类型': '沪A',
    '更新时间': '2026-10-27 16:11:35',
    '最新交易日': '2026-10-27',
}


def test_etf_quote_maps_chinese_columns():
    quote = _build_efinance_etf_quote(pd.Series(_SNAPSHOT_ROW), '510300')
    assert quote.price == 4.123
    assert quote.name == '沪深300ETF华泰柏瑞'
    assert quote.change_pct == -1.34
    assert quote.change_amount == -0.056
    assert quote.volume == 14039888
    assert quote.amount == 5847854734.0
    assert quote.turnover_rate == 3.96
    assert quote.high == 4.192
    assert quote.low == 4.123
    assert quote.open_price == 4.186
    assert quote.volume_ratio == 0.59
    assert quote.total_mv == 146147930600
    assert quote.circ_mv == 146147930600
    assert quote.pe_ratio is None  # '-' 非数值


def test_stock_quote_maps_chinese_columns():
    row = {**_SNAPSHOT_ROW, '股票代码': '600519', '股票名称': '贵州茅台',
           '动态市盈率': 21.5}
    quote = _build_efinance_quote(pd.Series(row), '600519')
    assert quote.price == 4.123
    assert quote.name == '贵州茅台'
    assert quote.pe_ratio == 21.5
    assert quote.open_price == 4.186
