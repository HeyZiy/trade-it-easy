"""T−1 收盘估值及看板主动补价的回归测试。"""

import base64
from datetime import date, datetime
from http.server import ThreadingHTTPServer
import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from uuid import uuid4

import pytest

import dashboard
from src import account_valuation, etf_account as wallet, trading_calendar
from src.dashboard_quotes import QuoteRefresh
from data_provider.closing_prices import ClosingPrices

TODAY = date(2025, 1, 7)
PRICE_DAY = "2025-01-06"


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(wallet, "_today", lambda: TODAY)
    path = tmp_path / "data" / "etf_account.json"
    wallet.initialize(path, asof="2025-01-02", cash=10000,
                      positions=[dict(code="588000", count=1000, price=2, strategy="core")],
                      targets={"588000": .6, "159915": .4}, budgets={"core": 1},
                      cash_allocations={"core": 10000})
    wallet.collect_close(path, PRICE_DAY, lambda *_: {"588000": 3})
    return path


def buy(path, code="510300", price=4):
    return wallet.record_event(path, dict(
        request_id=str(uuid4()), revision=wallet.revision(wallet.load(path)),
        date=TODAY.isoformat(), side="buy", strategy="core", code=code, qty=100, price=price, fee=1,
    ))


def wait_finished(refresh, path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = refresh.read(path)
        if not state.get("running") and not state.get("funds", {}).get("running"):
            return state
        time.sleep(.01)
    pytest.fail("后台刷新未结束")


def test_valuation_day_uses_confirmed_calendar_for_weekends_and_holidays(monkeypatch):
    monkeypatch.setattr(account_valuation, "latest_trading_day_on_or_before",
                        trading_calendar.latest_trading_day_on_or_before)
    dates = [date(2025, 1, 24), date(2025, 1, 27), date(2025, 2, 5)]
    monkeypatch.setattr(trading_calendar, "get_trading_dates",
                        lambda start, end: [day for day in dates if start <= day <= end])
    assert wallet.valuation_day(date(2025, 1, 25)) == "2025-01-24"
    assert wallet.valuation_day(date(2025, 2, 5)) == "2025-01-27"
    assert wallet.valuation_day(date(2025, 2, 6)) == "2025-02-05"
    monkeypatch.setattr(trading_calendar, "get_trading_dates", lambda *_: [])
    assert wallet.valuation_day(TODAY) is None


def test_calendar_recovers_after_temporary_failure(monkeypatch):
    import sys

    import pandas as pd

    now = [0]

    def download():
        if now[0] == 0:
            raise TimeoutError("calendar unavailable")
        return pd.DataFrame({"trade_date": [date.fromisoformat(PRICE_DAY)]})

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(tool_trade_date_hist_sina=download))
    monkeypatch.setattr(trading_calendar, "monotonic", lambda: now[0])
    monkeypatch.setattr(trading_calendar, "_refresh_after", float("-inf"))
    monkeypatch.setattr(trading_calendar, "_trading_dates", [])
    monkeypatch.setattr(account_valuation, "latest_trading_day_on_or_before",
                        trading_calendar.latest_trading_day_on_or_before)
    assert wallet.valuation_day(TODAY) is None
    now[0] = 59
    assert wallet.valuation_day(TODAY) is None
    now[0] = 60
    assert wallet.valuation_day(TODAY) == PRICE_DAY


def test_valuation_day_takes_same_day_after_evening_cutoff():
    evening = datetime(2025, 1, 7, 18, 30, tzinfo=trading_calendar.SHANGHAI)
    assert wallet.valuation_day(TODAY, now=evening) == TODAY.isoformat()
    before = datetime(2025, 1, 7, 18, 29, tzinfo=trading_calendar.SHANGHAI)
    assert wallet.valuation_day(TODAY, now=before) == PRICE_DAY
    saturday = datetime(2025, 1, 11, 19, 0, tzinfo=trading_calendar.SHANGHAI)
    assert wallet.valuation_day(date(2025, 1, 11), now=saturday) == "2025-01-10"


