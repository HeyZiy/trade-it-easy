"""跨研究复用的市场状态收益诊断，导入时不会获取行情或写文件。"""

from .analysis import RegimeAnalysis, RegimeConfig, analyze_returns
from .data import CurveSpec, normalize_returns, read_index, read_returns

__all__ = ["CurveSpec", "RegimeConfig", "RegimeAnalysis", "read_returns", "read_index",
           "normalize_returns", "analyze_returns"]
