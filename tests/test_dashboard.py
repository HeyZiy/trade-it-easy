import base64
import gzip
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pandas as pd
import pytest

import dashboard
from dashboard import dashboard_data


def test_report_retains_readable_formatting_without_active_html(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    name = "etf_weekly_20250102.md"
    (reports / name).write_text(
        "# 周报\n\n## 决策\n\n**继续持有**\n\n"
        "| 标的 | 评分 |\n| --- | --- |\n| ETF | 1 |\n\n"
        "[危险链接](javascript:alert(1))\n\n<script>alert(1)</script>\n"
        '<img src="x" onerror="alert(1)">', encoding="utf-8")
    result = dashboard.report_data(tmp_path, name)
    assert "<h2>决策</h2>" in result["html"]
    assert "<strong>继续持有</strong>" in result["html"]
    assert "<table>" in result["html"]
    assert "<script" not in result["html"] and "<img" not in result["html"]
    assert "<a " not in result["html"]
    with pytest.raises(FileNotFoundError):
        dashboard.report_data(tmp_path, "../secret.md")


def test_missing_and_corrupt_accounts_are_isolated(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "trade_ledger.jsonl").write_text("not json", encoding="utf-8")
    overview = dashboard_data(tmp_path)
    assert all(account.get("error") for account in overview["accounts"])
    assert overview["reports"] == []


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_old_simulation_files_stay_out_of_the_dashboard(tmp_path):
    from src.etf_account import initialize
    data = tmp_path / "data"
    initialize(data / "etf_account.json", asof="2025-01-02", cash=1000,
               positions=[dict(code="510300", name="核心ETF", count=100, price=3, strategy="core")])
    # 旧模拟账本的行契约（append-only JSONL），验证其内容不再回流看板。
    (data / "trade_ledger_etf_rotation.jsonl").write_text(
        json.dumps({"date": "2025-01-03", "code": "512500", "name": "轮动ETF",
                    "side": "buy", "qty": 200, "price": 2.0, "account": "etf_rotation"}),
        encoding="utf-8")
    overview = dashboard_data(tmp_path)
    strategies = {s["id"]: s for s in overview["strategies"]}
    assert [p["code"] for p in strategies["core"]["positions"]] == ["510300"]
    assert not strategies["rotation"]["positions"] and "observation" not in strategies["rotation"]
    assert strategies["core"]["trades"] == []
    assert overview["accounts"][0]["cash"] == 1000
    assert strategies["core"]["account_id"] == "etf"



def test_strategy_positions_carry_t1_close(tmp_path, monkeypatch):
    from datetime import date
    from src.etf_account import initialize, collect_close

    monkeypatch.setattr("src.etf_account._today", lambda: date(2025, 1, 3))
    path = tmp_path / "data" / "etf_account.json"
    initialize(path, asof="2025-01-01", cash=1000,
               positions=[dict(code="510300", name="核心ETF", count=100, price=3, strategy="core")])
    collect_close(path, "2025-01-02", lambda codes, day: {codes[0]: 3.3})
    (path.with_name("trade_ledger_etf_rotation.jsonl")).write_text(
        json.dumps({"date": "2025-01-02", "code": "512500", "name": "轮动ETF",
                    "side": "buy", "qty": 200, "price": 2.0, "account": "etf_rotation"}),
        encoding="utf-8")
    overview = dashboard_data(tmp_path)
    strategies = {s["id"]: s for s in overview["strategies"]}
    assert strategies["core"]["positions"][0]["close"] == 3.3



def test_authorized_accepts_only_exact_credentials():
    assert dashboard.authorized(basic("trader", "pw"), "trader", "pw")
    assert not dashboard.authorized(basic("trader", "bad"), "trader", "pw")
    assert not dashboard.authorized(basic("other", "pw"), "trader", "pw")
    assert not dashboard.authorized("", "trader", "pw")
    assert not dashboard.authorized("Bearer xyz", "trader", "pw")
    assert not dashboard.authorized("Basic %%%", "trader", "pw")
    assert not dashboard.authorized("Basic " + base64.b64encode(b"\xff\xfe").decode(), "trader", "pw")
    assert not dashboard.authorized(basic("trader", "pw"), "trader", "")


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_dashboard_bulk_fills_missing_listed_names(tmp_path, monkeypatch):
    from data_provider import bars, security_names
    from data_provider.fetchers.amazingdata import basics
    from src.etf_account import initialize, record_event, revision, load

    path = tmp_path / "data" / "etf_account.json"
    initialize(path, asof="2025-01-02", cash=1000,
               positions=[dict(code=code, count=100, price=3, strategy="core")
                          for code in ("513260", "159957", "159561", "600519")],
               cash_allocations={"core": 1000})
    record_event(path, {"request_id": "fund-name", "revision": revision(load(path)),
                        "date": "2025-01-06", "side": "fund_buy", "code": "000001",
                        "qty": 10, "amount": 10, "nav": 1, "nav_date": "2025-01-06"})
    before = path.read_bytes()
    security_names.remember({"159561": "德国ETF嘉实"})
    calls = []

    def sina():
        calls.append("sina")
        return pd.DataFrame({"code": ["513260", "159957", "159561", "588000"],
                             "name": ["恒生科技ETF汇添富", "nan", "简称待保留", "科创50ETF"]})

    def amazingdata(kind):
        calls.append(kind)
        return {"159957": "创业板HX", "513260": "恒科技"} if kind == "EXTRA_ETF" else {"600519": "贵州茅台"}

    monkeypatch.setattr(bars, "get_etf_universe", sina)
    monkeypatch.setattr(basics, "fetch_security_names", amazingdata)
    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    monkeypatch.setenv("TRADE_DASHBOARD_PASSWORD", "secret")
    monkeypatch.delenv("TRADE_DASHBOARD_TAILSCALE_LOGIN", raising=False)
    server = serve()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        auth = {"Authorization": basic("trader", "secret")}
        overview = urllib.request.Request(base + "/api/overview", headers=auth)
        with urllib.request.urlopen(overview) as response:
            account = json.load(response)["accounts"][0]
        requested = ["513260", "159957", "600519"]
        assert set(requested) <= set(account["missing_security_names"])
        assert {"159561", "000001"}.isdisjoint(account["missing_security_names"])
        assert not calls

        endpoint = base + "/api/etf/security/names"
        headers = {**auth, "Content-Type": "application/json", "X-Trade-Request": "dashboard"}
        invalid = urllib.request.Request(endpoint, json.dumps({"codes": ["513abc"]}).encode(), headers)
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(invalid)
        assert rejected.value.code == 400
        request = urllib.request.Request(endpoint, json.dumps({"codes": [*requested, "159561"]}).encode(), headers)
        expected = {"513260": "恒生科技ETF汇添富", "159957": "创业板HX", "600519": "贵州茅台", "159561": "德国ETF嘉实"}
        for _ in range(2):
            with urllib.request.urlopen(request) as response:
                assert json.load(response) == {"names": expected}
        assert calls == ["sina", "EXTRA_ETF", "EXTRA_STOCK_A"]
        assert security_names.names() == expected

        with urllib.request.urlopen(overview) as response:
            updated = json.load(response)["accounts"][0]
        assert not set(requested).intersection(updated["missing_security_names"])
        assert {row["code"]: row["name"] for row in updated["positions"]} == expected
        assert path.read_bytes() == before
    finally:
        server.shutdown()
        server.server_close()


def test_handler_requires_auth_before_routing(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_DELAY", 0)
    monkeypatch.setenv("TRADE_DASHBOARD_PASSWORD", "secret")
    server = serve()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        for path in ("/api/overview", "/", "/report/nope.md", "/api/report/nope.md", "/assets/app.js", "/unknown"):
            with pytest.raises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base + path)
            assert denied.value.code == 401
            assert "Basic" in denied.value.headers.get("WWW-Authenticate", "")
        request = urllib.request.Request(base + "/api/overview",
                                         headers={"Authorization": basic("trader", "secret")})
        with urllib.request.urlopen(request) as response:
            assert response.status == 200
            assert json.loads(response.read())["accounts"]
        wrong = urllib.request.Request(base + "/api/overview",
                                       headers={"Authorization": basic("trader", "bad")})
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(wrong)
        assert denied.value.code == 401
    finally:
        server.shutdown()
        server.server_close()


def test_serve_identity_replaces_password_for_reads_and_writes(monkeypatch):
    monkeypatch.setenv("TRADE_DASHBOARD_TAILSCALE_LOGIN", "owner@example.com")
    monkeypatch.setenv("TRADE_DASHBOARD_PASSWORD", "old-password")
    owner = {"Tailscale-User-Login": "owner@example.com"}
    server = serve()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        for headers in ({"Authorization": basic("trader", "old-password")},
                        {"Tailscale-User-Login": "other@example.com"}):
            with pytest.raises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(urllib.request.Request(base + "/", headers=headers))
            assert denied.value.code == 403
            assert denied.value.headers.get("WWW-Authenticate") is None
        with urllib.request.urlopen(urllib.request.Request(base + "/", headers=owner)) as response:
            assert response.status == 200

        post_headers = {"Content-Type": "application/json", "X-Trade-Request": "dashboard"}
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(urllib.request.Request(base + "/unknown", data=b"{}", headers=post_headers))
        assert denied.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as routed:
            urllib.request.urlopen(urllib.request.Request(base + "/unknown", data=b"{}", headers=post_headers | owner))
        assert routed.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_report_api_requires_auth_and_returns_inline_content(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    name = "quality_pool_advice_20250102.md"
    (reports / name).write_text("## 本次执行\n\n无调仓指令。", encoding="utf-8")
    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    monkeypatch.setenv("TRADE_DASHBOARD_PASSWORD", "secret")
    server = serve()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        request = urllib.request.Request(base + "/api/report/" + name,
                                         headers={"Authorization": basic("trader", "secret")})
        with urllib.request.urlopen(request) as response:
            result = json.loads(response.read())
            assert response.status == 200
            assert result["name"] == "质量动量"
            assert "<h2>本次执行</h2>" in result["html"]
        invalid = urllib.request.Request(base + "/api/report/../secret.md",
                                         headers={"Authorization": basic("trader", "secret")})
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(invalid)
        assert missing.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_static_assets_revalidate_with_etag_and_gzip(monkeypatch):
    monkeypatch.setenv("TRADE_DASHBOARD_PASSWORD", "secret")
    server = serve()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {"Authorization": basic("trader", "secret")}
        with urllib.request.urlopen(urllib.request.Request(base + "/assets/app.js", headers=headers)) as response:
            body = response.read()
            etag = response.headers["ETag"]
            assert response.status == 200 and etag and body
        cached = urllib.request.Request(base + "/assets/app.js", headers={**headers, "If-None-Match": etag})
        try:
            urllib.request.urlopen(cached)
            assert False, "etag revalidation should return 304"
        except urllib.error.HTTPError as not_modified:
            assert not_modified.code == 304
            assert not_modified.read() == b""
        compressed = urllib.request.Request(base + "/assets/app.js",
                                            headers={**headers, "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(compressed) as response:
            assert response.headers["Content-Encoding"] == "gzip"
            assert gzip.decompress(response.read()) == body
        with urllib.request.urlopen(urllib.request.Request(base + "/favicon.svg", headers=headers)) as response:
            assert response.status == 200
            assert response.headers["Content-Type"] == "image/svg+xml"
    finally:
        server.shutdown()
        server.server_close()


def test_handler_fails_closed_without_configured_password(monkeypatch):
    monkeypatch.setattr(dashboard, "AUTH_DELAY", 0)
    monkeypatch.delenv("TRADE_DASHBOARD_PASSWORD", raising=False)
    server = serve()
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/overview")
        assert denied.value.code == 503
    finally:
        server.shutdown()
        server.server_close()
