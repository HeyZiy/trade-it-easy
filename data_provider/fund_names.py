"""场外基金简称来自天天基金目录，和场内证券分开缓存。"""

import json
import logging
from threading import Lock
import time

import requests

from . import security_names

DIRECTORY_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
logger = logging.getLogger(__name__)
_refresh_lock = Lock()
_last_attempt = float("-inf")


def _cache_path():
    return security_names.CACHE_PATH.with_name("fund_names.sqlite3")


def names_for(codes):
    return security_names.names_for(codes, path=_cache_path()) if codes else {}


def _read_directory():
    response = requests.get(DIRECTORY_URL, timeout=(3, 5))
    response.raise_for_status()
    prefix, separator, text = response.text.lstrip("\ufeff").strip().partition("=")
    if not separator or prefix.strip() != "var r":
        raise ValueError("基金目录格式无效")
    rows = json.loads(text.strip().removesuffix(";"))
    if not isinstance(rows, list) or not rows:
        raise ValueError("基金目录为空或无效")
    return {row[0]: row[2] for row in rows if isinstance(row, list) and len(row) >= 3
            and isinstance(row[0], str) and len(row[0]) == 6 and row[0].isascii() and row[0].isdigit()}


def _refresh_directory():
    global _last_attempt
    path = _cache_path()
    with _refresh_lock:
        if time.monotonic() - _last_attempt < 60:
            return
        try:
            if time.time() - path.stat().st_mtime < 86400:
                return
        except FileNotFoundError:
            pass
        _last_attempt = time.monotonic()
        security_names.remember(_read_directory(), path=path)


def lookup(codes):
    """缺简称时批量补查目录。上游不可用时仍可使用已有简称。"""
    known = names_for(codes)
    if len(known) < len(codes):
        try:
            _refresh_directory()
            return names_for(codes)
        except (OSError, ValueError, requests.RequestException):
            logger.warning("场外基金简称更新失败", exc_info=True)
    return known
