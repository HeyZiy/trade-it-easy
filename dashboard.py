"""Private, read-only dashboard for the server's nominal strategy accounts."""

import argparse
import base64
from dataclasses import asdict
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from html.parser import HTMLParser
import hmac
import json
import logging
import os
from pathlib import Path
import time

from src.trade_ledger.ledger import derive, load_trades
from src.trade_ledger.performance import load_performance, valid_records

APP_ROOT = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TRADE_DASHBOARD_ROOT", APP_ROOT)).resolve()
# 展示元数据（台账文件名, id, 显示名），顺序即 accounts 数组顺序。账户存在性归
# sim_accounts 注册表（join key = 台账文件名，即「单池」的账户身份）；
# 新增账户 = 家里定义描述符 + sim_accounts 加一行 + 此处补一行展示元数据
# （test_display_table_covers_registry_accounts 强制两侧对齐）。
DISPLAY = (
    ("trade_ledger.jsonl", "core", "核心配置"),
    ("trade_ledger_industry_momentum.jsonl", "satellite", "行业动量（模拟）"),
    ("trade_ledger_etf_rotation.jsonl", "rotation", "ETF 跨资产轮动"),
    ("trade_ledger_quality_pool_v2.jsonl", "quality", "质量池轮动"),
)
# 展示层策略拆分：(id, 名称, 所属账本, 账户标签)。标签为 None 表示整本账。
# 四个策略分别读取独立台账；核心配置不包含行业模拟记录。
STRATEGIES = (
    ("core", "核心配置", "core", None),
    ("satellite", "行业动量（模拟）", "satellite", None),
    ("rotation", "ETF 跨资产轮动", "rotation", None),
    ("quality", "质量池轮动", "quality", None),
)
AUTH_REALM = 'Basic realm="trade-dashboard", charset="UTF-8"'
AUTH_DELAY = 0.5


class ReportHTML(HTMLParser):
    """Keep report formatting while dropping links, scripts and all HTML attributes."""

    TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "del",
            "ul", "ol", "li", "blockquote", "pre", "code", "hr", "br",
            "table", "thead", "tbody", "tr", "th", "td"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.TAGS:
            self.parts.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in self.TAGS and tag not in {"hr", "br"}:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(escape(data))


def report_data(root, name):
    """Render only a whitelisted latest report for reading inside the dashboard."""
    import markdown2

    report = next((r for r in _report_files(root) if r["file"] == name), None)
    if report is None:
        raise FileNotFoundError(name)
    markdown = (root / "reports" / name).read_text(encoding="utf-8")
    cleaner = ReportHTML()
    cleaner.feed(markdown2.markdown(markdown, safe_mode="escape",
                                   extras=["tables", "fenced-code-blocks", "strike"]))
    cleaner.close()
    return {**report, "html": "".join(cleaner.parts)}


def auth_config():
    """口令只从环境变量读取；未配置时服务拒绝提供内容。"""
    return (os.environ.get("TRADE_DASHBOARD_USER", "trader"),
            os.environ.get("TRADE_DASHBOARD_PASSWORD", ""))


def authorized(header, user, password):
    """校验 Basic 头；口令为空一律拒绝，避免漏配时裸奔。"""
    if not header or not password:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return False
    try:
        raw = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    got_user, _, got_password = raw.partition(":")
    user_ok = hmac.compare_digest(got_user.encode("utf-8"), user.encode("utf-8"))
    pass_ok = hmac.compare_digest(got_password.encode("utf-8"), password.encode("utf-8"))
    return user_ok and pass_ok


def account_data(root, filename):
    path = root / "data" / filename
    if not path.exists():
        return {"error": "台账文件不存在", "positions": [], "trades": []}
    trades = load_trades(path)
    today = date.today().isoformat()
    snapshot = derive(trades, today)
    performance = load_performance(path)
    rows, valid = valid_records(trades, performance.get("records", []), today)
    rows = sorted(rows, key=lambda r: r["date"])
    valid = sorted(valid, key=lambda r: r["date"])
    valid_dates = {row["date"] for row in valid}
    history = [{"date": row["date"],
                "nav": row["nav"] if row["date"] in valid_dates else None,
                "benchmark_nav": row.get("benchmark_nav") if row["date"] in valid_dates else None}
               for row in rows]
    baseline = performance.get("baseline_date")
    if valid and baseline and baseline < history[0]["date"]:
        history.insert(0, {"date": baseline, "nav": 1.0, "benchmark_nav": 1.0})
    latest = valid[-1] if valid else None
    warnings = list(rows[-1].get("issues", [])) if rows else []
    if rows and (not latest or latest["date"] != rows[-1]["date"]):
        warnings.append("最近收盘成绩不完整或流水已变更，显示最近有效成绩。")
    return {
        "positions": snapshot.positions, "cash": snapshot.cash,
        "receivables": snapshot.receivables,
        "trades": [asdict(t) for t in reversed(trades)],
        "performance": latest, "warnings": warnings,
        "history": history,
        "benchmark_name": performance.get("benchmark", {}).get("name", "同期基准"),
        "last_trade_date": trades[-1].date if trades else None,
    }


