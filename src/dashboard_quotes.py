"""看板后台补查场内收盘价和场外单位净值。"""

from copy import deepcopy
import logging
from pathlib import Path
import threading
import time

from src import etf_account
from src.trade_ledger.closing_prices import ClosingPrices

logger = logging.getLogger(__name__)


class QuoteRefresh:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}
        self._request = None
        self._last_start = float("-inf")

    def read(self, path):
        with self._lock:
            return deepcopy(self._state) if self._request and self._request[0] == Path(path).resolve() else {}

    def start(self, path, today):
        """合并重复点击；记账或配置变化后允许立即刷新新增标的。"""
        path = Path(path).resolve()
        account = etf_account.load(path)
        price_day = etf_account.valuation_day(today)
        fund_codes = tuple(sorted(etf_account.snapshot(account, today.isoformat())["fund_positions"]))
        request = (path, today, price_day, etf_account.revision(account), fund_codes)
        with self._lock:
            if self._state.get("running") or self._state.get("funds", {}).get("running"):
                return
            if request == self._request and time.monotonic() - self._last_start < 15:
                return
            self._request = request
            self._last_start = time.monotonic()
            self._state = {"running": True, "date": price_day, "error": None,
                           "funds": {"running": True} if fund_codes else {}}
            threading.Thread(target=self._fetch, args=(path, today, price_day), daemon=True).start()
            if fund_codes:
                threading.Thread(target=self._fetch_funds,
                                 args=(path, today.isoformat(), fund_codes), daemon=True).start()

    def _fetch(self, path, today, price_day):
        try:
            etf_account.refresh_close_prices(path, today, price_day, ClosingPrices().close_prices)
            error = None
        except Exception:
            logger.exception("网页收盘价刷新失败")
            error = "收盘价暂时无法更新，请稍后重试。"
        with self._lock:
            self._state.update(running=False, error=error)

    def _fetch_funds(self, path, day, codes):
        try:
            result = etf_account.refresh_fund_navs(path, day)
        except Exception:
            logger.exception("网页场外净值刷新失败")
            result = {"updated": [], "unchanged": [], "failed": list(codes)}
        with self._lock:
            self._state["funds"] = {"running": False, **result}
