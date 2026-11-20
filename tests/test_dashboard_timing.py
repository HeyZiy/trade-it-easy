import base64
from io import BytesIO
import json
from threading import Event
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import dashboard
from data_provider import bars, security_names
from data_provider.fetchers.amazingdata import client, market
from research.studies.timing import web_report
from src.dashboard_timing import TimingReports

API = "/api/research/timing"


def http(path, *, payload=None, auth=True, write_header=True):
    """Exercise the actual dashboard HTTP handler without binding a TCP port."""
    method = "GET" if payload is None else "POST"
    headers = [f"{method} {path} HTTP/1.0"]
    if auth:
        headers.append("Authorization: Basic " + base64.b64encode(b"trader:timing-test").decode())
    body = b""
    if payload is not None:
        body = json.dumps(payload).encode()
        headers += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
        if write_header:
            headers.append("X-Trade-Request: dashboard")
    incoming = "\r\n".join(headers).encode() + b"\r\n\r\n" + body
    outgoing = bytearray()
    sock = SimpleNamespace(makefile=lambda *_: BytesIO(incoming), sendall=outgoing.extend)
    dashboard.Handler(sock, ("127.0.0.1", 12345), None)
    raw_headers, raw_body = bytes(outgoing).split(b"\r\n\r\n", 1)
    status = int(raw_headers.split()[1])
    return status, json.loads(raw_body) if b"application/json" in raw_headers else raw_body


@pytest.fixture
def reports(tmp_path, monkeypatch):
    reports = TimingReports(tmp_path / "saved")
    monkeypatch.setattr(dashboard, "TIMING", reports)
    monkeypatch.setenv("TRADE_DASHBOARD_USER", "trader")
    monkeypatch.setenv("TRADE_DASHBOARD_PASSWORD", "timing-test")
    monkeypatch.setattr(dashboard, "AUTH_DELAY", 0)
    yield reports
    reports.worker.shutdown()


def test_archive_opens_without_fetching_and_keeps_cycle_window_separate(reports):
    code = "159915"
    for key, window in (("level1", ("2024-01-02", "2024-01-03")),
                        ("cycle", ("2024-01-03", "2024-01-04"))):
        folder = reports.archive / key
        folder.mkdir(parents=True)
        rows = {"code": code, "标的": "创业板", "strategy": "长拿", "start": window[0], "end": window[1],
                "年化收益%": 2, "最大回撤%": -2, "最长水下交易日": 1, "最长水下日历天": 2,
                "平均仓位%": 99, "深回撤时间占比%": 0}
        daily = folder / f"daily_{code}.csv"
        if key == "cycle":
            rows["period"] = "全期"
            daily = folder / f"{code}_全期" / "daily.csv"
            daily.parent.mkdir()
        pd.DataFrame([rows]).to_csv(folder / ("metrics_all.csv" if key == "level1" else "metrics.csv"), index=False)
        pd.DataFrame({"strategy": "长拿", "date": window, "nav": [990_000, 1_010_000],
                      "exposure": [.99, .99]}).to_csv(daily, index=False)

    assert http("/research/timing/159915", auth=False)[0] == 401
    assert http("/research/timing/159915")[0] == 200
    assert http(API)[1]["reports"][0]["end"] == "2024-01-03"
    assert http(API + "/159915")[1]["groups"].keys() == {"archive", "cycle"}
    assert http(API + "/159915/archive")[1]["dates"] == ["2024-01-02", "2024-01-03"]
    assert http(API + "/159915/cycle")[1]["dates"] == ["2024-01-03", "2024-01-04"]
    assert http(API, payload={"code": code}) == (200, {"phase": "existing"})
    assert not list(reports.cache.glob("*.json"))


def test_generate_etf_missing_from_directory_publishes_full_before_rolling_and_reopens(reports, monkeypatch):
    code = "563360"
    dates = pd.bdate_range("2023-01-03", periods=800)
    close = 2 + np.arange(len(dates)) / 180
    raw = pd.DataFrame({"date": dates, "hfq_open": close, "hfq_high": close + .03,
                        "hfq_low": close - .02, "hfq_close": close})
    directory_calls = []

    def incomplete_directory():
        directory_calls.append(True)
        return pd.DataFrame({"code": ["512690"], "name": ["酒 ETF"]})

    monkeypatch.setattr(bars, "get_etf_universe", incomplete_directory)
    monkeypatch.setattr(client, "is_configured", lambda: False)
    monkeypatch.setattr(bars, "get_etf_daily_em", lambda *args: raw)
    real_rolling = web_report.rolling_report
    rolling_started, release = Event(), Event()

    def rolling(h, runners):
        rolling_started.set()
        if not release.wait(timeout=10):
            raise RuntimeError("test did not release rolling job")
        return real_rolling(h, runners)

    monkeypatch.setattr(web_report, "rolling_report", rolling)
    assert http(API, payload={"code": "600000"})[0] == 400
    assert http(API, payload={"code": code}, write_header=False)[0] == 403
    assert http(API, payload={"code": code}) == (202, {"phase": "queued"})
    try:
        assert rolling_started.wait(timeout=10), reports.overview(code)["error"]
        assert not directory_calls
        assert http(API, payload={"code": code}) == (202, {"phase": "rolling"})
        current = http(API + "/" + code)[1]
        assert current["phase"] == "rolling"
        full = http(API + "/" + code + "/current")[1]
        assert len(full["series"]) == 24
        assert full["source"] == "东方财富后复权日线"
        assert full["start"] == dates[271].date().isoformat()
        assert full["end"] == dates[-1].date().isoformat()
        assert full["series"]["长拿"][1][0] < 0  # Day-one fee is visible against initial cash.
        assert current["rolling"] == []
    finally:
        release.set()
    reports.worker.shutdown()

    reloaded = TimingReports(reports.cache)
    assert reloaded.catalog()[0]["name"] == code
    saved = reloaded.overview(code)
    assert saved["phase"] is None and saved["rolling_state"] == "ready"
    assert {row["years"] for row in saved["rolling"]} == {1}
    assert saved["version"] == full["version"]
    assert reloaded.start(code) == "existing"