def test_evening_refresh_and_display_switch_to_same_day(account, monkeypatch):
    evening = datetime(2025, 1, 7, 18, 30, tzinfo=trading_calendar.SHANGHAI)
    monkeypatch.setattr(wallet, "_now", lambda: evening)
    calls = []

    def fetch(self, codes, day):
        calls.append((codes, day))
        return {"588000": 5, "159915": 2}

    monkeypatch.setattr(ClosingPrices, "close_prices", fetch)
    refresh = QuoteRefresh()
    refresh.start(account, TODAY, now=evening)
    state = wait_finished(refresh, account)
    assert state["date"] == TODAY.isoformat() and state["error"] is None
    assert calls == [(["159915", "588000"], TODAY.isoformat())]
    view = wallet.account_data(account)
    assert view["valuation"] == {"date": TODAY.isoformat(), "missing": []}
    assert view["equity"] == 15000


def test_evening_scheduled_report_collects_same_day(account, monkeypatch):
    import portfolio_report as entry
    days = [date.fromisoformat(PRICE_DAY), TODAY]
    monkeypatch.setattr(entry, "get_trading_dates", lambda start, end: [d for d in days if start <= d <= end])
    monkeypatch.setattr("src.etf_account.ACCOUNT_PATH", account)
    monkeypatch.setattr(entry.allocation_report, "publish", lambda *args, **kwargs: True)
    calls = []

    def fetch(self, codes, day):
        calls.append((codes, day))
        return {code: 3 for code in codes}

    monkeypatch.setattr(ClosingPrices, "close_prices", fetch)
    report, errors = entry.run(now=datetime(2025, 1, 7, 18, 30, tzinfo=entry.SHANGHAI), backfill=True)
    assert errors == 0
    assert all(day == TODAY.isoformat() for _, day in calls)
    state = wallet.load(account)
    assert state["records"][-1]["date"] == TODAY.isoformat()
    assert state["valuation_closes"]["date"] == TODAY.isoformat()


def test_scheduled_report_collects_targets_and_current_holdings_for_valuation(account, monkeypatch):
    import portfolio_report as entry

    buy(account)
    days = [date.fromisoformat(PRICE_DAY), TODAY]
    monkeypatch.setattr(entry, "get_trading_dates", lambda start, end: [d for d in days if start <= d <= end])
    monkeypatch.setattr("src.etf_account.ACCOUNT_PATH", account)
    calls = []

    def fetch(self, codes, day):
        calls.append((codes, day))
        return {code: 3 for code in codes}

    monkeypatch.setattr(ClosingPrices, "close_prices", fetch)
    report, errors = entry.run(now=datetime(2025, 1, 7, 8, 30, tzinfo=entry.SHANGHAI), backfill=True)
    assert errors == 0 and "总资产 12,899.00 元" in report
    assert calls == [(["588000"], PRICE_DAY), (["159915", "510300"], PRICE_DAY)]
    state = wallet.load(account)
    assert state["records"][-1]["closing_prices"] == {"588000": 3}
    assert state["valuation_closes"]["prices"]["510300"] == 3


def test_new_holding_is_pending_even_with_trade_price_and_today_close(account):
    buy(account)
    wallet.collect_close(account, TODAY.isoformat(), lambda *_: {"588000": 5, "510300": 6})
    view = wallet.account_data(account)
    assert view["valuation"] == {"date": PRICE_DAY, "missing": ["510300"]}
    assert {row["code"]: row["market_value"] for row in view["positions"]} == {
        "588000": 3000, "510300": None,
    }
    assert view["cash"] == 9599 and view["equity"] is None
    assert not view["strategies"][0]["suggestions"]
    with pytest.raises(ValueError, match="待估值"):
        wallet.cash_rebalance(account, {"revision": view["revision"]})


