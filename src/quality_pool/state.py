# -*- coding: utf-8 -*-
"""质量池模拟盘状态 — data/quality_pool_state.json 读写。

三个跨日状态（规格四、五节的执行约定）：
- next_signal_date：下一调仓信号日（每 20 个交易日；数据暂停也照常推进）；
- pending_plan：信号日写、次日执行日消费后清除（T+1 非紧邻按 STALE 丢弃）；
- exit_queue：未完成退出队列（停牌/跌停/可卖量不足 → 每交易日 09:31 重试）。

文件损坏宁可炸（同 LedgerError 纪律），不带病默认。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

STATE_PATH = "data/quality_pool_state.json"
STATE_VERSION = 1

_EMPTY = {
    "version": STATE_VERSION,
    "next_signal_date": None,
    "pending_plan": None,
    "exit_queue": {},
}


def load_state(path: Union[str, Path] = STATE_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return dict(_EMPTY)
    raw = json.loads(p.read_text(encoding="utf-8"))
    state = dict(_EMPTY)
    state.update(raw)
    state["exit_queue"] = dict(state.get("exit_queue") or {})
    return state


def save_state(state: dict, path: Union[str, Path] = STATE_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = STATE_VERSION
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def put_plan(state: dict, plan: Optional[dict]) -> None:
    state["pending_plan"] = plan


def pop_plan(state: dict) -> Optional[dict]:
    plan = state.get("pending_plan")
    state["pending_plan"] = None
    return plan
