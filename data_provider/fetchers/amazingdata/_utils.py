"""代码、日期、返回结构与缓存目录的共同规则（不登录）。"""
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from numbers import Integral
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import pandas as pd
from data_provider.codes import market_suffix, normalize_stock_code

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DateLike = Union[int, str, date, datetime, pd.Timestamp]


def date_to_int(value: DateLike) -> int:
    if isinstance(value, Integral):
        value = str(value)
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("日期不能为空")
    return int(stamp.strftime("%Y%m%d"))


def date_range(begin, end):
    begin, end = date_to_int(begin), date_to_int(end)
    if begin > end:
        raise ValueError("begin_date 必须不晚于 end_date")
    return begin, end

def _index_code_to_tgw_format(code: str) -> Optional[str]:
    """
    指数代码转换为 tgw 格式（sh000001 / 000001 -> 000001.SH）。

    指数与个股走独立通道：000001 在个股通道是平安银行(000001.SZ)，
    在指数通道是上证指数(000001.SH)——两者 tgw 代码本就不同市场后缀，
    但转换入口必须分开，避免裸码歧义导致误查个股 K 线。

    Returns:
        tgw 格式代码；不支持的代码返回 None
    """
    code = (code or "").strip().lower()
    if code.startswith(("sh", "sz", "bj")) and len(code) == 8:
        pref, num = code[:2], code[2:]
    else:
        # 裸码按确定规则推断：399 → 深证/国证指数，其余 → 上证指数
        pref, num = ("sz" if code.startswith("399") else "sh"), code
    if not (num.isdigit() and len(num) == 6):
        return None
    if pref == "bj":
        return None  # 北交所指数暂不支持
    return f"{num}.{pref.upper()}"


def _code_to_tgw_format(code: str) -> str:
    """
    将标准 6 位代码转换为 tgw 格式（600519 -> 600519.SH）。

    市场归属唯一权威在 codes.market_suffix；TGW 能力声明只含沪深股票与 ETF，
    北交所/B 股/码族外返回 None 由调用方 failover（不猜市场、不扩能力）。

    Returns:
        tgw 格式代码；不支持的代码返回 None
    """
    code = normalize_stock_code(code)
    if not code.isdigit() or len(code) != 6:
        return None
    suffix = market_suffix(code)
    if suffix not in ("SH", "SZ"):
        return None
    return f"{code}.{suffix}"


def _flatten_snapshot(raw):
    """query_snapshot 按日期嵌套返回 {20261030: {tgw: df}}，合并一层供按代码取值。"""
    if isinstance(raw, dict) and raw and all(isinstance(v, dict) for v in raw.values()):
        merged: Dict[str, Any] = {}
        for inner in raw.values():
            merged.update(inner)
        return merged
    return raw


def _resolve_cache_dir() -> str:
    """InfoData（get_equity_structure 等）本地 HDF5 缓存目录。

    与 src/etf/amazing_factors.py 共用同一份缓存（该模块直接复用本函数），
    避免两处各自落盘、各自冷启动。相对路径按「项目根」解析 —— cron 以项目根为
    工作目录，但手动从别处运行或被 import 复用时 CWD 会变，直接用相对路径会解析到别处。

    返回值必须带尾部分隔符：SDK 把 local_path 当字符串前缀拼 "infodata/..."，
    少了分隔符会拼成 AmazingData_local_datainfodata/（文档示例亦为 'D://...//'）。
    """
    raw = (os.getenv("AMAZING_DATA_DIR") or "").strip()
    if raw:
        path = Path(raw)
        base = path if path.is_absolute() else _PROJECT_ROOT / path
    else:
        base = _PROJECT_ROOT / "data" / "AmazingData_local_data"
    return str(base) + os.sep


def _tgw_codes(codes: Iterable[str]) -> List[str]:
    mapped: List[str] = []
    for code in codes:
        tgw = _code_to_tgw_format(str(code))
        if tgw is None:
            logger.debug(f"[AmazingData] 跳过无法映射的代码 {code}")
            continue
        mapped.append(tgw)
    return mapped


def _chunks(seq: List[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _plain_code(tgw_code) -> str:
    """SDK 快照键可能返回数字形态（如 601225 或 792），归一为 6 位代码。"""
    if isinstance(tgw_code, (int, float)):
        return f"{int(tgw_code):06d}"
    return str(tgw_code).split(".")[0]


def _normalize_map(raw, batch: List[str]) -> Dict[str, pd.DataFrame]:
    """SDK 返回形态归一：dict[code, df]、按日期嵌套的 dict[date][code]、或含 MARKET_CODE 的单表。"""
    out: Dict[str, pd.DataFrame] = {}
    requested = {_plain_code(c) for c in batch}
    raw = _flatten_snapshot(raw)
    if isinstance(raw, dict):
        for tgw, df in raw.items():
            code = _plain_code(tgw)
            if code in requested and df is not None and len(df):
                out[code] = df
    elif isinstance(raw, pd.DataFrame) and not raw.empty:
        if "MARKET_CODE" in raw.columns:
            for tgw in batch:
                part = raw[raw["MARKET_CODE"] == tgw]
                if len(part):
                    out[_plain_code(tgw)] = part
        else:
            logger.warning(f"[AmazingData] 返回单表但缺 MARKET_CODE 列，{len(raw)} 行被丢弃")
    return out


@contextmanager
def _temporary_sdk_cache():
    """强制落盘的在线请求独占临时目录，成功或异常均清理。

    请求时解析配置；尾部分隔符用于 SDK 按字符串前缀拼接文件路径。
    不读写共享持久缓存，只删除本次创建的子目录。
    """
    cache_root = _resolve_cache_dir()
    os.makedirs(cache_root, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='amazingdata_sdk_', dir=cache_root) as cache:
        yield cache + os.sep
