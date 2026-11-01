"""基本面计算：输入报表，输出可继续研究的时序表。"""
from .analysis import analyze_fundamentals
from .catalog import CATEGORY_MAP
from .core import (
    calc_profitability, calc_growth, calc_efficiency, calc_earnings_quality,
    calc_safety, calc_governance, calc_valuation, calc_shareholder, calc_size,
    get_ttm, get_single_quarter,
)
from .data import fetch_inputs

__all__ = [
    'analyze_fundamentals', 'fetch_inputs', 'CATEGORY_MAP', 'get_ttm',
    'get_single_quarter', 'calc_profitability', 'calc_growth', 'calc_efficiency',
    'calc_earnings_quality', 'calc_safety', 'calc_governance', 'calc_valuation',
    'calc_shareholder', 'calc_size',
]
