"""观察列的时点、通道状态与报告集成，不访问网络或真实台账。"""

import pandas as pd
import pytest

from src.etf import allocation as eo
from src.etf.trend import donchian_observation
from src.etf.rebalancer import RebalancePlan


def history():
    dates = pd.bdate_range("2026-08-03", periods=26)
    bars = pd.DataFrame({"date": dates, "hfq_high": 11., "hfq_low": 9., "hfq_close": 10.})
    bars.loc[20, ["hfq_high", "hfq_low", "hfq_close"]] = [13., 10., 12.]
    bars.loc[23, ["hfq_high", "hfq_low", "hfq_close"]] = [10., 7., 8.]
    bars.loc[24, ["hfq_high", "hfq_low", "hfq_close"]] = [14., 10., 13.]
    return bars


def at_close(bars):
    return pd.Timestamp(bars.date.iloc[-1]).to_pydatetime().replace(hour=16)


def test_state_replays_breakout_persists_inside_channel_and_exits():
    bars = history()
    held = donchian_observation(bars.iloc[:23], now=at_close(bars.iloc[:23]))
    assert held["state"] is True
    assert held["last_signal"] == bars.date.iloc[20].strftime("%Y-%m-%d")
    # 第24行收盘恰等于此前20日最高价13，不重新入场。
    exited = donchian_observation(bars, now=at_close(bars))
    assert exited["state"] is False
    assert exited["last_signal"] == bars.date.iloc[23].strftime("%Y-%m-%d")


def test_intraday_and_future_rows_cannot_trigger_close_signal():
    bars = history()
    bars.loc[25, ["hfq_high", "hfq_low", "hfq_close"]] = [16., 14., 15.]
    now = at_close(bars)
    intraday = donchian_observation(bars, now=now.replace(hour=14))
    assert intraday["state"] is False
    assert intraday["asof"] == bars.date.iloc[24].strftime("%Y-%m-%d")
    assert donchian_observation(bars, now=now)["state"] is True
    assert donchian_observation(bars, now=at_close(bars.iloc[:23]))["state"] is True


def test_no_breakout_is_unknown_and_bad_prices_are_not_a_sell_signal():
    bars = history().iloc[:21].copy()
    bars.loc[20, ["hfq_high", "hfq_low", "hfq_close"]] = [11., 9., 10.]
    assert donchian_observation(bars, now=at_close(bars))["state"] is None
    bars.loc[20, "hfq_high"] = float("nan")
    with pytest.raises(ValueError, match="OHLC无效"):
        donchian_observation(bars, now=at_close(bars))
    with pytest.raises(ValueError, match="不足21日"):
        donchian_observation(bars.iloc[:20], now=at_close(bars))


def test_fetch_failure_is_isolated_and_uses_each_configured_etf(monkeypatch):
    from data_provider import bars as provider

    calls = []

    def fetch(code, end):
        calls.append(code)
        if code == "563360":
            raise RuntimeError("unavailable")
        return history()

    monkeypatch.setattr(provider, "get_etf_daily_em", fetch)
    observations = eo._fetch_trend_observations()
    assert observations["563360"]["state"] is None
    assert observations["513180"]["state"] is False
    assert "513380" not in calls
    assert "CASH" not in calls


def test_report_columns_share_snapshot_and_do_not_change_target_or_plan(monkeypatch):
    target = {asset.code: asset.neutral_weight for asset in eo.CORE_BASELINE}
    plan = RebalancePlan([], 0., False, "无指令", [])
    alloc = {"positions": [], "total_assets": 100_000., "rotation_mv": 0.,
             "rotation_positions": [], "core_positions": [], "target": target, "plan": plan}
    valuations = [{"code": "515180", "name": "红利ETF", "pe": 8.,
                   "div_yield": 5., "div_yield_spread": 3., "source_name": "中证红利"}]
    ctx = eo.WeekContext(None, None, None, alloc,
                         trends={"513180": {"state": False, "asof": "2026-10-14"}},
                         valuations=valuations)

    def unexpected_fetch(*args, **kwargs):
        raise AssertionError("报告渲染不应重新取数")

    monkeypatch.setattr(eo, "_raw_valuation_rows", unexpected_fetch)
    output = eo._holding_overview(ctx)
    assert "| 唐奇安20/10 | 估值 |" in output
    assert "**× ★**" in output
    assert "513180" in output and "重点观察规则" in output
    assert "513380" not in output
    assert "2026-10-14 收盘" in output
    assert "PE 8.0 / 股息 5.0% / 利差 +3.0pt（偏低）" in output
    assert "中证红利" in eo._valuation_reference(ctx)
    assert alloc["target"]["513180"] == .05
    assert alloc["plan"] is plan and plan.orders == []


def test_pe_alone_never_becomes_cheap_or_buy():
    assert eo._valuation_cell({"pe": 8.}) == "PE 8.0"
    assert eo._valuation_cell({}) == "—"