def test_same_date_refresh_uses_latest_account_and_keeps_partial_success(account, monkeypatch):
    entered, released = threading.Event(), threading.Event()
    calls = []

    def fetch(self, codes, day):
        calls.append((codes, day))
        if len(calls) == 1:
            entered.set()
            assert released.wait(5)
            return {"159915": 2, "510300": float("nan")}
        return {"510300": 3.5}

    monkeypatch.setattr(ClosingPrices, "close_prices", fetch)
    refresh = QuoteRefresh()
    refresh.start(account, TODAY)
    try:
        assert entered.wait(5)
        refresh.start(account, TODAY)
        buy(account)
        before = wallet.account_data(account)
    finally:
        released.set()
    assert wait_finished(refresh, account)["error"] is None
    assert calls == [(["159915"], PRICE_DAY)]
    assert wallet.account_data(account)["valuation"]["missing"] == ["510300"]
    refresh.start(account, TODAY)
    assert wait_finished(refresh, account)["error"] is None
    assert calls[-1] == (["510300"], PRICE_DAY)
    assert len(calls) == 2

    view = wallet.account_data(account)
    assert view["equity"] == 12949
    assert view["valuation"] == {"date": PRICE_DAY, "missing": []}
    for key in ("cash", "history", "trades", "revision"):
        assert view[key] == before[key]
    assert {item["code"]: item["price"] for item in view["strategies"][0]["suggestions"]} == {
        "588000": 3, "159915": 2,
    }
    assert all(item["price_date"] == PRICE_DAY for item in view["strategies"][0]["suggestions"])
    assert wallet.cash_rebalance(account, {"revision": view["revision"]})["valuation"] == view["valuation"]
    refresh.start(account, TODAY)
    assert len(calls) == 2


def test_target_missing_only_blocks_that_reference(account):
    view = wallet.account_data(account)
    core = view["strategies"][0]
    assert view["equity"] == 13000 and core["allocation_budget"] == 13000
    groups = {row["id"]: row for row in core["allocation_groups"]}
    assert groups["159915"]["reference_price_missing"]
    assert groups["159915"]["market_value"] == 0 and groups["159915"]["weight"] == .4
    assert not groups["588000"]["reference_price_missing"]
    assert [item["code"] for item in core["suggestions"]] == ["588000"]
    assert wallet.cash_rebalance(account, {"revision": view["revision"]})["valuation"]["date"] == PRICE_DAY


def test_refresh_api_keeps_t1_and_updates_fund_nav_independently(account, monkeypatch):
    buy(account)
    for code in ("000051", "000123"):
        wallet.record_event(account, dict(request_id=str(uuid4()), revision=wallet.revision(wallet.load(account)),
                                          date=TODAY.isoformat(), side="fund_buy", code=code,
                                          qty=123.45, amount=185.18, nav=1.5, nav_date=PRICE_DAY))
    before = wallet.account_data(account)
    fetches = []

    def fetch_close(self, codes, day):
        fetches.append((codes, day))
        return {"159915": 2}

    def fetch_fund(code, through):
        assert through == TODAY.isoformat()
        if code == "000051":
            return through, 1.6
        raise TimeoutError("test fund unavailable")

    class TestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2025, 1, 7, 10, tzinfo=tz)

    monkeypatch.setattr(ClosingPrices, "close_prices", fetch_close)
    monkeypatch.setattr("data_provider.fund_nav.latest_fund_nav", fetch_fund)
    monkeypatch.setattr(dashboard, "datetime", TestClock)
    monkeypatch.setattr(dashboard, "ROOT", account.parent.parent)
    monkeypatch.setattr(dashboard, "QUOTES", QuoteRefresh())
    monkeypatch.setattr(dashboard, "AUTH_DELAY", 0)
    monkeypatch.setenv("TRADE_DASHBOARD_USER", "test")
    monkeypatch.setenv("TRADE_DASHBOARD_PASSWORD", "local-test-password")
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    auth = base64.b64encode(b"test:local-test-password").decode()
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
    try:
        for request_headers, status in (({}, 401), (headers, 403)):
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(base + "/api/etf/refresh", b"{}", request_headers))
            assert error.value.code == status
        write_headers = {**headers, "X-Trade-Request": "dashboard"}
        nav_request = urllib.request.Request(base + "/api/etf/fund/nav",
            json.dumps({"code": "000051", "date": TODAY.isoformat()}).encode(), write_headers)
        with urllib.request.urlopen(nav_request) as response:
            assert json.load(response) == {"date": TODAY.isoformat(), "nav": 1.6}
        with urllib.request.urlopen(urllib.request.Request(base + "/api/etf/refresh", b"{}", write_headers)) as response:
            assert response.status == 202
        wait_finished(dashboard.QUOTES, account)
        with urllib.request.urlopen(urllib.request.Request(base + "/api/overview", headers=headers)) as response:
            result = json.load(response)
    finally:
        server.shutdown()
        server.server_close()
    assert fetches == [(["159915", "510300"], PRICE_DAY)]
    overview, = result["accounts"]
    assert overview["equity"] is None and overview["valuation"]["missing"] == ["510300"]
    assert overview["quote_refresh"]["funds"] == {
        "running": False, "updated": ["000051"], "unchanged": [], "failed": ["000123"],
    }
    for key in ("cash", "history", "trades", "revision"):
        assert overview[key] == before[key]
    core = next(row for row in result["strategies"] if row["id"] == "core")
    groups = {row["id"]: row for row in core["allocation_groups"]}
    assert groups["outside-fund:000051"]["market_value"] == 197.52
    assert groups["outside-fund:000051"]["products"][0]["date"] == TODAY.isoformat()
    assert groups["outside-fund:000123"]["products"][0]["date"] == PRICE_DAY


