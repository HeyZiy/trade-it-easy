"""质量动量在独立测试数据上的持仓资格、重试和状态检查。"""

from copy import deepcopy
from datetime import date

import pandas as pd
import pytest

from src.quality_pool import config, feeds, signal, state


@pytest.fixture
def env(monkeypatch):
    from datetime import timedelta
    import src.trading_calendar as tc
    import quality_pool as entry

    cal = [date(2026, 9, 1) + timedelta(days=i) for i in range(150)]
    cal = [d for d in cal if d.weekday() < 5]
    monkeypatch.setattr(entry, "is_trading_day", lambda day=None: day in cal)
    monkeypatch.setattr(tc, "get_trading_dates",
                        lambda start, end: [d for d in cal if start <= d <= end])
    monkeypatch.setattr(tc, "latest_trading_day_on_or_before",
                        lambda d, **kw: max((x for x in cal if x <= d), default=None))
    monkeypatch.setattr(entry, "save_report", lambda *args, **kwargs: "report.md")
    monkeypatch.setattr(entry, "notify", lambda report: True)
    return monkeypatch


def _patch_signal_data(monkeypatch, universe={"600001", "600002"},
                       closes=None, fail=False):
    import src.quality_pool.feeds as feeds
    if fail:
        def boom(*a, **kw):
            raise RuntimeError("data down")
        monkeypatch.setattr(feeds, "fetch_universe", boom)
        return
    monkeypatch.setattr(feeds, "fetch_universe", lambda d: universe)
    monkeypatch.setattr(feeds, "fetch_closes", lambda codes, asof, rows=253:
                        pd.DataFrame({c: 10.0 for c in codes},
                                     index=pd.bdate_range(end=asof, periods=rows)))
    monkeypatch.setattr(feeds, "raw_closes_at", lambda codes, asof: {c: 10.0 for c in codes})
    monkeypatch.setattr(feeds, "fetch_fundamentals",
                        lambda codes, asof, raw_closes=None, *, min_profit_yoy=None: pd.DataFrame.from_dict(
                            {c: dict(roe_ttm_pct=16.0, np_yoy_pct=20.0,
                                     pe_ttm=15.0, pb=2.0) for c in codes},
                            orient="index"))
    monkeypatch.setattr(feeds, "fetch_screening_status",
                        lambda codes, date: pd.DataFrame(
                            {"is_st": False, "is_suspended": False},
                            index=pd.Index(list(codes), name="code")))
    monkeypatch.setattr(feeds, "fetch_industries", lambda codes, asof: {c: c for c in codes})


def _patch_execute_data(monkeypatch, prices, names=None,
                        suspended=(), st=()):
    """execute 侧 feeds 假数据：真实 assembly.build_snapshot 被测，不在 qp 上打洞。"""
    import src.quality_pool.feeds as feeds
    codes = sorted(prices)
    names = names or {c: f"股{c}" for c in codes}
    status = pd.DataFrame(
        {"is_st": [c in st for c in codes],
         "is_suspended": [c in suspended for c in codes],
         "high_limit": [prices.get(c, 10.0) * 1.1 for c in codes],
         "low_limit": [prices.get(c, 10.0) * 0.9 for c in codes]},
        index=pd.Index(codes, name="code"))
    monkeypatch.setattr(feeds, "fetch_realtime_prices", lambda cs: {c: prices[c] for c in cs if c in prices})
    monkeypatch.setattr('src.quality_pool.assembly.names_for', lambda cs: {c: names.get(c, f"股{c}") for c in cs})
    monkeypatch.setattr(feeds, "raw_closes_at",
                        lambda cs, asof: {c: prices.get(c, 10.0) for c in cs})
    monkeypatch.setattr(feeds, "fetch_execution_status",
                        lambda cs, d: status.loc[[c for c in cs if c in status.index]])


def test_failed_signal_can_retry_from_saved_checkpoint(env):
    checkpoints = []
    current = state.empty_state()

    def persist(value):
        checkpoints.append(deepcopy(value))

    def fail(day):
        raise RuntimeError("data down")

    env.setattr(feeds, "fetch_universe", fail)
    assert "数据异常" in signal.run_signal(date(2026, 10, 2), state=current,
                                           held_codes=set(), state_writer=persist)
    assert checkpoints[-1]["signal_retry"] and checkpoints[-1]["pending_plan"] is None
    _patch_signal_data(env)
    signal.run_signal(date(2026, 10, 2), state=current,
                      held_codes=set(), state_writer=persist)
    assert not checkpoints[-1]["signal_retry"]
    assert checkpoints[-1]["pending_plan"]["selected"] == ["600001", "600002"]