def test_unavailable_refresh_keeps_real_cache_report(reports, tmp_path, monkeypatch):
    code = "512690"
    dates = pd.bdate_range("2024-01-02", periods=300).strftime("%Y-%m-%d")
    cache = tmp_path / "market"
    daily = cache / "etf_daily"
    daily.mkdir(parents=True)
    source = "amazingdata_raw_hfq"
    pd.DataFrame([{"code": code, "display_name": "快照 ETF", "last_bar_date": dates[-1],
                   "current_listed": True, "source": source}]).to_csv(cache / "etf_securities.csv", index=False)
    (daily / f"{code}.json").write_text(json.dumps({"schema_version": 2, "source": source}), encoding="utf-8")
    pd.DataFrame({"date": dates, "hfq_open": 2, "hfq_high": 2, "hfq_low": 2, "hfq_close": 2,
                  "quality": "ok"}).to_csv(daily / f"{code}.csv", index=False)
    reports.market_cache = cache

    requested = Event()

    def unavailable(code):
        requested.set()
        raise ValueError("在线行情暂不可用")

    monkeypatch.setattr(web_report, "load_live", unavailable)
    assert reports.start(code) == "queued"
    reports.worker.submit(lambda: None).result(timeout=10)
    initial = reports.group(code, "current")
    assert initial["end"] == dates[-1]
    assert "AmazingData" in initial["source"]
    assert not requested.is_set()

    assert reports.start(code, refresh=True) == "queued"
    reports.worker.submit(lambda: None).result(timeout=10)
    assert requested.is_set()
    assert reports.overview(code)["error"] == "在线行情暂不可用"
    assert reports.group(code, "current") == initial


def test_index_code_enables_pe_row_and_is_surfaced(reports, monkeypatch):
    code = "512690"
    idx = pd.bdate_range("2022-01-03", periods=400)
    c = 2 + np.arange(len(idx)) / 100
    h = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c}, index=idx)
    pe = pd.Series(np.linspace(8, 25, len(idx)), index=idx)
    monkeypatch.setattr(web_report, "load_live", lambda code: ("测试 ETF", h, "东方财富后复权日线"))
    seen = {}

    def fake_pe(index_code, pe_dir, refresh=False):
        seen["index_code"] = index_code
        return pe, "2023-09-01"

    monkeypatch.setattr(web_report, "load_pe", fake_pe)
    assert http(API, payload={"code": code, "index_code": "12ab"})[0] == 400
    assert http(API, payload={"code": code, "index_code": "000300"}) == (202, {"phase": "queued"})
    reports.worker.submit(lambda: None).result(timeout=10)
    assert seen["index_code"] == "000300"
    full = http(API + "/" + code + "/current")[1]
    assert "PE分位" in full["series"]
    assert full["index_code"] == "000300"
    assert full["pe_as_of"] == "2023-09-01"
    assert http(API + "/" + code)[1]["index_code"] == "000300"
    assert http(API, payload={"code": code, "index_code": "000300"}) == (200, {"phase": "existing"})


@pytest.mark.parametrize("amazingdata_available", [True, False])
def test_online_research_prefers_full_amazingdata_history(monkeypatch, amazingdata_available):
    expected = {"date": ["2024-01-02"], "hfq_open": [1.0], "hfq_high": [2.0],
                "hfq_low": [1.0], "hfq_close": [2.0]}
    monkeypatch.setattr(security_names, "security_name", lambda code: "恒生 ETF")
    calls = []

    def eastmoney(*args):
        calls.append("东财")
        return pd.DataFrame(expected)

    def fetch(codes, end, *, begin_date):
        calls.append("AmazingData")
        assert codes == ["159920"] and begin_date == "1990-01-01"
        return {"159920": pd.DataFrame(expected)} if amazingdata_available else {}

    monkeypatch.setattr(bars, "get_etf_daily_em", eastmoney)
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(market, "fetch_etf_hfq_bars", fetch)
    name, history, source = web_report.load_live("159920")
    assert name == "恒生 ETF"
    if amazingdata_available:
        assert calls == ["AmazingData"]
        assert source == "AmazingData 后复权 · 在线"
    else:
        assert calls == ["AmazingData", "东财"]
        assert source == "东方财富后复权日线"
    assert history.loc["2024-01-02", "close"] == 2.0
