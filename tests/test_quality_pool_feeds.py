# -*- coding: utf-8 -*-
"""质量池取数装配测试 — 财务可见性/口径换算/前复权（feeds.py）。"""

import numpy as np
import pandas as pd
import pytest

from src.quality_pool import feeds

ASOF = "2026-09-30"


def test_industries_use_provider_and_preserve_asof_membership(monkeypatch, tmp_path):
    from pathlib import Path
    from data_provider.fetchers import amazingdata_info as provider

    paths, queried = [], []
    frames = {
        'A': pd.DataFrame({'CON_CODE': ['600001.SH', '600002.SH', '600003.SH'],
                           'INDATE': ['2020-01-01'] * 3,
                           'OUTDATE': ['2026-10-01', None, None]}),
        'B': pd.DataFrame({'CON_CODE': ['600001.SH', '600003.SH'],
                           'INDATE': ['2026-10-01', '2020-01-01'],
                           'OUTDATE': [None, None]}),
    }

    def write_cache(local_path):
        cache = Path(local_path)
        paths.append(cache)
        (cache / 'sdk.h5').write_bytes(b'cache')

    class SDK:
        def get_industry_base_info(self, local_path, is_local):
            assert is_local is False
            write_cache(local_path)
            return pd.DataFrame({'INDEX_CODE': ['A', 'B', 'C'], 'LEVEL_TYPE': [1, 1, 2]})

        def get_industry_constituent(self, indices, local_path, is_local):
            assert is_local is False
            assert len(indices) == 1
            index = indices[0]
            queried.append(index)
            write_cache(local_path)
            # Real SDK constituent responses may be dictionaries of frames.
            return {index: frames[index]}

    monkeypatch.setenv('AMAZING_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(provider, '_info', lambda: SDK())
    assert feeds.fetch_industries(['600001', '600002', '600003'], ASOF) == {
        '600001': 'A', '600002': 'A'}
    assert queried == ['A', 'B']
    assert len(set(paths)) == 3 and all(not path.exists() for path in paths)
    assert list(tmp_path.iterdir()) == []


def _income_rows(rows) -> pd.DataFrame:
    """rows: (reporting_period, ann_date, actual_ann, stmt_type, net_profit)。"""
    frame = pd.DataFrame(rows, columns=[
        "REPORTING_PERIOD", "ANN_DATE", "ACTUAL_ANN_DATE",
        "STATEMENT_TYPE", "NET_PRO_EXCL_MIN_INT_INC"])
    frame['NET_PRO_INCL_MIN_INT_INC'] = frame['NET_PRO_EXCL_MIN_INT_INC']
    return frame


def _balance_rows(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=[
        "REPORTING_PERIOD", "ANN_DATE", "ACTUAL_ANN_DATE",
        "STATEMENT_TYPE", "TOT_SHARE_EQUITY_EXCL_MIN_INT"])


def _structure_rows(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["CHANGE_DATE", "TOT_SHARE"])


QUARTERS = [
    # 最近五个单季度（累计披露已差分），单位：元
    ("20250630", "2025-08-20", None, 2, 80.0),    # 去年同季（2026Q2 的同比基数）
    ("20250930", "2025-10-28", None, 2, 90.0),
    ("20251231", "2026-03-28", None, 2, 110.0),
    ("20260331", "2026-04-25", None, 2, 120.0),
    ("20260630", "2026-08-20", None, 2, 130.0),
]


def _patch_frames(monkeypatch, income=None, balance=None, structure=None,
                  kline=None, factors=None):
    monkeypatch.setattr(feeds, '_calendar_dates', lambda begin, end: pd.bdate_range(begin, end))
    if income is not None:
        monkeypatch.setattr(feeds, "_raw_income", lambda codes, b, e: {"600001": income})
    if balance is not None:
        monkeypatch.setattr(feeds, "_raw_balance_sheets",
                            lambda codes, b, e: {"600001": balance})
    if structure is not None:
        monkeypatch.setattr(feeds, "_raw_equity_structure",
                            lambda codes, b, e: {"600001": structure})
    if kline is not None:
        monkeypatch.setattr(feeds, "_raw_kline", lambda codes, b, e: kline)
    if factors is not None:
        monkeypatch.setattr(feeds, "_raw_adj_factors", lambda codes, b, e: factors)


def _balance():
    return _balance_rows([
        ("20260630", "2026-08-20", None, 1, 20_000.0),
        ("20260331", "2026-04-25", None, 1, 19_000.0),
    ])


def _structure():
    return _structure_rows([("2020-01-01", 10_000.0)])   # 万股


def test_fundamentals_core_ratios(monkeypatch):
    _patch_frames(monkeypatch, income=_income_rows(QUARTERS),
                  balance=_balance(), structure=_structure())
    frame = feeds.fetch_fundamentals(
        ["600001"], ASOF, raw_closes={"600001": 20.0})
    row = frame.loc["600001"]
    assert row["roe_ttm_pct"] == pytest.approx(450.0 / 20_000.0 * 100)
    assert row["np_yoy_pct"] == pytest.approx(130.0 / 80.0 * 100 - 100)
    # TTM = 110+120+130+90 = 450；总市值 = 10_000万股×1e4×20元 = 2e9
    assert row["pe_ttm"] == pytest.approx(2e9 / 450.0)
    assert row["pb"] == pytest.approx(2e9 / 20_000.0)


def test_unpublished_quarter_invisible(monkeypatch):
    """披露日晚于截止日的报告期不可见（财报未披露 → 同比算不出来 → 剔除）。"""
    rows = list(QUARTERS[:-1]) + [("20260630", "2026-10-01", None, 2, 130.0)]
    _patch_frames(monkeypatch, income=_income_rows(rows),
                  balance=_balance(), structure=_structure())
    frame = feeds.fetch_fundamentals(["600001"], ASOF, raw_closes={"600001": 20.0})
    assert frame.isna().any(axis=1).all()   # 2026Q2 未披露 → 最新单季为 2026Q1，去年同季缺失 → 不可比


def test_correction_keeps_latest_disclosure(monkeypatch):
    """同一报告期多次披露（更正）取最晚披露行。"""
    rows = list(QUARTERS) + [("20260630", "2026-08-25", None, 2, 140.0)]
    _patch_frames(monkeypatch, income=_income_rows(rows),
                  balance=_balance(), structure=_structure())
    frame = feeds.fetch_fundamentals(["600001"], ASOF, raw_closes={"600001": 20.0})
    assert frame.loc["600001"]["roe_ttm_pct"] == pytest.approx(
        460.0 / 20_000.0 * 100)


def test_cumulative_statement_type_ignored(monkeypatch):
    """STATEMENT_TYPE=1（累计口径）的利润行不进单季序列。"""
    rows = list(QUARTERS) + [("20260630", "2026-08-20", None, 1, 999.0)]
    _patch_frames(monkeypatch, income=_income_rows(rows),
                  balance=_balance(), structure=_structure())
    frame = feeds.fetch_fundamentals(["600001"], ASOF, raw_closes={"600001": 20.0})
    assert frame.loc["600001"]["roe_ttm_pct"] == pytest.approx(
        450.0 / 20_000.0 * 100)


def test_negative_prior_year_growth_uses_absolute_base(monkeypatch):
    rows = [("20250630", "2025-08-20", None, 2, -90.0)] + list(QUARTERS[1:])
    _patch_frames(monkeypatch, income=_income_rows(rows),
                  balance=_balance(), structure=_structure())
    frame = feeds.fetch_fundamentals(["600001"], ASOF, raw_closes={"600001": 20.0})
    assert frame.loc['600001', 'np_yoy_pct'] == pytest.approx((130. + 90.) / 90. * 100.)


def test_growth_uses_consolidated_profit_independently_of_parent_profit(monkeypatch):
    income = _income_rows(QUARTERS)
    income.loc[0, 'NET_PRO_INCL_MIN_INT_INC'] = 200.
    income.loc[4, 'NET_PRO_INCL_MIN_INT_INC'] = 300.
    _patch_frames(monkeypatch, income=income, balance=_balance(), structure=_structure())
    row = feeds.fetch_fundamentals(['600001'], ASOF, raw_closes={'600001': 20.}).loc['600001']
    assert row['np_yoy_pct'] == pytest.approx(50.)
    assert row['roe_ttm_pct'] == pytest.approx(450. / 20_000. * 100.)


@pytest.mark.parametrize('case', ['field_missing', 'latest_missing', 'zero_base'])
def test_growth_missing_data_does_not_fall_back_to_parent_or_older_quarter(monkeypatch, case):
    income = _income_rows(QUARTERS)
    if case == 'field_missing':
        income = income.drop(columns='NET_PRO_INCL_MIN_INT_INC')
    elif case == 'latest_missing':
        income.loc[4, 'NET_PRO_INCL_MIN_INT_INC'] = np.nan
    else:
        income.loc[0, 'NET_PRO_INCL_MIN_INT_INC'] = 0.
    _patch_frames(monkeypatch, income=income, balance=_balance(), structure=_structure())
    row = feeds.fetch_fundamentals(['600001'], ASOF, raw_closes={'600001': 20.}).loc['600001']
    assert pd.isna(row['np_yoy_pct'])
    assert pd.notna(row['roe_ttm_pct'])


def test_missing_latest_parent_profit_does_not_use_older_ttm(monkeypatch):
    income = _income_rows(QUARTERS)
    income.loc[4, 'NET_PRO_EXCL_MIN_INT_INC'] = np.nan
    _patch_frames(monkeypatch, income=income, balance=_balance(), structure=_structure())
    row = feeds.fetch_fundamentals(['600001'], ASOF, raw_closes={'600001': 20.}).loc['600001']
    assert pd.isna(row['roe_ttm_pct']) and pd.isna(row['pe_ttm'])
    assert row['np_yoy_pct'] == pytest.approx(62.5)


def test_negative_ttm_yields_negative_pe_passing_upper_bound(monkeypatch):
    rows = list(QUARTERS[:1]) + [
        ("20250930", "2025-10-28", None, 2, 90.0),
        ("20251231", "2026-03-28", None, 2, -400.0),
        ("20260331", "2026-04-25", None, 2, 120.0),
        ("20260630", "2026-08-20", None, 2, 130.0)]
    _patch_frames(monkeypatch, income=_income_rows(rows),
                  balance=_balance(), structure=_structure())
    frame = feeds.fetch_fundamentals(["600001"], ASOF, raw_closes={"600001": 20.0})
    # TTM = 90-400+120+130 = -60 → PE 为负（无下界，可通过）
    assert frame.loc["600001"]["pe_ttm"] < 0


def test_missing_balance_sheet_excluded(monkeypatch):
    _patch_frames(monkeypatch, income=_income_rows(QUARTERS),
                  balance=_balance_rows([]), structure=_structure())
    frame = feeds.fetch_fundamentals(["600001"], ASOF, raw_closes={"600001": 20.0})
    assert frame.isna().any(axis=1).all()


def test_missing_ttm_quarter_cannot_make_incomplete_pe(monkeypatch):
    rows = [r for r in QUARTERS if r[0] != "20250930"]
    _patch_frames(monkeypatch, income=_income_rows(rows),
                  balance=_balance(), structure=_structure())
    frame = feeds.fetch_fundamentals(["600001"], ASOF, raw_closes={"600001": 20.0})
    assert frame.isna().any(axis=1).all()


def test_fundamentals_batches_release_raw_frames_and_preserve_old_shares(monkeypatch):
    import weakref
    codes = [f"{600001 + i}" for i in range(70)]
    refs = []
    calls = []

    def income(batch, begin, end):
        # 上一批函数已返回；全市场累积原表的写法会使此断言失败。
        assert all(ref() is None for ref in refs)
        assert begin == "2024-07-01" and end == ASOF
        calls.append(batch)
        frames = {c: _income_rows(QUARTERS) for c in batch}
        refs.extend(weakref.ref(df) for df in frames.values())
        return frames

    def structure(batch, begin, end):
        assert begin == "1990-01-01" and end == ASOF
        return {c: _structure() for c in batch}  # 2020 年股本仍有效

    monkeypatch.setattr(feeds, "_raw_income", income)
    monkeypatch.setattr(feeds, "_raw_balance_sheets",
                        lambda batch, b, e: {c: _balance() for c in batch})
    monkeypatch.setattr(feeds, "_raw_equity_structure", structure)
    frame = feeds.fetch_fundamentals(codes, ASOF, raw_closes={c: 20.0 for c in codes})
    assert list(frame.index) == codes
    assert [len(batch) for batch in calls] == [32, 32, 6]
    assert all(ref() is None for ref in refs)
    assert frame.loc["600001", "pb"] == pytest.approx(2e9 / 20_000.0)


# ── 前复权收盘 ──

def _kline_fixture(dates, closes):
    return {"600001": pd.DataFrame({
        "code": "600001.SH", "kline_time": dates,
        "close": closes, "volume": 1000, "amount": 10000.0})}


def test_closes_adjusted_and_windowed(monkeypatch):
    n = 140
    dates = pd.date_range(end=ASOF, periods=n, freq="B")
    raw = np.linspace(10.0, 12.0, n)
    factors = pd.DataFrame({"600001.SH": np.linspace(1.0, 2.0, n)},
                           index=dates)
    _patch_frames(monkeypatch,
                  kline=_kline_fixture(dates, raw), factors=factors)
    closes = feeds.fetch_closes(["600001"], ASOF, rows=121)
    assert len(closes) == 121
    # 前复权 = 原始 close × 因子；窗口末行 = 12.0 × 2.0
    assert closes["600001"].iloc[-1] == pytest.approx(24.0)
    # 截止日为 asof
    assert closes.index[-1] == pd.Timestamp(ASOF).normalize()
    # 逐行等于 原始 close × 复权因子（归一化是常数缩放，不影响分数）
    expected = np.asarray(raw[-121:]) * np.asarray(factors["600001.SH"][-121:])
    assert np.allclose(closes["600001"].to_numpy(), expected)


def test_closes_missing_factor_drops_code(monkeypatch):
    dates = pd.date_range(end=ASOF, periods=130, freq="B")
    _patch_frames(monkeypatch, kline=_kline_fixture(dates, [10.0] * 130),
                  factors=pd.DataFrame())
    closes = feeds.fetch_closes(["600001"], ASOF)
    assert closes.empty


# ── 状态与解禁 ──

def test_status_normalization(monkeypatch):
    raw = pd.DataFrame({
        "MARKET_CODE": ["600001.SS", "600002.SS"],
        "TRADE_DATE": [ASOF, ASOF],
        "IS_ST_SEC": ["1", "0"],
        "IS_SUSP_SEC": ["0", "1"],
        "PRECLOSE": [10.0, np.nan],
        "HIGH_LIMITED": [11.0, np.nan],
        "LOW_LIMITED": [9.0, np.nan],
    })
    monkeypatch.setattr(feeds, "_raw_status", lambda codes, date: raw)
    status = feeds.fetch_status(["600001", "600002"], ASOF)
    assert bool(status.at["600001", "is_st"]) is True
    assert bool(status.at["600002", "is_suspended"]) is True
    assert status.at["600001", "high_limit"] == pytest.approx(11.0)


def test_unlock_codes_collected(monkeypatch):
    raw = pd.DataFrame({"MARKET_CODE": ["600001.SS", "600002.SS", "600003.SS"],
                        "LIST_DATE": ["2026-10-10", "2027-01-05", "2026-10-05"],
                        "SHARE_RATIO": [1.0, 2.0, 0.5]})
    monkeypatch.setattr(feeds, "_raw_restricted", lambda codes, b, e: raw)
    # 90 自然日窗口：2026-10-01 ~ 2026-12-30 → 600001/600003 命中，600002 窗外
    unlocks = feeds.fetch_unlock_codes(["600001", "600002", "600003"], "2026-10-01")
    assert unlocks == {"600001", "600003"}


def test_day_limits_missing_official_values_do_not_invent_ten_percent(monkeypatch):
    monkeypatch.setattr(feeds, "fetch_execution_status", lambda codes, date: pd.DataFrame())
    limits = feeds.day_limits(["600001"], "2026-10-01", {"600001": 10.0})
    assert limits == {}