def test_full_portfolio_reviews_only_holdings_and_skips_entry_data(env):
    held = {str(600001 + i) for i in range(config.TOP_N)}
    _patch_signal_data(env, universe=held | {"600099"})
    original = feeds.fetch_fundamentals
    current = state.empty_state()

    def holdings_only(codes, asof, raw_closes=None):
        assert set(codes) == held
        return original(codes, asof, raw_closes)

    env.setattr(feeds, "fetch_fundamentals", holdings_only)
    env.setattr(feeds, "fetch_closes", lambda *a, **kw: pytest.fail("满仓不拉排名行情"))
    report = signal.run_signal(date(2026, 10, 2), state=current,
                               held_codes=held, state_writer=lambda _: None)
    assert "未重算全池排名" in report and signal.POOL_HEADING not in report
    assert set(current["pending_plan"]["kept"]) == held
    assert current["exit_queue"] == {}


def test_full_pool_report_preserves_shortlist_and_lists_unranked(env):
    codes = [str(600001 + i) for i in range(9)]
    _patch_signal_data(env, universe=set(codes))
    closes = feeds.fetch_closes
    env.setattr(feeds, "fetch_closes", lambda *a, **kw: closes(*a, **kw).drop(columns=codes[-1]))
    industry_requests = []

    def industries(requested, asof):
        industry_requests.append(requested)
        return dict.fromkeys(requested, "银行")

    env.setattr(feeds, "fetch_industries", industries)
    env.setattr(signal, "names_for", lambda _: {codes[0]: "样例股票"})
    current = state.empty_state()
    report = signal.run_signal(date(2026, 10, 2), state=current,
                               held_codes=set(), state_writer=lambda _: None)
    table = report.split(signal.POOL_HEADING)[1]
    assert "通过质量筛选 9 只，参与动量排名 8 只" in table
    assert "| 1 | 样例股票（600001） | +0.00% | 拟补位 | 16.0% | +20.0% | 15.00 | 2.00 |" in table
    assert "| 3 | 600003 | +0.00% | 补位范围内·未入选 |" in table
    assert "| 7 | 600007 |" in table and "| — | 600009 |" in table
    assert industry_requests == [codes[:6]]
    assert current["pending_plan"]["selected"] == codes[:config.INDUSTRY_CAP]


def test_ma20_blocks_only_new_entries_inside_original_top_six(env):
    codes = [str(600001 + i) for i in range(8)]
    _patch_signal_data(env, universe=set(codes))
    original = feeds.fetch_closes

    def closes_with_decline(*args, **kwargs):
        closes = original(*args, **kwargs)
        closes.loc[closes.index[-1], codes[:2]] = 9.0
        return closes

    env.setattr(feeds, 'fetch_closes', closes_with_decline)
    env.setattr(feeds, 'fetch_industries', lambda requested, asof: {c: c for c in requested})
    current = state.empty_state()
    report = signal.run_signal(date(2026, 10, 2), state=current,
                               held_codes={codes[0]}, state_writer=lambda _: None)
    assert current['pending_plan']['selected'] == [codes[0], *codes[2:6]]
    assert current['exit_queue'] == {}
    assert f'MA20 弱势暂缓新开仓：{codes[1]}' in report


def test_signal_prefilters_candidate_st_and_missing_status(env):
    _patch_signal_data(env, universe={"600001", "600002", "600003", "600004", "600005"})
    env.setattr(feeds, 'fetch_signal_status', lambda *args: pd.DataFrame(
        {'is_st': pd.array([True, True, False, pd.NA], dtype='boolean')},
        index=['600001', '600002', '600003', '600004']))
    original = feeds.fetch_fundamentals
    calls = []

    def fetch(codes, asof, raw_closes=None, *, min_profit_yoy=None):
        calls.append((codes, min_profit_yoy))
        return original(codes, asof, raw_closes, min_profit_yoy=min_profit_yoy)

    env.setattr(feeds, 'fetch_fundamentals', fetch)
    current = state.empty_state()
    signal.run_signal(date(2026, 10, 2), state=current,
                      held_codes={'600001'}, state_writer=lambda _: None)

    assert calls == [(['600001'], None), (['600003'], config.NP_YOY_MIN)]
    assert current['exit_queue'] == {'600001': 'out_of_pool'}
    assert current['pending_plan']['selected'] == ['600003']


def test_state_rejects_incompatible_version():
    with pytest.raises(ValueError, match="策略状态文件版本不兼容"):
        state.restore({"version": 2, "exit_queue": {"600001": "out_of_pool"}})
