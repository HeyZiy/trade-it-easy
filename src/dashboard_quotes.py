"""网页主动取价：后台独立刷新场内报价与场外单位净值。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
import math
from pathlib import Path
import threading
import time

SHANGHAI = timezone(timedelta(hours=8))
logger = logging.getLogger(__name__)


class QuoteRefresh:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}
        self._path = None
        self._fund_codes = ()
        self._last_start = float("-inf")

    def read(self, path):
        with self._lock:
            return deepcopy(self._state) if self._path == Path(path).resolve() else {}

    def start(self, path, revision, codes, day, *, fund_codes=()):
        """合并重复点击；取数期间不持有账户锁，完成后再按账户版本应用。"""
        path = Path(path).resolve()
        codes = sorted(set(codes))
        fund_codes = tuple(sorted(set(fund_codes)))
        with self._lock:
            if self._state.get("running") or self._state.get("funds", {}).get("running"):
                return
            same = (path == self._path and self._state.get("revision") == revision
                    and self._state.get("day") == day)
            if same and fund_codes == self._fund_codes and time.monotonic() - self._last_start < 15:
                return
            previous = self._state.get("quotes", {}) if same else {}
            self._path = path
            self._fund_codes = fund_codes
            self._last_start = time.monotonic()
            self._state = {"running": True, "revision": revision, "day": day,
                           "quotes": previous,
                           "missing": self._state.get("missing", []) if same else [], "error": None,
                           "fetched_at": self._state.get("fetched_at") if same else None,
                           "funds": {"running": True} if fund_codes else {}}
            threading.Thread(target=self._fetch, args=(codes,), daemon=True).start()
            if fund_codes:
                threading.Thread(target=self._fetch_funds, args=(path, day, fund_codes), daemon=True).start()

    def _fetch(self, codes):
        try:
            from data_provider import realtime_prices

            prices = realtime_prices(codes)
            prices = {code: value for code, value in prices.items()
                      if code in codes and type(value) in (int, float)
                      and math.isfinite(value) and value > 0}
            error = None
        except Exception:
            logger.exception("网页行情刷新失败")
            prices, error = {}, "行情获取失败，请稍后重试。"
        fetched_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        with self._lock:
            missing = [code for code in codes if code not in prices]
            # 完整批次原子替换；部分成功不能把新旧报价混成一次账户估值。
            if not missing:
                self._state.update(quotes={code: {"price": prices[code], "fetched_at": fetched_at}
                                           for code in codes}, fetched_at=fetched_at)
            self._state.update(running=False, missing=missing, error=error)

    def _fetch_funds(self, path, day, codes):
        from src.etf_account import refresh_fund_navs

        try:
            result = refresh_fund_navs(path, day)
        except Exception:
            logger.exception("网页场外净值刷新失败")
            result = {"updated": [], "unchanged": [], "failed": list(codes)}
        with self._lock:
            self._state["funds"] = {"running": False, **result}
