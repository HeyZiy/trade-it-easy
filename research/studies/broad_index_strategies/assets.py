"""本研究唯一的标的配置：ETF、跟踪指数及可选估值取数方式。

新增标的只需在 ASSETS 中增加 AssetSpec，并准备对应行情缓存。
valuation=None 时仍运行价格策略，不运行该标的的估值策略。
"""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ValuationSource:
    method: Literal["akshare", "direct"]
    symbol: str

    def __post_init__(self):
        if self.method not in ("akshare", "direct") or not self.symbol.strip():
            raise ValueError("估值方式须为 akshare/direct，并提供取数用的 symbol")


@dataclass(frozen=True)
class AssetSpec:
    etf_code: str
    name: str
    index_code: str
    valuation: ValuationSource | None = None

    def __post_init__(self):
        if len(self.etf_code) != 6 or not self.etf_code.isascii() or not self.etf_code.isdigit():
            raise ValueError("ETF代码须为6位数字字符串，保留前导零")
        if self.index_code:
            if len(self.index_code) != 6 or not self.index_code.isascii() or not self.index_code.isdigit():
                raise ValueError("指数代码须为6位数字字符串，保留前导零；无A股指数代码（如QDII）时置空")
        elif self.valuation is not None:
            raise ValueError("配置了估值策略的标的必须提供指数代码")
        if not self.name.strip():
            raise ValueError("标的名称不能为空")


# 12 只 ETF。恒生科技（513180）2021-05 上市、科技风格指数，窗口仅约 4.5 年，
# 起点在 2021 顶部腰斩后的位置，解读结果时注意窗口短、起点特殊。
# QDII 与恒生科技暂无稳定指数 PE 数据源，valuation=None（只跑价格策略）。
# 中证红利暂无现有接口可用的PE历史，仅运行价格策略。
ASSETS = (
    AssetSpec("510300", "沪深300ETF", "000300", ValuationSource("akshare", "沪深300")),
    AssetSpec("510500", "中证500ETF", "000905", ValuationSource("akshare", "中证500")),
    AssetSpec("159915", "创业板ETF", "399006", ValuationSource("direct", "399006.SZ")),
    AssetSpec("510050", "上证50ETF", "000016", ValuationSource("akshare", "上证50")),
    AssetSpec("510180", "上证180ETF", "000010", ValuationSource("akshare", "上证180")),
    AssetSpec("159901", "深100ETF", "399330", ValuationSource("direct", "399330.SZ")),
    AssetSpec("512100", "中证1000ETF", "000852", ValuationSource("akshare", "中证1000")),
    AssetSpec("159920", "恒生ETF", "", None),
    AssetSpec("513500", "标普500ETF", "", None),
    AssetSpec("513100", "纳指ETF", "", None),
    AssetSpec("513180", "恒生科技ETF", "", None),
    AssetSpec("515180", "中证红利ETF", "000922", None),
)

if len({asset.etf_code for asset in ASSETS}) != len(ASSETS):
    raise ValueError("ASSETS 中ETF代码重复")
