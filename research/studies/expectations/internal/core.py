"""低预期研究使用的日期解析和中心秩工具。"""
import numpy as np
import pandas as pd
from research.tools.single_factor.single_factor_test import clean

def report_date(value):
    if pd.isna(value):
        return pd.NaT
    try:
        text = str(value).strip().lower()
        if len(text) == 6 and text[:4].isdigit() and text[4] == 'q' and text[5] in '1234':
            return pd.Period(text, freq='Q').end_time.normalize()
        date = pd.Timestamp(value).normalize()
        return date if date.is_quarter_end else pd.NaT
    except (ValueError, TypeError, OverflowError):
        return pd.NaT

def centered_rank(values):
    """同分同秩，截面中心化；产品项不等价于只买低估值且改善强。"""
    ranks = clean(values).rank(method='average', pct=True)
    return ranks - ranks.mean()
