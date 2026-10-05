"""将不同研究的收益CSV转换为统一日收益，行情获取只在显式调用时发生。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
KINDS = ("auto", "cumulative-percent", "cumulative-return", "nav", "daily-return")


@dataclass(frozen=True)
class CurveSpec:
    """明确输入单位；columns按日期、策略、可选基准排列，#N表示从0开始的列位置。"""

    kind: str = "auto"
    columns: tuple[str, ...] | None = None
    initial_nav: float = 1.0
    initial_benchmark_nav: float = 1.0

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"不支持的收益格式：{self.kind}")
        if self.columns and (len(self.columns) not in (2, 3) or self.kind == "auto"):
            raise ValueError("自定义列需要指定format，并提供日期、策略、可选基准共2或3列")
        if any(not np.isfinite(x) or x <= 0 for x in (self.initial_nav, self.initial_benchmark_nav)):
            raise ValueError("初始净值/资金必须为有限正数")


def read_csv(path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法读取CSV编码：{path}")


def check_dates(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    # pandas 3会按输入类型选择s/us精度；merge_asof要求左右精度完全一致。
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize().astype("datetime64[ns]")
    if frame.empty or frame["date"].isna().any() or frame["date"].duplicated().any():
        raise ValueError("数据为空或日期缺失/重复，不能进行日收益归因")
    return frame.sort_values("date").reset_index(drop=True)


def column_name(raw: pd.DataFrame, selector: str) -> str:
    if selector.startswith("#"):
        try:
            position = int(selector[1:])
            if position < 0 or position >= len(raw.columns):
                raise ValueError()
            return raw.columns[position]
        except ValueError as exc:
            raise ValueError(f"列位置无效：{selector}") from exc
    if selector not in raw.columns:
        raise ValueError(f"CSV不存在列：{selector}")
    return selector


def normalize_returns(raw: pd.DataFrame, spec: CurveSpec | None = None) -> pd.DataFrame:
    """输出date/strategy_nav/strategy_return及可选benchmark_*，日收益为小数。

    auto只识别约定的列名，不通过数值大小猜测单位。净值/资金输入必须给定
    CSV首行之前的初始值；默认1。首行仍作为交易日收益，不丢弃或重置为零。
    """
    spec = spec or CurveSpec()
    known = {
        "cumulative-percent": ("时间", "策略收益", "基准收益"),
        "cumulative-return": ("date", "strategy_cumulative", "benchmark_cumulative"),
        "nav": ("date", "strategy_nav", "benchmark_nav"),
        "daily-return": ("date", "strategy_return", "benchmark_return"),
    }
    kind = spec.kind
    if kind == "auto":
        # 规范化结果同时有NAV和return列，优先直接使用已还原的日收益。
        candidates = [k for k in ("cumulative-percent", "daily-return", "nav", "cumulative-return")
                      if set(known[k][:2]).issubset(raw.columns)]
        if not candidates:
            raise ValueError("无法识别收益CSV。请指定format和columns；汇总表/成交表不能代替逐日收益曲线")
        kind = candidates[0]
    if spec.columns:
        selected = [column_name(raw, col) for col in spec.columns]
    else:
        defaults = known[kind]
        if not set(defaults[:2]).issubset(raw.columns):
            raise ValueError(f"{kind}需要列{defaults[:2]}，或使用columns指定")
        selected = [*defaults[:2]] + ([defaults[2]] if defaults[2] in raw.columns else [])
    if len(set(selected)) != len(selected):
        raise ValueError("日期、策略和基准列不能重复")
    names = ["date", "strategy_value", "benchmark_value"][:len(selected)]
    frame = raw[selected].copy()
    frame.columns = names
    frame = check_dates(frame)
    for prefix, initial in (("strategy", spec.initial_nav), ("benchmark", spec.initial_benchmark_nav)):
        col = f"{prefix}_value"
        if col not in frame:
            continue
        values = frame[col].astype(str).str.strip()
        if kind == "cumulative-percent":
            values = values.str.rstrip("%")
        elif values.str.contains("%", regex=False).any():
            raise ValueError("该格式使用小数/净值，不接受百分号；请显式转换单位")
        values = pd.to_numeric(values, errors="raise")
        if not np.isfinite(values).all():
            raise ValueError(f"{prefix}存在缺失或非有限数值")
        if kind == "daily-return":
            returns = values
            nav = (1 + returns).cumprod()
        else:
            nav = (1 + values / 100 if kind == "cumulative-percent" else
                   1 + values if kind == "cumulative-return" else values / initial)
            returns = nav / nav.shift(fill_value=1) - 1
        if (returns <= -1).any() or (nav <= 0).any() or not np.isfinite(nav).all():
            raise ValueError(f"{prefix}净值必须为有限正数，日收益须大于-100%")
        frame[f"{prefix}_nav"], frame[f"{prefix}_return"] = nav, returns
        frame = frame.drop(columns=[col])
    frame.attrs["input_kind"] = kind
    return frame


def read_returns(path: Path, spec: CurveSpec | None = None) -> pd.DataFrame:
    return normalize_returns(read_csv(path), spec)


def normalize_index(raw: pd.DataFrame) -> pd.DataFrame:
    if not {"date", "close"}.issubset(raw.columns):
        raise ValueError("指数CSV需要date和close列")
    frame = check_dates(raw[["date", "close"]])
    frame["close"] = pd.to_numeric(frame["close"], errors="raise")
    if not np.isfinite(frame["close"]).all() or (frame["close"] <= 0).any():
        raise ValueError("指数收盘价缺失、非有限或非正值")
    return frame


def read_index(path: Path) -> pd.DataFrame:
    return normalize_index(read_csv(path))


def fetch_index(symbol: str = "sh000906", cache: Path | None = None,
                refresh: bool = False) -> tuple[pd.DataFrame, dict]:
    """Sina指数日线；缓存按symbol隔离。离线调用直接使用read_index即可。"""
    if not re.fullmatch(r"(?:sh|sz)\d{6}", symbol):
        raise ValueError("新浪指数代码须为sh/sz加6位数字，例如sh000906")
    cache = cache or HERE / "_cache" / f"{symbol}.csv"
    source = {"source": f"AKShare Sina {symbol}", "index_symbol": symbol,
              "source_url": f"https://finance.sina.com.cn/realstock/company/{symbol}/nc.shtml",
              "cache": str(cache.resolve()), "cache_hit": cache.exists() and not refresh}
    if source["cache_hit"]:
        raw = read_csv(cache)
        if "symbol" in raw and not raw["symbol"].eq(symbol).all():
            raise ValueError("缓存中的指数代码与请求不符，请更换缓存路径或刷新")
        return normalize_index(raw), source
    import akshare as ak

    raw = ak.stock_zh_index_daily(symbol=symbol)
    index = normalize_index(raw)
    raw["symbol"] = symbol
    cache.parent.mkdir(parents=True, exist_ok=True)
    raw.to_csv(cache, index=False, encoding="utf-8-sig")
    return index, source