def test_fund_nav_source_selects_latest_valid_unit_nav_at_cutoff(monkeypatch):
    from data_provider import fund_nav

    def fetch(url, *, params, headers, timeout):
        assert url == fund_nav.HISTORY_URL
        assert params["fundCode"] == "000051" and params["endDate"] == "2025-01-07"
        assert params["pageIndex"] == 1 and params["pageSize"] > 1
        assert headers["Referer"].endswith("jjjz_000051.html") and timeout == (3, 5)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
            "ErrCode": 0,
            "Data": {
                "FundType": "001", "Feature": "030,031,050,051,053", "SYType": None,
                "LSJZList": [
                    {"FSRQ": "2025-01-04", "DWJZ": "1.2", "LJJZ": "2.2"},
                    {"FSRQ": "2025-01-07", "DWJZ": "NaN", "LJJZ": "2.3"},
                    {"FSRQ": "2025-01-08", "DWJZ": "1.8", "LJJZ": "2.8"},
                    {"FSRQ": "2025-01-06", "DWJZ": "0.9", "LJJZ": "2.4"},
                    {"FSRQ": "invalid", "DWJZ": "99", "LJJZ": "99"},
                ],
            },
        })

    monkeypatch.setattr(fund_nav.requests, "get", fetch)
    assert fund_nav.latest_fund_nav("000051", "2025-01-07") == ("2025-01-06", 0.9)


@pytest.mark.parametrize(("code", "feature", "income_type", "value", "expected"), [
    ("007696", "070,080,200", "每万份收益", "102.0726", ("2025-01-07", 102.0726)),
    ("511880", "070,200,201", None, "100.9810", ("2025-01-07", 100.981)),
    ("511990", "100,201", "每百份收益", "0.2767", None),
    ("001821", None, "每万份收益", "-0.0100", ("2025-01-07", 1.0)),
    ("001821", None, "每万份收益", "--", None),
    ("001821", None, None, "0.3348", None),
])
def test_money_market_nav_respects_quote_kind(monkeypatch, code, feature, income_type, value, expected):
    from data_provider import fund_nav

    monkeypatch.setattr(fund_nav.requests, "get", lambda *args, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {
            "ErrCode": 0,
            "Data": {
                "FundType": "005", "Feature": feature, "SYType": income_type,
                "LSJZList": [{"FSRQ": "2025-01-07", "DWJZ": value, "LJJZ": "1.27"}],
            },
        },
    ))
    assert fund_nav.latest_fund_nav(code, "2025-01-07") == expected


def test_quote_refresh_checks_fund_with_only_a_pending_subscription(account, monkeypatch):
    day = TODAY.isoformat()
    order = wallet.record_event(account, dict(
        request_id=str(uuid4()), revision=wallet.revision(wallet.load(account)),
        date=day, side="fund_pending", code="096001", amount=50, nav_date=day,
        subscription_fee={"kind": "none", "value": 0},
    ))
    monkeypatch.setattr(ClosingPrices, "close_prices", lambda *_: {})
    monkeypatch.setattr("data_provider.fund_nav.latest_fund_nav", lambda code, through: (through, 2.5))
    refresh = QuoteRefresh()
    refresh.start(account, TODAY)
    assert wait_finished(refresh, account)["funds"]["confirmed"] == [order["id"]]
    view = wallet.account_data(account)
    assert view["fund_pending"] == {}
    assert view["fund_positions"]["096001"]["qty"] == 20
    assert view["cash"] == 9950
