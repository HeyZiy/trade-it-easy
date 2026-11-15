"""Read archived timing reports and generate separate per-ETF web snapshots."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import re
import threading

from data_provider.codes import is_etf_code
from src.json_store import save

SCHEMA = 1
MAX_JOBS = 4


class QueueFull(Exception):
    pass


class TimingReports:
    def __init__(self, cache, market_cache=None):
        self.cache = cache
        self.archive = cache / "archive"
        self.market_cache = market_cache
        self.lock = threading.Lock()
        self.jobs = {}
        self.errors = {}
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="etf-timing")

    def _path(self, code):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or not is_etf_code(code):
            raise ValueError("请输入 6 位沪深上市 ETF 代码。")
        return self.cache / f"{code}.json"

    def _load(self, code):
        path = self._path(code)
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("schema") == SCHEMA and report.get("code") == code:
                return report
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            logging.exception("无法读取 ETF 择时报告：%s", code)
        return None

    def _save(self, code, report):
        path = self._path(code)
        path.parent.mkdir(parents=True, exist_ok=True)
        save(path, report, compact=True)

    def catalog(self):
        from research.studies.timing.web_report import archive_catalog

        catalog = archive_catalog(self.archive)
        for path in self.cache.glob("*.json"):
            if not re.fullmatch(r"[0-9]{6}", path.stem) or not is_etf_code(path.stem):
                continue
            if report := self._load(path.stem):
                catalog[path.stem] = {"code": path.stem, "name": report["name"], "end": report["full"]["end"]}
        return sorted(catalog.values(), key=lambda row: row["code"])

    def overview(self, code):
        from research.studies.timing.web_report import archive_catalog, archive_groups

        with self.lock:
            phase = self.jobs.get(code)
            error = self.errors.get(code)
        saved = self._load(code)
        archive = archive_catalog(self.archive).get(code)
        groups = archive_groups(self.archive, code, archive is not None)
        if saved:
            groups = {"current": "网页生成 · 固定价格规则", **groups}
        if saved and saved["rolling_state"] == "pending" and not phase:
            error = error or "多起点计算未完成，可重新生成。"
        return {
            "code": code, "name": (saved or archive or {"name": f"ETF {code}"})["name"],
            "groups": groups, "phase": phase, "error": error,
            "version": saved["version"] if saved else None,
            "rolling_state": saved["rolling_state"] if saved else None,
            "rolling": saved["rolling"] if saved else [],
            "case_study": code == "159915" and self.case_study().is_file(),
        }

    def group(self, code, key):
        from research.studies.timing.web_report import archive_catalog, archive_group, archive_groups

        self._path(code)
        if key == "current":
            report = self._load(code)
            if report:
                return {**report["full"], "version": report["version"], "rolling": report["rolling"]}
        else:
            known = code in archive_catalog(self.archive)
            if key in archive_groups(self.archive, code, known):
                return archive_group(self.archive, code, key)
        raise FileNotFoundError(code)

    def case_study(self):
        return self.archive / "159915" / "summary.md"

    def start(self, code, refresh=False):
        from research.studies.timing.web_report import archive_catalog

        self._path(code)
        if not isinstance(refresh, bool):
            raise ValueError("刷新选项无效。")
        with self.lock:
            if code in self.jobs:
                return self.jobs[code]
            if not refresh and (self._load(code) is not None or code in archive_catalog(self.archive)):
                return "existing"
            if len(self.jobs) >= MAX_JOBS:
                raise QueueFull("已有多份报告在生成，请稍后再试。")
            self.errors.pop(code, None)
            self.jobs[code] = "queued"
            self.worker.submit(self._generate, code, refresh)
            return "queued"

    def _generate(self, code, refresh):
        try:
            from research.studies.timing.web_report import full_report, load_cached, load_live, rolling_report

            with self.lock:
                self.jobs[code] = "history"
            snapshot = load_cached(code, self.market_cache) if self.market_cache and not refresh else None
            name, history, source = snapshot or load_live(code)
            full, runners = full_report(history, source)
            report = {"schema": SCHEMA, "code": code, "name": name, "full": full,
                      "version": datetime.now(timezone.utc).isoformat(), "rolling_state": "pending", "rolling": []}
            self._save(code, report)
            with self.lock:
                self.jobs[code] = "rolling"
            report["rolling"] = rolling_report(history, runners)
            report["rolling_state"] = "ready"
            self._save(code, report)
        except ValueError as error:
            self._failed(code, str(error))
        except Exception:
            logging.exception("ETF 择时报告生成失败：%s", code)
            self._failed(code, "报告生成失败，请稍后重试。")
        finally:
            with self.lock:
                self.jobs.pop(code, None)

    def _failed(self, code, message):
        with self.lock:
            if len(self.errors) >= 64:
                self.errors.pop(next(iter(self.errors)))
            self.errors[code] = message
