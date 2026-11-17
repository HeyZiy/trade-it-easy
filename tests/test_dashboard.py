import base64
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

import dashboard
from dashboard import dashboard_data
from src.trade_ledger.ledger import TradeRecord, append_trade


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
    append_trade(TradeRecord(date="2025-01-03", code="512500", name="轮动ETF",
                             side="buy", qty=200, price=2.0, account="etf_rotation"),
                 data / "trade_ledger_etf_rotation.jsonl")
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
    append_trade(TradeRecord("2025-01-02", "512500", "轮动ETF", "buy", 200, 2, account="etf_rotation"),
                 path.with_name("trade_ledger_etf_rotation.jsonl"))
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
