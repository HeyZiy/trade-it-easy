import base64
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

import dashboard
from dashboard import account_data, dashboard_data
from src.trade_ledger.ledger import TradeRecord, append_trade
from src.trade_ledger.performance import _fingerprint


def test_account_shows_ledger_and_only_valid_performance(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "trade_ledger.jsonl"
    trade = TradeRecord(date="2025-01-02", code="510300", name="ETF", side="buy",
                        qty=100, price=3.0)
    append_trade(trade, path)
    rows = [{"date": "2025-01-02", "nav": 1.1, "fingerprint": _fingerprint([trade], "2025-01-02"), "issues": []},
            {"date": "2025-01-03", "nav": None, "fingerprint": "bad", "issues": ["缺价格"]}]
    path.with_suffix(".performance.json").write_text(json.dumps({"records": rows}), encoding="utf-8")
    result = account_data(tmp_path, path.name)
    assert result["positions"][0]["count"] == 100
    assert result["cash"] == 999700
    assert result["performance"]["date"] == "2025-01-02"
    assert "缺价格" in result["warnings"]


def test_curve_keeps_missing_days_as_gaps_and_excludes_future_records(tmp_path):
    path = tmp_path / "data" / "trade_ledger.jsonl"
    trade = TradeRecord("2025-01-02", "510300", "ETF", "buy", 100, 3.0)
    append_trade(trade, path)
    rows = [
        {"date": "2025-01-02", "nav": 1.01, "benchmark_nav": 1.005,
         "fingerprint": _fingerprint([trade], "2025-01-02"), "issues": []},
        {"date": "2025-01-03", "nav": 1.2, "benchmark_nav": 1.1,
         "fingerprint": "old-ledger", "issues": []},
        {"date": "2025-01-06", "nav": 1.02, "benchmark_nav": None,
         "fingerprint": _fingerprint([trade], "2025-01-06"), "issues": []},
        {"date": "2099-01-01", "nav": 2, "fingerprint": "future", "issues": []},
    ]
    state = {"start_date": "2025-01-02", "baseline_date": "2024-12-31",
             "benchmark": {"name": "测试基准"}, "records": rows}
    path.with_suffix(".performance.json").write_text(json.dumps(state), encoding="utf-8")
    account = account_data(tmp_path, path.name)
    assert account["history"] == [
        {"date": "2024-12-31", "nav": 1.0, "benchmark_nav": 1.0},
        {"date": "2025-01-02", "nav": 1.01, "benchmark_nav": 1.005},
        {"date": "2025-01-03", "nav": None, "benchmark_nav": None},
        {"date": "2025-01-06", "nav": 1.02, "benchmark_nav": None},
    ]
    assert account["benchmark_name"] == "测试基准"
    strategies = {s["id"]: s for s in dashboard_data(tmp_path)["strategies"]}
    assert strategies["core"]["history"] == account["history"]


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


def test_display_table_covers_registry_accounts():
    """注册表与展示表强制对齐：新账户漏补 DISPLAY 展示元数据时在此失败。"""
    from src.sim_accounts import all_accounts

    registered = {Path(sa.ledger_path).name for sa in all_accounts()}
    listed = {filename for filename, _, _ in dashboard.DISPLAY}
    assert registered == listed


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_core_and_industry_use_independent_ledgers(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "trade_ledger.jsonl"
    append_trade(TradeRecord(date="2025-01-02", code="510300", name="核心ETF",
                             side="buy", qty=100, price=3.0, account="core"), path)
    append_trade(TradeRecord(date="2025-01-03", code="512500", name="卫星ETF",
                             side="buy", qty=200, price=2.0, account="satellite"),
                 data / "trade_ledger_industry_momentum.jsonl")
    overview = dashboard_data(tmp_path)
    strategies = {s["id"]: s for s in overview["strategies"]}
    assert [p["code"] for p in strategies["core"]["positions"]] == ["510300"]
    assert [p["code"] for p in strategies["satellite"]["positions"]] == ["512500"]
    assert [t["code"] for t in strategies["core"]["trades"]] == ["510300"]
    assert [t["code"] for t in strategies["satellite"]["trades"]] == ["512500"]
    assert strategies["core"]["ledger"] == "core"
    assert strategies["satellite"]["ledger"] == "satellite"
    assert "cash" not in strategies["core"] or strategies["core"]["cash"] is None
    assert not strategies["core"]["shared"] and not strategies["satellite"]["shared"]
    assert not strategies["rotation"]["shared"] and not strategies["quality"]["shared"]


def test_strategy_positions_carry_last_close_snapshot(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "trade_ledger.jsonl"
    core = TradeRecord(date="2025-01-02", code="510300", name="核心ETF",
                       side="buy", qty=100, price=3.0, account="core")
    satellite = TradeRecord(date="2025-01-02", code="512500", name="卫星ETF",
                            side="buy", qty=200, price=2.0, account="satellite")
    append_trade(core, path)
    append_trade(satellite, data / "trade_ledger_industry_momentum.jsonl")
    rows = [{"date": "2025-01-02", "nav": 1.0, "issues": [],
             "fingerprint": _fingerprint([core], "2025-01-02"),
             "positions": [{"code": "510300", "name": "核心ETF", "count": 100,
                            "cost_price": 3.0, "close": 3.3,
                            "market_value": 330.0, "pos_pct": 0.033}]}]
    path.with_suffix(".performance.json").write_text(json.dumps({"records": rows}), encoding="utf-8")
    strategies = {s["id"]: s for s in dashboard_data(tmp_path)["strategies"]}
    assert strategies["core"]["positions"][0]["close"] == 3.3
    # 快照缺某只持仓时保持空值，页面回退到成本口径。
    assert strategies["satellite"]["positions"][0]["close"] is None


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


def test_report_api_requires_auth_and_returns_inline_content(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    name = "quality_pool_20250102.md"
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
            assert result["name"] == "质量池"
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
