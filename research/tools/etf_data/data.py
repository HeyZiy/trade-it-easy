# -*- coding: utf-8 -*-
"""纯本地 ETF 研究数据。窗口默认排除截止日，信号价格按截止日归一化。"""
from pathlib import Path
import json

import numpy as np
import pandas as pd

DEFAULT_CACHE = Path(__file__).resolve().parent / "_cache"
PRICE_FIELDS = ("open", "high", "low", "close")
SCHEMA_VERSION = 2


class LocalEtfData:
    """数据缓存读取器，不模拟平台撮合，也不把缺行情断言为真实停牌。"""

    def __init__(self, cache_dir=None):
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE
        manifest_path = self.cache_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError("请先运行 python -m research.tools.etf_data.fetch")
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("旧缓存不含可信复权信息，请用 etf_data.fetch --rebuild 迁移")
        self.frames = {}
        self.securities_table = pd.read_csv(self.cache_dir / "etf_securities.csv",
                                             dtype={"code": str}).set_index("code")
        self.calendar = pd.Index(pd.read_csv(self.cache_dir / "calendar.csv")["date"].astype(str))
        if self.calendar.has_duplicates or not self.calendar.is_monotonic_increasing:
            raise ValueError("交易日历必须升序且无重复")
        self._frame("close")

    def _frame(self, field):
        field = "amount" if field == "money" else field
        if field not in (*PRICE_FIELDS, "volume", "amount", "quality",
                         *(f"hfq_{f}" for f in PRICE_FIELDS)):
            raise ValueError(f"不支持的字段 {field!r}")
        if field not in self.frames:
            df = pd.read_csv(self.cache_dir / f"wide_{field}.csv", index_col="date",
                             dtype=str if field == "quality" else None, low_memory=False)
            df.index = df.index.astype(str)
            if not df.index.equals(self.calendar) or df.columns.has_duplicates:
                raise ValueError(f"{field} 宽表与日历不一致或标的重复")
            if set(df.columns) != set(self.securities_table.index):
                raise ValueError(f"{field} 宽表与标的资料不一致")
            self.frames[field] = df
        return self.frames[field]

    def trading_days(self):
        return self.calendar.copy()

    def _loc(self, date):
        date = self.calendar[-1] if date is None else pd.Timestamp(date).strftime("%Y-%m-%d")
        loc = self.calendar.searchsorted(date, side="right") - 1
        if loc < 0:
            raise KeyError(f"{date} 早于缓存首日 {self.calendar[0]}")
        if date > self.calendar[-1]:
            raise KeyError(f"{date} 超出缓存截止日 {self.calendar[-1]}")
        return loc

    def _series(self, field, code):
        return self._frame(field).reindex(columns=[code])[code]

    def history(self, count, field, security_list=None, end_date=None, *,
                include_end=False, adjust="pre"):
        """最近 count 个交易日，默认不含截止日；缺记录保留 NaN，不填零。

        adjust='pre'：后复权价格 × 截止日原始收盘/后复权收盘；
        adjust='hfq'：供应商后复权；adjust='raw'：原始价格。
        非价格字段不复权。休市日期自动定位其前一交易日。
        """
        if not isinstance(count, (int, np.integer)) or count <= 0:
            raise ValueError("count 必须是正整数")
        if adjust not in ("pre", "hfq", "raw"):
            raise ValueError("adjust 可选 pre / hfq / raw")
        field = "amount" if field == "money" else field
        loc = self._loc(end_date)
        stop = loc + 1 if include_end else loc
        codes = list(self.securities_table.index) if security_list is None else list(security_list)
        if len(codes) != len(set(codes)):
            raise ValueError("security_list 中有重复代码")
        source_field = f"hfq_{field}" if field in PRICE_FIELDS and adjust != "raw" else field
        if field in PRICE_FIELDS and adjust != "raw":
            hfq_close = self._frame("hfq_close")
            unavailable = [c for c in codes if c in hfq_close and not hfq_close[c].notna().any()]
            if unavailable:
                raise ValueError("缺少供应商复权行情，请重新获取：" + " ".join(unavailable[:10]))
        out = self._frame(source_field).iloc[max(0, stop - count):stop].reindex(columns=codes).copy()
        quality = self._frame("quality").reindex(index=out.index, columns=codes)
        if field != "quality":
            out = out.where(quality.isin(["ok", "adjustment_missing"]) if adjust == "raw"
                            or field not in PRICE_FIELDS else quality.eq("ok"))
        if field in PRICE_FIELDS and adjust == "pre":
            # 只访问截止日的基准；未来权益变化不会改变既有窗口的价格比值。
            raw = self._frame("close").iloc[loc].reindex(codes)
            hfq = self._frame("hfq_close").iloc[loc].reindex(codes)
            anchor_quality = self._frame("quality").iloc[loc].reindex(codes)
            out = out.mul((raw / hfq).where((hfq > 0) & anchor_quality.eq("ok")), axis=1)
        return out

    def attribute_history(self, code, count, fields=("close",), end_date=None, *,
                          include_end=False, adjust="pre", skip_missing=False):
        """单标的多字段窗口；skip_missing 仅跳过缺行，不代表平台 skip_paused。"""
        if not isinstance(count, (int, np.integer)) or count <= 0:
            raise ValueError("count 必须是正整数")
        fields = [fields] if isinstance(fields, str) else list(fields)
        loc = self._loc(end_date)
        requested = loc + int(include_end) if skip_missing else count
        requested = max(1, requested)
        out = pd.DataFrame({f: self.history(requested, f, [code], end_date,
                              include_end=include_end, adjust=adjust)[code] for f in fields})
        if skip_missing:
            out = out.dropna(how="all")
        return out.tail(count)

    def all_securities(self, date=None):
        """首根行情日代理上市日；真正退市日仅在资料提供时用于过滤。"""
        sec = self.securities_table
        if date is not None:
            day = pd.Timestamp(date).strftime("%Y-%m-%d")
            sec = sec[sec.start_date.notna() & (sec.start_date <= day)]
            if "delist_date" in sec:
                sec = sec[sec.delist_date.isna() | (sec.delist_date > day)]
        return sec.copy()

    def get_security_name(self, code):
        name = self.securities_table.display_name.get(code)
        return code if pd.isna(name) else str(name)

    def data_status(self, code, date):
        """ok / adjustment_missing / zero_activity / invalid_price / missing。"""
        if date not in self.calendar or code not in self.securities_table.index:
            return "missing"
        value = self._series("quality", code).at[date]
        return "missing" if pd.isna(value) else str(value)

    def is_tradable(self, code, date):
        return self.data_status(code, date) in ("ok", "adjustment_missing")

    def paused(self, code, date):
        """保守的不可交易标志；缺数据、异常数据都不可交易，并非确证停牌。"""
        return not self.is_tradable(code, date)

    def last_price(self, code, date=None, *, adjust="raw"):
        """截止日价格；默认原始成交价，未知代码返回浮点 NaN。"""
        if date is None:
            date = self.calendar[-1]
        if date not in self.calendar:
            return float("nan")
        values = self.history(1, "close", [code], date, include_end=True, adjust=adjust)
        return float(values.iloc[-1, 0])

    def index_close(self, code="000300"):
        df = pd.read_csv(self.cache_dir / f"index_{code}.csv", usecols=["date", "close"])
        return df.set_index("date")["close"].reindex(self.calendar)