def strategy_view(key, name, account, tag):
    """按账户标签切分展示；现金与成绩属于整本账，拆分页只给比例与合计口径。"""
    account = account or {}
    performance = account.get("performance")
    # 收盘价只来自成绩快照，老记录没有该字段时保持空值。
    closes = {row.get("code"): row.get("close")
              for row in (performance or {}).get("positions") or []}
    positions = [{**p, "close": closes.get(p.get("code"))}
                 for p in account.get("positions", [])]
    trades = account.get("trades", [])
    if tag is not None:
        positions = [p for p in positions if p.get("account") == tag]
        trades = [t for t in trades if t.get("account") == tag]
    return {"id": key, "name": name, "ledger": account.get("id"),
            "positions": positions, "trades": trades[:20],
            "performance": performance,
            "history": account.get("history", []),
            "benchmark_name": account.get("benchmark_name", "同期基准"),
            "warnings": account.get("warnings", []),
            "error": account.get("error"),
            "last_trade_date": account.get("last_trade_date"),
            "shared": tag is not None}


def _display_accounts():
    """枚举展示账户：存在性归 sim_accounts 注册表，元数据与顺序归 DISPLAY。"""
    from src.sim_accounts import all_accounts

    registry = {Path(sa.ledger_path).name for sa in all_accounts()}
    listed = {filename for filename, _, _ in DISPLAY}
    for filename in sorted(listed - registry):
        logging.warning("展示元数据指向的台账已不在注册表: %s", filename)
    for filename in sorted(registry - listed):
        logging.warning("注册表账户缺展示元数据，未展示: %s", filename)
    for filename, key, name in DISPLAY:
        if filename in registry:
            yield filename, key, name


def _report_files(root) -> list:
    """最近报告清单：按前缀取最新一份，供总览与 /report/ 白名单共用。"""
    report_dir = root / "reports"
    reports = []
    for prefix, label in (("etf_weekly", "核心配置"), ("industry_momentum", "行业动量"),
                          ("etf_rotation", "ETF 跨资产轮动"), ("quality_pool", "质量池"),
                          ("portfolio_close", "收盘成绩")):
        files = sorted(report_dir.glob(prefix + "_????????.md"), reverse=True)
        if files:
            reports.append({"name": label, "file": files[0].name,
                            "date": files[0].stem[-8:]})
    return reports


def _state_detail(filename: str, state) -> str:
    """状态文件摘要行：质量池给信号节奏，行业池给重建与成员数。"""
    if filename.startswith("quality"):
        return (f"上次信号：{state.get('last_signal_date') or '暂无'}；"
                f"下次计划：{state.get('next_signal_date') or '暂无'}；"
                f"待退出：{len(state.get('exit_queue') or {})} 只；"
                f"重试：{'是' if state.get('signal_retry') else '否'}")
    return f"池更新：{state.get('rebuilt_on') or '暂无'}；成员：{len(state.get('members') or [])} 只"


def dashboard_data(root=ROOT):
    accounts = []
    for filename, key, name in _display_accounts():
        try:
            data = account_data(root, filename)
        except Exception:
            logging.exception("读取账户失败: %s", key)
            data = {"error": "账户数据读取失败，请检查服务器日志。", "positions": [], "trades": []}
        accounts.append({"id": key, "name": name, **data})
    by_id = {account["id"]: account for account in accounts}
    strategies = [strategy_view(key, name, by_id.get(ledger), tag)
                  for key, name, ledger, tag in STRATEGIES]
    reports = _report_files(root)
    states = []
    for filename, label in (("quality_pool_v2_state.json", "质量池状态"),
                            ("industry_momentum_pool.json", "行业池状态")):
        path = root / "data" / filename
        try:
            detail = _state_detail(filename, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            detail = "状态文件不存在或无法读取"
        states.append({"name": label, "detail": detail})
    return {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "accounts": accounts, "strategies": strategies,
            "reports": reports, "states": states}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        user, password = auth_config()
        if not password:
            logging.error("未配置 TRADE_DASHBOARD_PASSWORD，拒绝提供服务")
            self.respond("服务未配置访问口令。".encode("utf-8"), "text/plain; charset=utf-8", status=503)
            return
        if not authorized(self.headers.get("Authorization", ""), user, password):
            logging.warning("鉴权失败 %s %s", self.client_address[0], self.path)
            time.sleep(AUTH_DELAY)
            self.respond("需要登录。".encode("utf-8"), "text/plain; charset=utf-8", status=401,
                         headers={"WWW-Authenticate": AUTH_REALM})
            return
        if self.path == "/":
            self.respond((APP_ROOT / "dashboard" / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path in {"/assets/app.js", "/assets/styles.css"}:
            filename = self.path.rsplit("/", 1)[-1]
            content_type = "text/javascript; charset=utf-8" if filename.endswith(".js") else "text/css; charset=utf-8"
            self.respond((APP_ROOT / "dashboard" / filename).read_bytes(), content_type)
        elif self.path == "/api/overview":
            body = json.dumps(dashboard_data(), ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.respond(body, "application/json; charset=utf-8")
        elif self.path.startswith("/api/report/"):
            name = self.path.removeprefix("/api/report/")
            try:
                report = report_data(ROOT, name)
            except FileNotFoundError:
                self.send_error(404)
                return
            self.respond(json.dumps(report, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")
        elif self.path.startswith("/report/"):
            name = self.path.removeprefix("/report/")
            allowed = {r["file"] for r in dashboard_data()["reports"]}
            if name not in allowed:
                self.send_error(404)
                return
            self.respond((ROOT / "reports" / name).read_bytes(), "text/plain; charset=utf-8")
        else:
            self.send_error(404)

    def respond(self, body, content_type, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    logging.warning("只监听本机 127.0.0.1:%s；需通过 tunnel 访问，口令读 TRADE_DASHBOARD_PASSWORD", args.port)
    if not auth_config()[1]:
        logging.error("TRADE_DASHBOARD_PASSWORD 未设置，所有请求将返回 503")
    server.serve_forever()


if __name__ == "__main__":
    main()
