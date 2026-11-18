"""证券简称缓存。代码是标识；简称来自已有行情和证券资料，读取不联网。"""

import logging
from functools import lru_cache
from pathlib import Path
import re
import sqlite3

from .codes import is_a_stock_code, is_etf_code, normalize_stock_code

CACHE_PATH = Path("data/security_names.sqlite3")
logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def _read(path, modified, size):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
        return dict(connection.execute("SELECT code,name FROM securities"))


def names(*, path=None):
    """只读缓存；未创建或暂不可用时由调用方显示代码。"""
    path = (path or CACHE_PATH).resolve()
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


def names_for(codes, *, path=None):
    """批量读取已知简称，保留请求中的代码写法，省略未知简称。"""
    known = names(path=path)
    normalized = {code: normalize_stock_code(code) for code in codes}
    return {code: known[plain] for code, plain in normalized.items() if plain in known}


def _valid_name(code, name):
    if isinstance(name, str):
        name = name.strip()
        if name and name != code and name.lower() not in {"nan", "none", "<na>"} and len(name) <= 80:
            return name
    return None


def remember(values, *, path=None, overwrite=True):
    """保存已取到的简称；失败不影响原数据和旧缓存。"""
    rows = []
    for code, name in values.items():
        code = normalize_stock_code(code)
        name = _valid_name(code, name)
        if re.fullmatch(r"\d{6}", code) and name:
            rows.append((code, name))
    if not rows:
        return
    path = path or CACHE_PATH
    on_conflict = "DO UPDATE SET name=excluded.name" if overwrite else "DO NOTHING"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path, timeout=2) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS securities (code TEXT PRIMARY KEY, name TEXT NOT NULL)")
            connection.executemany(f"INSERT INTO securities VALUES (?,?) ON CONFLICT(code) {on_conflict}", rows)
        _read.cache_clear()
    except (OSError, sqlite3.Error):
        logger.warning("证券名称缓存更新失败，不影响行情", exc_info=True)


def is_listed_code(code):
    return (isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code)
            and (is_etf_code(code) or is_a_stock_code(code)))


def lookup(codes):
    """按需补查场内简称，ETF 优先用新浪目录中的展示简称。"""
    from .bars import get_etf_universe
    from .fetchers.amazingdata.basics import fetch_security_names

    codes = set(codes)
    known = names_for(codes)
    pending = codes - known.keys()
    if not pending:
        return known

    etfs = {code for code in pending if is_etf_code(code)}
    stocks = pending - etfs
    found = {}
    if etfs:
        try:
            directory = get_etf_universe()
            if directory is not None:
                found.update((code, name) for code, name in zip(directory["code"], directory["name"])
                             if code in etfs and _valid_name(code, name))
        except Exception:
            logger.warning("新浪 ETF 简称查询失败", exc_info=True)

    for security_type, needed in (("EXTRA_ETF", etfs - found.keys()), ("EXTRA_STOCK_A", stocks)):
        if needed:
            try:
                directory = fetch_security_names(security_type)
                found.update((code, name) for code, name in directory.items() if code in needed)
            except (Exception, SystemExit):
                logger.warning("AmazingData %s 简称查询失败", security_type, exc_info=True)

    remember(found, overwrite=False)
    return names_for(codes)
