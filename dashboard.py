"""投资账户、策略建议与研究报告看板。"""

import argparse
import base64
from datetime import date, datetime
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from html.parser import HTMLParser
import hmac
import json
import logging
import os
from pathlib import Path
import re
import time

from src import fund_plans
from src.account_advice import strategy_identity
from src.etf_account import FILENAME as ETF_FILENAME, SHANGHAI, account_data as etf_account_data
from src.dashboard_quotes import QuoteRefresh
from src.dashboard_timing import QueueFull, TimingReports
from src.portfolio_config import HOLDING_AREA, OWNER_NAMES, STRATEGY_NAMES

APP_ROOT = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("TRADE_DASHBOARD_ROOT", APP_ROOT)).resolve()
AUTH_REALM = 'Basic realm="trade-dashboard", charset="UTF-8"'
AUTH_DELAY = 0.5
ASSET_TYPES = {".js": "text/javascript; charset=utf-8",
               ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
COMPRESSIBLE = {"text/html", "text/plain", "text/css", "text/javascript", "application/json"}
QUOTES = QuoteRefresh()
TIMING_URL = "/research/timing"
TIMING_API = "/api" + TIMING_URL
TIMING = TimingReports(ROOT / "data" / "timing_reports",
                       APP_ROOT / "research" / "data" / "etf_daily_ad")


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
    report = next((r for r in _report_files(root) if r["file"] == name), None)
    if report is None:
        raise FileNotFoundError(name)
    markdown = (root / "reports" / name).read_text(encoding="utf-8")
    return {**report, "html": markdown_html(markdown)}


def markdown_html(markdown):
    import markdown2

    cleaner = ReportHTML()
    cleaner.feed(markdown2.markdown(markdown, safe_mode="escape",
                                   extras=["tables", "fenced-code-blocks", "strike"]))
    cleaner.close()
    return "".join(cleaner.parts)


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


def account_data(root):
    path = root / "data" / ETF_FILENAME
    refresh = QUOTES.read(path)
    account = etf_account_data(path)
    account["quote_refresh"] = refresh if refresh and refresh["date"] == account["valuation_date"] else None
    account["dca"] = fund_plans.dca_status(path)
    return account


def _report_files(root) -> list:
    """最近报告清单：按前缀取最新一份，供总览与 /report/ 白名单共用。"""
    report_dir = root / "reports"
    reports = []
    for prefix, label in (("etf_rotation_advice", "跨资产动量"), ("quality_pool_advice", "质量动量")):
        files = sorted(report_dir.glob(prefix + "_????????.md"), reverse=True)
        if files:
            reports.append({"name": label, "file": files[0].name,
                            "date": files[0].stem[-8:]})
    return reports


def dashboard_data(root=ROOT):
    try:
        account = account_data(root)
    except FileNotFoundError:
        account = {"error": "投资账户尚未初始化", "positions": [], "trades": []}
    except Exception:
        logging.exception("读取投资账户失败")
        account = {"error": "账户数据读取失败，请检查服务器日志。", "positions": [], "trades": []}
    strategies = account.pop("strategies", None)
    if strategies is None:
        strategies = [
            {**strategy_identity(key), "positions": [], "trades": [], "allocation_groups": [],
             "budget": None, "budget_pct": None, "invested": 0, "buying_power": None,
             "suggestions": [], "status": "账户未初始化", "history": [], "performance": None}
            for key in OWNER_NAMES
        ]
    account["holding_area"] = next((row for row in strategies if row["id"] == HOLDING_AREA), None)
    strategies = [row for row in strategies if row["id"] in STRATEGY_NAMES]
    account.update(id="etf", name="投资账户")
    return {"generated_at": datetime.now().astimezone().isoformat(),
            "accounts": [account], "strategies": strategies, "reports": _report_files(root)}


class Handler(BaseHTTPRequestHandler):
    def check_access(self, *, json_response=False):
        tailscale_login = os.environ.get("TRADE_DASHBOARD_TAILSCALE_LOGIN")
        headers = None
        if tailscale_login:
            # Serve replaces client-supplied identity headers. Keep the backend on loopback.
            if self.headers.get_all("Tailscale-User-Login") == [tailscale_login]:
                return True
            status, message = 403, "此 Tailscale 账号无权访问。"
        else:
            user, password = auth_config()
            if not password:
                status, message = 503, "服务未配置访问口令。"
            elif authorized(self.headers.get("Authorization", ""), user, password):
                return True
            else:
                logging.warning("鉴权失败 %s %s", self.client_address[0], self.path)
                time.sleep(AUTH_DELAY)
                status, message = 401, "需要登录。"
                headers = {"WWW-Authenticate": AUTH_REALM}
        body = b"{}" if json_response else message.encode("utf-8")
        content_type = "application/json" if json_response else "text/plain; charset=utf-8"
        self.respond(body, content_type, status=status, headers=headers)
        return False

    def timing_json(self, result, status=200):
        body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.respond(body, "application/json; charset=utf-8", status=status)

    def read_timing(self):
        try:
            if self.path == TIMING_API:
                self.timing_json({"reports": TIMING.catalog()})
                return
            match = re.fullmatch(r"/([0-9]{6})(?:/([a-z-]+))?", self.path.removeprefix(TIMING_API))
            if not match:
                raise FileNotFoundError
            code, group = match.groups()
            if group == "case" and code == "159915":
                result = {"html": markdown_html(TIMING.case_study().read_text(encoding="utf-8"))}
            else:
                result = TIMING.group(code, group) if group else TIMING.overview(code)
            self.timing_json(result)
        except FileNotFoundError:
            self.timing_json({"error": "未找到该研究结果。"}, 404)
        except ValueError as error:
            self.timing_json({"error": str(error)}, 400)
        except Exception:
            logging.exception("读取 ETF 择时研究失败")
            self.timing_json({"error": "无法读取报告，请稍后重试。"}, 500)

    def start_timing(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError("请求内容无效。")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求内容无效。")
            phase = TIMING.start(payload.get("code"), payload.get("refresh", False), payload.get("index_code"))
            self.timing_json({"phase": phase}, 200 if phase == "existing" else 202)
        except ValueError as error:
            self.timing_json({"error": str(error)}, 400)
        except QueueFull as error:
            self.timing_json({"error": str(error)}, 429)

    def do_POST(self):
        if not self.check_access(json_response=True):
            return
        # 自定义头不属于简单跨站请求，浏览器必须先通过同源预检。
        if (self.headers.get("X-Trade-Request") != "dashboard"
                or self.headers.get("Content-Type") != "application/json"):
            self.respond(b'{}', "application/json", status=403)
            return
        if self.path == TIMING_API:
            self.start_timing()
            return
        from src.etf_account import (
            FILENAME, cash_rebalance, delete_fund_subscription, dismiss, edit_event, event_context, lookup_fund_names, lookup_fund_nav,
            lookup_security_names, preview_fund_subscription_deletion, record_event, set_budgets,
        )
        handlers = {"/api/etf/event": record_event, "/api/etf/dismiss": dismiss,
                    "/api/etf/budgets": set_budgets, "/api/etf/event/edit": edit_event,
                    "/api/etf/event/delete-subscription": delete_fund_subscription,
                    "/api/etf/event/preview-delete-subscription": preview_fund_subscription_deletion,
                    "/api/etf/event/context": event_context, "/api/etf/fund/nav": lookup_fund_nav,
                    "/api/etf/cash/rebalance": cash_rebalance, "/api/etf/fund/names": lookup_fund_names,
                    "/api/etf/security/names": lookup_security_names}
        from src.portfolio_config import classify_asset, update
        handlers["/api/etf/config"] = update
        handlers["/api/etf/asset/classify"] = classify_asset
        handlers["/api/etf/dca/save"] = fund_plans.save_plan
        handlers["/api/etf/dca/delete"] = fund_plans.delete_plan
        handlers["/api/etf/dca/record"] = fund_plans.record_installment
        if self.path == "/api/etf/refresh":
            try:
                path = ROOT / "data" / FILENAME
                now = datetime.now(SHANGHAI)
                QUOTES.start(path, now.date(), now=now)
            except Exception:
                logging.exception("启动网页估值刷新失败")
                self.respond(json.dumps({"error": "无法刷新估值，请检查账户数据后重试。"}, ensure_ascii=False).encode(),
                             "application/json; charset=utf-8", status=503)
                return
            self.respond(b'{"ok":true}', "application/json; charset=utf-8", status=202)
            return
        if self.path not in handlers:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            # 配置包含所有策略的产品列表和分类，单次记账请求则很小。
            limit = 1024 * 1024 if self.path == "/api/etf/config" else 16384
            if not 0 < length <= limit:
                raise ValueError("请求内容过长或为空")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("记账内容须为对象")
            result = handlers[self.path](ROOT / "data" / FILENAME, payload)
        except (ValueError, TypeError, KeyError) as error:
            self.respond(json.dumps({"error": str(error)}, ensure_ascii=False).encode(),
                         "application/json; charset=utf-8", status=400)
            return
        except Exception:
            logging.exception("投资账户写入失败")
            self.respond(json.dumps({"error": "保存失败，请刷新核对后重试。"}, ensure_ascii=False).encode(),
                         "application/json; charset=utf-8", status=500)
            return
        self.respond(json.dumps(result if self.path in {"/api/etf/event/context", "/api/etf/cash/rebalance", "/api/etf/fund/nav",
                                                            "/api/etf/fund/names", "/api/etf/security/names"} else {"ok": True},
                                ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def download_portfolio(self):
        from src.portfolio_export import export_workbook

        now = datetime.now(SHANGHAI)
        try:
            body = export_workbook(ROOT / "data" / ETF_FILENAME, now=now)
        except FileNotFoundError:
            self.respond("投资账户尚未初始化。".encode(), "text/plain; charset=utf-8", status=404)
            return
        except Exception:
            logging.exception("导出投资账户失败")
            self.respond("无法导出配置和持仓，请检查账户数据后重试。".encode(),
                         "text/plain; charset=utf-8", status=500)
            return
        filename = f"portfolio-{now:%Y%m%d-%H%M%S}.xlsx"
        self.respond(body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    def do_GET(self):
        if not self.check_access():
            return
        if self.path == "/api/etf/config":
            from src.portfolio_config import allocation_rows, data
            from data_provider import fund_names
            from data_provider.security_names import names
            try:
                result = data(ROOT / "data" / ETF_FILENAME)
                funds = {code for row in allocation_rows(result["config"]) for code in row.get("fund_codes", [])}
                result.update(names=names(), fund_names=fund_names.names_for(funds), strategy_names=STRATEGY_NAMES)
            except ValueError as error:
                self.respond(json.dumps({"error": str(error)}, ensure_ascii=False).encode(),
                             "application/json; charset=utf-8", status=400)
                return
            self.respond(json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        elif self.path == "/api/etf/export":
            self.download_portfolio()
        elif self.path == "/":
            self.respond((APP_ROOT / "dashboard" / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.rstrip("/") == "/settings/bond":
            self.send_response(302)
            self.send_header("Location", "/settings/core")
            self.end_headers()
        elif re.fullmatch(r"/settings(?:/(?:budgets|fees|accounts|core|rotation|history))?/?", self.path):
            self.respond((APP_ROOT / "dashboard" / "settings.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == TIMING_URL or re.fullmatch(TIMING_URL + r"/[0-9]{6}", self.path):
            self.respond((APP_ROOT / "dashboard" / "timing.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == TIMING_API or self.path.startswith(TIMING_API + "/"):
            self.read_timing()
        elif self.path.startswith("/assets/") and Path(self.path).suffix in ASSET_TYPES:
            self.serve_asset(APP_ROOT / "dashboard" / Path(self.path).name)
        elif self.path == "/favicon.svg" or self.path == "/favicon.ico":
            self.serve_asset(APP_ROOT / "dashboard" / "favicon.svg")
        elif self.path == "/api/overview":
            body = json.dumps(dashboard_data(ROOT), ensure_ascii=False, allow_nan=False).encode("utf-8")
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
            allowed = {r["file"] for r in dashboard_data(ROOT)["reports"]}
            if name not in allowed:
                self.send_error(404)
                return
            self.respond((ROOT / "reports" / name).read_bytes(), "text/plain; charset=utf-8")
        else:
            self.send_error(404)

    def serve_asset(self, path):
        """静态资源带 ETag 协商缓存：内容未变时返回 304，不再整份重传。"""
        try:
            stat = path.stat()
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        etag = f'W/"{int(stat.st_mtime)}-{stat.st_size}"'
        if self.headers.get("If-None-Match") == etag:
            self.respond(b"", "text/plain; charset=utf-8", status=304, etag=etag)
            return
        self.respond(body, ASSET_TYPES[path.suffix], etag=etag)

    def respond(self, body, content_type, status=200, headers=None, *, etag=None):
        body, gzip_used = self.maybe_gzip(body, content_type)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if gzip_used:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def maybe_gzip(self, body, content_type):
        """对可压缩类型按 Accept-Encoding 协商 gzip，太小或不支持则原样返回。"""
        accepts = (self.headers.get("Accept-Encoding") or "").lower()
        if (len(body) >= 1024 and "gzip" in accepts and content_type.split(";")[0] in COMPRESSIBLE):
            return gzip.compress(body, 6), True
        return body, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    logging.info("看板只监听本机 127.0.0.1:%s", args.port)
    if os.environ.get("TRADE_DASHBOARD_TAILSCALE_LOGIN"):
        logging.info("看板使用 Tailscale Serve 登录身份")
    elif not auth_config()[1]:
        logging.error("TRADE_DASHBOARD_PASSWORD 未设置，所有请求将返回 503")
    server.serve_forever()


if __name__ == "__main__":
    main()
