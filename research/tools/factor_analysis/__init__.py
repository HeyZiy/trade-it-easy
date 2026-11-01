"""统一因子分析函数。计算不联网，报告只消费结果，SDK 扩展按需加载。"""
from .core import (rank_ic, clean, mad_outlier, neutralize, assign_groups,
                   evaluate_period, membership_turnover, summarize_ic)
from .analysis import FactorResult, analyze_factor, analyze_factors, combine_factors
from .study import Config, run_study, report
from .reporting import render_report

__all__ = [
    'rank_ic', 'clean', 'mad_outlier', 'neutralize', 'assign_groups',
    'evaluate_period', 'membership_turnover', 'summarize_ic', 'FactorResult',
    'analyze_factor', 'analyze_factors', 'combine_factors',
    'Config', 'run_study', 'report', 'render_report',
]
