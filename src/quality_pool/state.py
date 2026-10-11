# -*- coding: utf-8 -*-
"""质量池状态 — data/quality_pool_v2_state.json 读写。

三个跨日状态（规格四、五节的执行约定）：
- next_signal_date：原定20交易日节点，失败恢复不重设；
- pending_plan：盘前写、当日执行消费后清除，版本/执行日/截止日不符则丢弃；
- exit_queue：未完成退出队列（停牌/跌停/可卖量不足 → 每交易日 09:31 重试）。

signal_retry在取数前持久化，成功写名单后清除；中断次日重算信号。
deferred_codes只补判未知旧仓，确认出池加入退出队列，不生成新的买入名单。

文件损坏宁可炸（同 LedgerError 纪律），不带病默认。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Union

from src.quality_pool.config import STATE_PATH, STRATEGY_VERSION

STATE_VERSION = STRATEGY_VERSION

_EMPTY = {
    "version": STATE_VERSION,
    "next_signal_date": None,
    "pending_plan": None,
    "exit_queue": {},
    "signal_retry": False,
    "last_signal_date": None,
    "deferred_codes": [],
}


def load_state(path: Union[str, Path] = STATE_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return dict(_EMPTY, exit_queue={}, deferred_codes=[])
    raw = json.loads(p.read_text(encoding="utf-8"))
    if raw.get('version') != STATE_VERSION:
        raise ValueError('策略状态文件版本不兼容')
    state = dict(_EMPTY)
    state.update(raw)
    state["exit_queue"] = dict(state.get("exit_queue") or {})
    return state


def save_state(state: dict, path: Union[str, Path] = STATE_PATH) -> None:
    """同目录临时文件替换；写入失败时保留上一份完整状态。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = STATE_VERSION
    payload = json.dumps(state, ensure_ascii=False, indent=2)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=p.parent,
                prefix=p.name + ".", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, p)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def put_plan(state: dict, plan: Optional[dict]) -> None:
    state["pending_plan"] = (dict(plan, strategy_version=STATE_VERSION)
                             if plan is not None else None)


def pop_plan(state: dict) -> Optional[dict]:
    plan = state.get("pending_plan")
    state["pending_plan"] = None
    return plan
