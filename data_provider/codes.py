# -*- coding: utf-8 -*-
"""
===================================
股票代码规范化与市场/类型判定
===================================

纯函数，不碰网络。集中存放项目所有代码判定规则：
- normalize_stock_code / canonical_stock_code：代码归一化
- classify_market：市场归类（只剩 cn——美股/港股不在支持范围）
- is_bse_code / is_st_stock / is_kc_cy_stock / is_etf_code：市场/类型判定
- is_us_stock_code：字母 ticker 守卫谓词（拒收用）
- ETF_PREFIXES：ETF 码族常量
- _split_prefix：sh/sz/bj 前缀拆分

个股日线多源与实时报价见 manager.py / realtime.py；ETF/指数日线见 bars.py。
"""
import re
from typing import Optional, Tuple

def normalize_stock_code(stock_code: str) -> str:
    """
    Normalize stock code by stripping exchange prefixes/suffixes.

    Accepted formats and their normalized results:
    - '600519'      -> '600519'   (already clean)
    - 'SH600519'    -> '600519'   (strip SH prefix)
    - 'SZ000001'    -> '000001'   (strip SZ prefix)
    - 'BJ920748'    -> '920748'   (strip BJ prefix, BSE)
    - 'sh600519'    -> '600519'   (case-insensitive)
    - '600519.SH'   -> '600519'   (strip .SH suffix)
    - '000001.SZ'   -> '000001'   (strip .SZ suffix)
    - '920748.BJ'   -> '920748'   (strip .BJ suffix, BSE)
    - 'AAPL'        -> 'AAPL'     (keep US stock ticker as-is)

    This function is applied at the DataProviderManager layer so that
    all individual fetchers receive a clean 6-digit code (for A-shares/ETFs).
    """
    code = stock_code.strip()
    upper = code.upper()

    # Strip SH/SZ/BJ prefix (e.g. SH600519 -> 600519, BJ920748 -> 920748)
    if upper.startswith(('SH', 'SZ', 'BJ')) and not upper.startswith(('SH.', 'SZ.', 'BJ.')):
        candidate = code[2:]
        if candidate.isdigit() and len(candidate) in (5, 6):
            return candidate

    # Strip .SH/.SZ/.BJ suffix (e.g. 600519.SH -> 600519, 920748.BJ -> 920748)
    if '.' in code:
        base, suffix = code.rsplit('.', 1)
        if suffix.upper() in ('SH', 'SZ', 'BJ') and base.isdigit():
            return base

    return code


ETF_PREFIXES = ("51", "52", "53", "55", "56", "58", "15", "16", "18")



def classify_market(code: str) -> str:
    """市场归类（只剩 cn——美股/港股不在支持范围）。

    港股代码不做显式拒收：流入 A 股接口后自然以"查无数据"失败。
    """
    return "cn"


def is_bse_code(code: str) -> bool:
    """
    Check if the code is a Beijing Stock Exchange (BSE) A-share code.

    BSE rules:
    - Old format (pre-2024): 8xxxxx (e.g. 838163), 4xxxxx (e.g. 430047)
    - New format (2024+, post full migration Oct 2025): 920xxx+
    Note: 900xxx are Shanghai B-shares, NOT BSE — must return False.
    """
    c = (code or "").strip().split(".")[0]
    if len(c) != 6 or not c.isdigit():
        return False
    return c.startswith(("8", "4")) or c.startswith("92")

def is_st_stock(name: str) -> bool:
    """
    Check if the stock is an ST or *ST stock based on its name.

    ST stocks have special trading rules and typically a ±5% limit.
    """
    n = (name or "").upper()
    return 'ST' in n

def is_kc_cy_stock(code: str) -> bool:
    """
    Check if the stock is a STAR Market (科创板) or ChiNext (创业板) stock based on its code.

    - STAR Market: Codes starting with 688
    - ChiNext: Codes starting with 300
    Both have a ±20% limit.
    """
    c = (code or "").strip().split(".")[0]
    return c.startswith("688") or c.startswith("30")


def canonical_stock_code(code: str) -> str:
    """
    Return the canonical (uppercase) form of a stock code.

    This is a display/storage layer concern, distinct from normalize_stock_code
    which strips exchange prefixes. Apply at system input boundaries to ensure
    consistent case across BOT, WEB UI, API, and CLI paths (Issue #355).

    Examples:
        'aapl'    -> 'AAPL'
        'AAPL'    -> 'AAPL'
        '600519'  -> '600519'  (digits are unchanged)
    """
    return (code or "").strip().upper()



def _split_prefix(code: str) -> Tuple[str, Optional[str]]:
    """返回 (numeric_code, market_prefix)。prefix 为 sh/sz/bj（无前缀时 None）。"""
    raw = (code or "").strip().lower()
    m = re.match(r"^(sh|sz|bj)([0-9]{6})$", raw)
    if m:
        return m.group(2), m.group(1)
    return raw, None


def is_etf_code(code: str) -> bool:
    """按码族判定是否为 ETF（51/52/53/55/56/58/15/16/18，码族→市场无歧义）。

    53xxxx（上证系列 ETF）、55xxxx（科创债 ETF）是上交所 2025 年启用的新码族。
    """
    num, _ = _split_prefix(code)
    return num[:2] in ETF_PREFIXES


def is_us_stock_code(code: str) -> bool:
    """字母 ticker 守卫谓词（全仓唯一实现）：1-5 个大写字母、可选 .X 后缀。

    打到 A 股数据接口；SPX/DJI 等指数符号同样按字母 ticker 拒收。
    """
    c = (code or "").strip().upper()
    return bool(re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', c))

