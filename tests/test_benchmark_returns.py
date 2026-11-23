from types import SimpleNamespace

import pytest

from data_provider import benchmark_returns, fund_nav


def test_returns_align_with_china_close_without_looking_ahead(monkeypatch):
    friday, monday, tuesday = "2025-01-03", "2025-01-06", "2025-01-07"
    levels = {friday: 100, monday: 110, tuesday: 99}
    monkeypatch.setattr(benchmark_returns, "_index_levels", lambda *_: levels)
    returns = {
        "001821": {"2025-01-04": .001, "2025-01-05": .002, monday: .003},
        "513500": {friday: .02, monday: -.03, tuesday: .10},
        "159934": {monday: -.04, tuesday: .05},
    }
    monkeypatch.setattr(benchmark_returns, "fund_daily_returns", lambda code, *_, **__: returns[code])
    result = benchmark_returns.benchmark_returns(["H00300", *returns], [friday, monday, tuesday])
    assert result["H00300"] == pytest.approx({monday: .10, tuesday: -.10})
    assert result["513500"] == {monday: .02, tuesday: -.03}
    assert result["159934"] == returns["159934"]
    assert result["001821"] == pytest.approx({monday: 1.001 * 1.002 * 1.003 - 1})


def test_official_index_uses_exact_code_and_breaks_on_missing_close(monkeypatch):
    rows = [{"tradeDate": "20250103", "indexCode": "H00300", "close": 100},
            {"tradeDate": "20250106", "indexCode": "000300", "close": 110},
            {"tradeDate": "20250107", "indexCode": "H00300", "close": 99}]
    calls = []

    def request(url, *, params, timeout):
        calls.append(params)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"code": "200", "data": rows})

    monkeypatch.setattr(benchmark_returns.requests, "get", request)
    assert benchmark_returns.benchmark_returns(["H00300"], ["2025-01-03", "2025-01-06", "2025-01-07"]) == {
        "H00300": {}}
    assert calls == [{"indexCode": "H00300", "startDate": "20250103", "endDate": "20250107"}]


def test_money_fund_uses_income_without_changing_its_unit_nav(monkeypatch):
    rows = [{"FSRQ": "2025-01-06", "DWJZ": "0.3348", "JZZZL": "--"},
            {"FSRQ": "2025-01-07", "DWJZ": "-0.0100", "JZZZL": None}]
    data = {"FundType": "005", "Feature": None, "SYType": "每万份收益", "LSJZList": rows}
    monkeypatch.setattr(fund_nav, "_history", lambda *_, **__: {"Data": data, "TotalCount": 2})
    assert fund_nav.fund_daily_returns("001821", "2025-01-06", "2025-01-07", money_fund=True) == pytest.approx({
        "2025-01-06": .3348 / 10_000, "2025-01-07": -.01 / 10_000})
    assert fund_nav.latest_fund_nav("001821", "2025-01-07") == ("2025-01-07", 1.0)
    with pytest.raises(ValueError, match="净值日增长率"):
        fund_nav.fund_daily_returns("001821", "2025-01-06", "2025-01-07")


def test_fund_proxy_uses_disclosed_return_and_paginates(monkeypatch):
    calls = []

    def history(_, **query):
        page = query["pageIndex"]
        calls.append(page)
        row = {"FSRQ": f"2025-01-0{page}", "DWJZ": "1.2", "JZZZL": "-1.23", "FHFCZ": "每份派现金0.1元"}
        return {"Data": {"FundType": "007", "Feature": "", "LSJZList": [row]}, "TotalCount": 2}

    monkeypatch.setattr(fund_nav, "_history", history)
    assert fund_nav.fund_daily_returns("513500", "2025-01-01", "2025-01-02") == pytest.approx({
        "2025-01-01": -.0123, "2025-01-02": -.0123})
    assert calls == [1, 2]
