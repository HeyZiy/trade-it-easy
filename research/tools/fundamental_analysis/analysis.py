"""按类别计算基本面时序表；数据获取由调用方决定。"""
import pandas as pd

from . import core
from .catalog import CATEGORY_MAP, QUARTERLY_CATEGORIES


def select_categories(categories=None):
    if categories is None or categories == 'all':
        return list(CATEGORY_MAP)
    selected = [categories] if isinstance(categories, str) else list(categories)
    unknown = set(selected) - CATEGORY_MAP.keys()
    if unknown:
        raise ValueError(f"未知基本面类别: {sorted(unknown)}")
    return list(dict.fromkeys(selected))


def analyze_fundamentals(code, bs, inc, cf, *, kline=None,
                         equity_structure=None, dividend=None,
                         holder_num=None, share_holder=None, categories=None):
    """返回 {类别: DataFrame}；季频索引为报告期，日频索引为交易日。

    三表使用 SDK 字段和累计报表口径。日频类别要求 kline，其他辅助数据
    缺失时延续各计算函数的 NaN 规则。计算异常直接向调用方抛出。
    """
    selected = select_categories(categories)
    quarterly = {
        'profitability': core.calc_profitability,
        'growth': core.calc_growth,
        'efficiency': core.calc_efficiency,
        'earnings_quality': core.calc_earnings_quality,
        'safety': core.calc_safety,
    }
    trade_dates = None
    if any(category not in QUARTERLY_CATEGORIES for category in selected):
        if kline is None or kline.empty:
            raise ValueError('日频基本面类别需要非空 kline')
        times = pd.to_datetime(kline['kline_time'])
        trade_dates = pd.DatetimeIndex(times).drop_duplicates().sort_values()
    result = {}
    for category in selected:
        if category in quarterly:
            frame = quarterly[category](bs, inc, cf)
        elif category == 'valuation':
            frame = core.calc_valuation(code, bs, inc, cf, dividend, kline, equity_structure)
        elif category == 'size':
            frame = core.calc_size(code, kline, equity_structure)
        elif category == 'governance':
            frame = core.calc_governance(code, inc, equity_structure, dividend, trade_dates)
        else:
            frame = core.calc_shareholder(code, holder_num, share_holder, trade_dates)
        result[category] = frame
    return result
