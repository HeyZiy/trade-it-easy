# -*- coding: utf-8 -*-
"""报告数据源注册表。

投资账户提供共享资金与持仓；质量池及跨资产动量的独立观察各提供一份
运行记录。数据源不等于策略：多资产配置和接入账户后的轮动都从投资账户分配份额。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class AccountSource:
    """一份资金账户或观察记录的报告事实：台账在哪、叫什么、跟什么基准比。

    benchmark 为 collect_close 的现形透传（{"name", "weights"|"index"}），
    形状归 src/trade_ledger/performance 所有。
    """
    ledger_path: str
    label: str
    benchmark: Dict
    is_investment: bool = False


def all_sources():
    """枚举报告数据源：投资账户及各独立观察记录。"""
    from src.etf.config import ACCOUNT_SOURCE as core
    from src.etf.rotation import ACCOUNT_SOURCE as rotation
    from src.quality_pool.config import ACCOUNT_SOURCE as quality_pool

    return [core, quality_pool, rotation]
