"""网页主动取价：后台单次刷新，报价仅留在内存，不写账户或收盘成绩。"""

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
        self._last_start = float("-inf")

    def read(self, path):
        with self._lock:
            return deepcopy(self._state) if self._path == Path(path).resolve() else {}

    def start(self, path, revision, codes, day):
        """合并重复点击；取数期间不持有账户锁，完成后再按账户版本应用。"""
        path = Path(path).resolve()
        codes = sorted(set(codes))
        with self._lock:
            if self._state.get("running"):
                return
            same = (path == self._path and self._state.get("revision") == revision
                    and self._state.get("day") == day)
            if same and time.monotonic() - self._last_start < 15:
                return
            previous = self._state.get("quotes", {}) if same else {}
            self._path = path
            self._last_start = time.monotonic()
            self._state = {"running": True, "revision": revision, "day": day,
                           "quotes": previous,
                           "missing": self._state.get("missing", []) if same else [], "error": None,
                           "fetched_at": self._state.get("fetched_at") if same else None}
            threading.Thread(target=self._fetch, args=(codes,), daemon=True).start()

    def _fetch(self, codes):
        try:
            from data_provider.manager import realtime_quotes

            prices, _ = realtime_quotes(codes) if codes else ({}, {})
            prices = {code: value for code, value in prices.items()
                      if code in codes and type(value) in (int, float)
                      and math.isfinite(value) and value > 0}
            error = None
        except Exception:
            logger.exception("网页行情刷新失败")
            prices, error = {}, "行情获取失败，请稍后重试。"
        fetched_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        with self._lock:
            quotes = dict(self._state["quotes"])
            quotes.update({code: {"price": value, "fetched_at": fetched_at}
                           for code, value in prices.items() if code in codes})
            missing = [code for code in codes if code not in prices]
            self._state.update(running=False, quotes=quotes, missing=missing,
                               fetched_at=fetched_at, error=error)
