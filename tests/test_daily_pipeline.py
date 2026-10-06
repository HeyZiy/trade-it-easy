# -*- coding: utf-8 -*-
"""个股日线管道测试 — fetch_stock_daily 走 interface（假 fetcher + 注入日历）。

覆盖：能力筛选、failover、新鲜度拒旧数据切换、end_date=None 宽松口径、
周末收敛、换手率回补、全挂汇总；另附三家真 fetcher 实例化冒烟
（防"类体手术丢方法→抽象类无法实例化"类回归）与 tushare token 参数化。
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from data_provider.daily import fetch_stock_daily
from data_provider.types import DataFetchError, KIND_STOCK_DAILY, Need


def _closes(dates, turnover=None):
    df = pd.DataFrame({"date": dates, "close": [10.0] * len(dates)})
    if turnover is not None:
        df["turnover_rate"] = turnover
    return df


class _FakeFetcher:
    """最小 fetcher 假件：能力声明 + 固定返回/异常。"""

    SUPPORTS = frozenset({(KIND_STOCK_DAILY, "cn")})

    def __init__(self, name, df=None, error=None, columns="full"):
        self.name = name
        self.priority = 0
        self._df = df
        self._error = error
        self._columns = columns
        self.calls = 0

    def supports(self, need):
        return (need.kind, need.market) in self.SUPPORTS

    @property
    def SUPPORTS_COLUMNS(self):
        return None if self._columns == "full" else set(self._columns)

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._df


def _need():
    return Need(KIND_STOCK_DAILY, "600519", "cn")


def _weekend_aware_calendar(d):
    """周末收敛到周五（注入假日历，零 src 触碰）。"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d - timedelta(days=2)
    return d


def test_failover_to_next_source():
    primary = _FakeFetcher("主源", error=ConnectionError("down"))
    backup = _FakeFetcher("备源", df=_closes(["2026-10-05"], turnover=[1.2]))
    df = fetch_stock_daily(_need(), [primary, backup],
                           start_date="2026-09-01", end_date="2026-10-05",
                           latest_trading_day=_weekend_aware_calendar)
    assert list(df["date"]) == ["2026-10-05"]
    assert primary.calls == 1 and backup.calls == 1


def test_staleness_rejects_and_fails_over():
    stale = _FakeFetcher("陈旧源", df=_closes(["2026-10-01"], turnover=[1.0]))
    fresh = _FakeFetcher("新鲜源", df=_closes(["2026-10-05"], turnover=[1.2]))
    df = fetch_stock_daily(_need(), [stale, fresh],
                           start_date="2026-09-01", end_date="2026-10-05",
                           latest_trading_day=_weekend_aware_calendar)
    assert list(df["date"]) == ["2026-10-05"]
    assert stale.calls == 1 and fresh.calls == 1  # 旧数据被拒，切下家


def test_end_date_none_is_explicit_loose():
    """end_date=None = 宽松口径：不检查新鲜度，数据可用（warning 见 caplog）。"""
    source = _FakeFetcher("源", df=_closes(["2026-10-01"]))
    df = fetch_stock_daily(_need(), [source], start_date="2026-09-01",
                           end_date=None,
                           latest_trading_day=_weekend_aware_calendar)
    assert list(df["date"]) == ["2026-10-01"]


def test_saturday_end_date_clamped():
    """end_date 是周六 → 日历收敛到周五；周五收盘的数据不算过期。"""
    source = _FakeFetcher("源", df=_closes(["2026-10-02"]))  # 2026-10-03 是周六
    df = fetch_stock_daily(_need(), [source], start_date="2026-09-01",
                           end_date="2026-10-03",
                           latest_trading_day=_weekend_aware_calendar)
    assert list(df["date"]) == ["2026-10-02"]


def test_no_calendar_skips_check(caplog):
    """未注入日历：检查不可用（warning），数据照常返回。"""
    source = _FakeFetcher("源", df=_closes(["2026-10-01"]))
    with caplog.at_level("WARNING"):
        df = fetch_stock_daily(_need(), [source], start_date="2026-09-01",
                               end_date="2026-10-05", latest_trading_day=None)
    assert list(df["date"]) == ["2026-10-01"]
    assert any("未注入交易日历" in r.message for r in caplog.records)


def test_turnover_backfill_respects_columns_declaration():
    """主源缺换手率 → 从声明该列的备源回补；未声明的源不发无效请求。"""
    primary = _FakeFetcher("主源", df=_closes(["2026-10-05"]))
    declarer = _FakeFetcher("声明源", df=_closes(["2026-10-05"], turnover=[1.5]))
    silent = _FakeFetcher("无列源", df=_closes(["2026-10-05"], turnover=[9.9]),
                          columns={"date", "close"})
    df = fetch_stock_daily(_need(), [primary, declarer, silent],
                           start_date="2026-09-01", end_date="2026-10-05",
                           latest_trading_day=_weekend_aware_calendar)
    assert df["turnover_rate"].tolist() == [1.5]
    assert declarer.calls == 1 and silent.calls == 0


def test_all_fail_raises_summary():
    sources = [_FakeFetcher(f"源{i}", error=DataFetchError("挂")) for i in range(3)]
    with pytest.raises(DataFetchError) as exc:
        fetch_stock_daily(_need(), sources, start_date="2026-09-01",
                          end_date="2026-10-05",
                          latest_trading_day=_weekend_aware_calendar)
    assert all(f"源{i}" in str(exc.value) for i in range(3))


# ── 实例化冒烟：防"类体手术丢方法 → 抽象类无法实例化"类回归 ──

def test_fetchers_instantiate_smoke():
    from data_provider.fetchers.akshare_fetcher import AkshareFetcher
    from data_provider.fetchers.efinance_fetcher import EfinanceFetcher
    from data_provider.fetchers.tushare_fetcher import TushareFetcher
    assert AkshareFetcher().name
    assert EfinanceFetcher().name
    tushare = TushareFetcher(token=None)
    assert tushare._api is None and tushare.priority == 2  # 未供料：默认优先级


def test_tushare_token_parameterization():
    from data_provider.fetchers.tushare_fetcher import TushareFetcher
    fetcher = TushareFetcher(token=None)
    assert fetcher._determine_priority(None) == 2
