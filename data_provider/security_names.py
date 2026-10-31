"""证券简称缓存。代码是标识；这里只保存行情源提供的展示名称，不主动联网。"""

import logging
from functools import lru_cache
from pathlib import Path
import re
import sqlite3

from .codes import normalize_stock_code

CACHE_PATH = Path("data/security_names.sqlite3")
logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _read(path, modified, size):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
        return dict(connection.execute("SELECT code,name FROM securities"))


def names():
    """只读缓存；未创建或暂不可用时由调用方显示代码。"""
    path = CACHE_PATH.resolve()
    try:
        stat = path.stat()
        return dict(_read(path, stat.st_mtime_ns, stat.st_size))
    except FileNotFoundError:
        return {}
    except (OSError, sqlite3.Error):
        logger.warning("证券名称缓存读取失败", exc_info=True)
        return {}


def security_name(code):
    code = normalize_stock_code(code)
    return "现金" if code == "CASH" else names().get(code, code)


def remember(values):
    """合并行情顺带返回的名称；失败不影响报价、成交和旧缓存。"""
    rows = []
    for code, name in values.items():
        code = normalize_stock_code(code)
        if not isinstance(name, str):
            continue
        name = name.strip()
        if re.fullmatch(r"\d{6}", code) and name and name != code and len(name) <= 80:
            rows.append((code, name))
    if not rows:
        return
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(CACHE_PATH, timeout=2) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS securities (code TEXT PRIMARY KEY, name TEXT NOT NULL)")
            connection.executemany("INSERT INTO securities VALUES (?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name", rows)
        _read.cache_clear()
    except (OSError, sqlite3.Error):
        logger.warning("证券名称缓存更新失败，不影响行情", exc_info=True)
