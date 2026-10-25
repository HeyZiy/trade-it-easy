# -*- coding: utf-8 -*-
"""
模拟账户注册表 — 谁存在，一处枚举
===================================

CONTEXT.md「单池」：独立策略账户 = 独立台账文件。本模块只回答"现在有哪几个
模拟账户"，各账户的台账路径、标签与收盘基准定义在各自家里：

- src/etf/config.py         ETF 共享组合（主台账，含 core/satellite 两标签）
- src/quality_pool/config.py 质量池轮动（独立台账）
- src/etf/rotation.py        ETF 跨资产动量轮动（独立台账）

与 src/trade_ledger/ledger.ACCOUNTS 的分工：那是流水行 account 标签的
合法集合（一个台账文件可含多个标签，主台账即如此）；本表是台账文件级的
模拟账户枚举，供收盘成绩（portfolio_report）等报告消费方读取——消费方
不再逐家 import 台账路径。all_accounts() 调用时才 import 各家（注册表
顺序 = 报告章节顺序）；新增账户 = 在其家里定义描述符 + 此处加一行。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass
class SimAccount:
    """一个模拟账户的报告事实：台账在哪、叫什么、跟什么基准比。

    benchmark 为 collect_close 的现形透传（{"name", "weights"|"index"}），
    形状归 src/trade_ledger/performance 所有。
    """
    ledger_path: str
    label: str
    benchmark: Dict


def all_accounts():
    """全部模拟账户，报告章节顺序（共享 → 质量池 → 跨资产轮动）。"""
    from src.etf.config import SIM_ACCOUNT as etf_shared
    from src.etf.rotation import SIM_ACCOUNT as rotation
    from src.quality_pool.config import SIM_ACCOUNT as quality_pool

    return [etf_shared, quality_pool, rotation]
